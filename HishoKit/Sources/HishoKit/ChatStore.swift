// 役割: 会話状態の唯一の持ち主(アプリ層)。popover 破棄でも stream を保持し続ける。
// session_id はアプリ起動時に生成し、clear() で新スレッドとして再生成する(履歴 replay はサーバ側)。
import Foundation
import Observation

@MainActor
@Observable
public final class ChatStore {
    public private(set) var messages: [ChatMessage] = []
    public private(set) var sessionID: String
    public var isStreaming: Bool { streamTask != nil }

    private let client: any ChatStreaming
    private var streamTask: Task<Void, Never>?

    public init(client: any ChatStreaming) {
        self.client = client
        self.sessionID = Self.newSessionID()
    }

    private static func newSessionID() -> String {
        "sess-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
    }

    public func send(_ text: String, port: Int) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, streamTask == nil else { return }

        messages.append(ChatMessage(role: .user, text: trimmed, status: .complete))
        let assistantID = UUID()
        messages.append(ChatMessage(id: assistantID, role: .assistant, text: "", status: .streaming))

        streamTask = Task { [weak self] in
            guard let self else { return }
            var finished = false
            do {
                for try await event in self.client.stream(
                    sessionID: self.sessionID, userMessage: trimmed, port: port) {
                    switch event {
                    case .delta(let piece):
                        self.update(assistantID) { $0.text += piece }
                    case .finish:
                        break  // 完了確定は .done で行う
                    case .done:
                        finished = true
                        self.update(assistantID) { $0.status = .complete }
                    case .error(let message):
                        finished = true
                        self.update(assistantID) { $0.status = .error(message) }
                    }
                }
                if !finished {
                    self.update(assistantID) { $0.status = .error("応答が途中で切れました") }
                }
            } catch is CancellationError {
                self.update(assistantID) { $0.status = .error("キャンセルしました") }
            } catch {
                self.update(assistantID) { $0.status = .error(error.localizedDescription) }
            }
            self.streamTask = nil
        }
    }

    public func cancel() {
        streamTask?.cancel()
    }

    /// 会話を新スレッドとして開始し直す。画面を空にし session_id を再生成する。
    /// SQLite の turns / RAG chunks は触らない(内容は保存されたまま残る)。
    public func clear() {
        streamTask?.cancel()   // 進行中なら止める(streamTask の nil 戻しは task 自身の完了処理に任せる)
        messages.removeAll()
        sessionID = Self.newSessionID()
    }

    /// テストと graceful 終了用: 進行中 stream の完了を待つ。
    public func awaitStreamEnd() async {
        await streamTask?.value
    }

    private func update(_ id: UUID, _ mutate: (inout ChatMessage) -> Void) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        mutate(&messages[i])
    }
}

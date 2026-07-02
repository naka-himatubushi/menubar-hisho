// 役割: popover の中身 — 状態バナー + 会話ログ(逐次描画・自動スクロール) + 入力欄。
// ホスト(MenuBarExtra / NSPopover)非依存。状態は外から渡された @Observable を描くだけ。
import SwiftUI

public struct ChatView: View {
    private let chat: ChatStore
    private let core: CoreProcessManager
    @State private var input = ""
    @FocusState private var inputFocused: Bool

    public init(chat: ChatStore, core: CoreProcessManager) {
        self.chat = chat
        self.core = core
    }

    public var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: core.state) { core.restart() }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(chat.messages) { message in
                            MessageRow(message: message).id(message.id)
                        }
                    }
                    .padding(8)
                }
                .onChange(of: chat.messages.last?.text) {
                    if let last = chat.messages.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
            Divider()
            HStack(spacing: 8) {
                TextField("メッセージを入力…", text: $input)
                    .textFieldStyle(.roundedBorder)
                    .focused($inputFocused)
                    .onSubmit(send)
                    .disabled(!canSend)
                Button("送信", action: send)
                    .disabled(!canSend || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(8)
        }
        .frame(width: 360, height: 480)
        .onAppear { inputFocused = true }
    }

    private var canSend: Bool {
        core.state == .ready && !chat.isStreaming
    }

    private func send() {
        chat.send(input, port: core.port)
        input = ""
        inputFocused = true
    }
}

/// ready 以外のときだけ出る一行バナー。core 停止時は手動再起動ボタン(自動再起動はしない)。
struct StatusBanner: View {
    let state: CoreState
    let onRestart: () -> Void

    var body: some View {
        switch state {
        case .ready:
            EmptyView()
        case .startingCore:
            banner("秘書を起動中…", tint: .secondary)
        case .warmingModel:
            banner("モデルを準備中…(初回は数十秒かかります)", tint: .secondary)
        case .ollamaDown:
            banner("ollama に接続できません — `ollama serve` を確認", tint: .orange)
        case .coreStopped(let reason):
            HStack {
                Text("core 停止: \(reason)").font(.caption).foregroundStyle(.red)
                Spacer()
                Button("再起動", action: onRestart).font(.caption)
            }
            .padding(6)
            .background(.red.opacity(0.08))
        }
    }

    private func banner(_ text: String, tint: Color) -> some View {
        HStack {
            ProgressView().controlSize(.small)
            Text(text).font(.caption).foregroundStyle(tint)
            Spacer()
        }
        .padding(6)
        .background(.quaternary.opacity(0.4))
    }
}

/// 1 メッセージの行。user は右寄せ、assistant は左寄せ + streaming/エラー表示。
struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                Text(message.text.isEmpty && message.status == .streaming ? "…" : message.text)
                    .textSelection(.enabled)
                if case .error(let reason) = message.status {
                    Text("⚠️ \(reason)").font(.caption2).foregroundStyle(.red)
                }
            }
            .padding(8)
            .background(message.role == .user ? Color.accentColor.opacity(0.15)
                                              : Color.gray.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            if message.role == .assistant { Spacer(minLength: 40) }
        }
    }
}

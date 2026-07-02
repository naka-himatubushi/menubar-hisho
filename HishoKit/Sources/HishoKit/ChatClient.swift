// 役割: core への chat リクエスト組立と、URLSession.bytes による SSE ストリーム消費。
// ChatStreaming protocol が ChatStore へのテスト用差し込み口(seam)。
import Foundation

/// core 契約に沿った /v1/chat/completions リクエストを組み立てる純関数部。
public struct ChatRequestBuilder: Sendable {
    private let baseURL: URL

    public init(port: Int) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    public func makeRequest(sessionID: String, userMessage: String) throws -> URLRequest {
        var req = URLRequest(url: baseURL.appendingPathComponent("v1/chat/completions"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("popover", forHTTPHeaderField: "X-Hisho-Source")
        req.timeoutInterval = 300  // streaming 中はデータ到着ごとにリセットされる
        let body: [String: Any] = [
            "session_id": sessionID,
            "stream": true,
            "messages": [["role": "user", "content": userMessage]],
        ]
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return req
    }
}

/// ChatStore が消費するストリーミング抽象。テストでは Fake、実機では CoreChatClient。
public protocol ChatStreaming: Sendable {
    func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error>
}

/// 実トランスポート。core が error frame 後に [DONE] なしで close する仕様に合わせ、
/// エラーイベント後は素直にストリームを終える。
public struct CoreChatClient: ChatStreaming {
    public init() {}

    public func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let req = try ChatRequestBuilder(port: port)
                        .makeRequest(sessionID: sessionID, userMessage: userMessage)
                    let (bytes, response) = try await URLSession.shared.bytes(for: req)
                    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                        throw URLError(.badServerResponse)
                    }
                    let parser = SSEParser()
                    for try await line in bytes.lines {
                        guard let event = parser.parse(line: line) else { continue }
                        continuation.yield(event)
                        if case .error = event { break }
                        if case .done = event { break }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

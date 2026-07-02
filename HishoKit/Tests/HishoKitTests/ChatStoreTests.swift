// 役割: ChatStore のメッセージ状態遷移 — 正常完走 / error frame / 途中切断 / 二重送信ガード。
import Foundation
import Testing
@testable import HishoKit

/// 台本どおりのイベントを流す Fake。finish(throwing:) で異常系も再現。
struct FakeStreamer: ChatStreaming {
    let events: [SSEEvent]
    var thrown: URLError? = nil  // Sendable な具象型に限定 (Swift 6 strict concurrency)

    func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { c in
            for e in events { c.yield(e) }
            c.finish(throwing: thrown)
        }
    }
}

@Suite @MainActor struct ChatStoreTests {
    @Test func happyPathAccumulatesDeltasAndCompletes() async {
        let store = ChatStore(client: FakeStreamer(
            events: [.delta("こん"), .delta("にちは"), .finish(reason: "stop"), .done]))
        store.send("やあ", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages.count == 2)
        #expect(store.messages[0].role == .user)
        #expect(store.messages[0].text == "やあ")
        #expect(store.messages[1].role == .assistant)
        #expect(store.messages[1].text == "こんにちは")
        #expect(store.messages[1].status == .complete)
        #expect(store.isStreaming == false)
    }

    @Test func errorFrameMarksAssistantError() async {
        let store = ChatStore(client: FakeStreamer(
            events: [.delta("途中"), .error(message: "ollama 500: boom")]))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages[1].text == "途中")  // partial 内容は残す
        #expect(store.messages[1].status == .error("ollama 500: boom"))
    }

    @Test func abruptCloseWithoutDoneIsError() async {
        // core 死亡などで [DONE] なしに stream が終わったケース
        let store = ChatStore(client: FakeStreamer(events: [.delta("とち")]))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages[1].status == .error("応答が途中で切れました"))
    }

    @Test func transportErrorIsSurfaced() async {
        let store = ChatStore(client: FakeStreamer(
            events: [], thrown: URLError(.cannotConnectToHost)))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        if case .error = store.messages[1].status {} else {
            Issue.record("expected .error, got \(store.messages[1].status)")
        }
    }

    @Test func emptyAndDuplicateSendsAreIgnored() async {
        let store = ChatStore(client: FakeStreamer(events: [.done]))
        store.send("   ", port: 51100)
        #expect(store.messages.isEmpty)

        store.send("a", port: 51100)
        store.send("b", port: 51100)  // streaming 中の二重送信は無視
        await store.awaitStreamEnd()
        #expect(store.messages.filter { $0.role == .user }.count == 1)
    }
}

// 役割: popover マーカー・session_id・「最新 user メッセージ 1 件のみ」という core 契約をリクエストが守るか検証。
import Foundation
import Testing
@testable import HishoKit

@Suite struct ChatRequestBuilderTests {
    @Test func requestFollowsCoreContract() throws {
        let req = try ChatRequestBuilder(port: 51100)
            .makeRequest(sessionID: "sess-abc123", userMessage: "こんにちは")

        #expect(req.url?.absoluteString == "http://127.0.0.1:51100/v1/chat/completions")
        #expect(req.httpMethod == "POST")
        #expect(req.value(forHTTPHeaderField: "X-Hisho-Source") == "popover")
        #expect(req.value(forHTTPHeaderField: "Content-Type") == "application/json")

        let body = try JSONSerialization.jsonObject(with: req.httpBody!) as! [String: Any]
        #expect(body["session_id"] as? String == "sess-abc123")
        #expect(body["stream"] as? Bool == true)
        let msgs = body["messages"] as! [[String: String]]
        #expect(msgs.count == 1)  // 履歴 replay はサーバ側 — 最新 1 件だけ送る
        #expect(msgs[0] == ["role": "user", "content": "こんにちは"])
        #expect(body["model"] == nil)  // core 既定モデルに任せる
    }

    @Test func fallbackPortIsRespected() throws {
        let req = try ChatRequestBuilder(port: 60123)
            .makeRequest(sessionID: "s", userMessage: "x")
        #expect(req.url?.absoluteString.contains("127.0.0.1:60123") == true)
    }
}

// 役割: SSEParser が core の実 SSE 形状(role初回/delta/finish/[DONE]/error frame)を正しく分類するか検証。
import Testing
@testable import HishoKit

@Suite struct SSEParserTests {
    let parser = SSEParser()

    @Test func contentDelta() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"こん"},"finish_reason":null}]}"#
        #expect(parser.parse(line: line) == .delta("こん"))
    }

    @Test func roleOnlyFirstChunkIsIgnored() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}"#
        #expect(parser.parse(line: line) == nil)
    }

    @Test func finishChunk() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}"#
        #expect(parser.parse(line: line) == .finish(reason: "stop"))
    }

    @Test func doneSentinel() {
        #expect(parser.parse(line: "data: [DONE]") == .done)
    }

    @Test func errorFrame() {
        let line = #"data: {"error":{"message":"ollama 500: boom","type":"hisho_error","param":null,"code":null}}"#
        #expect(parser.parse(line: line) == .error(message: "ollama 500: boom"))
    }

    @Test func nonDataLinesAreIgnored() {
        #expect(parser.parse(line: "") == nil)
        #expect(parser.parse(line: ": comment") == nil)
        #expect(parser.parse(line: "event: x") == nil)
    }
}

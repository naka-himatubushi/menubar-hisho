"""chat_stream が /api/chat に正しいbodyを送り、行を parser に流すことを検証(httpxモック)。"""
import json
import httpx
import pytest
from hisho_core.llm import chat_stream


def _mock_factory(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        ndjson = (json.dumps({"message": {"content": "hi"}, "done": False}) + "\n"
                  + json.dumps({"done": True, "done_reason": "stop", "eval_count": 1}) + "\n")
        return httpx.Response(200, content=ndjson.encode())
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory


@pytest.mark.asyncio
async def test_stream_sends_body_and_parses():
    cap = {}
    evts = [e async for e in chat_stream(
        [{"role": "user", "content": "hi"}],
        model="qwen3.6:35b-a3b", ollama_host="http://127.0.0.1:11434",
        num_ctx=8192, keep_alive="30m", think=False,
        client_factory=_mock_factory(cap))]
    assert cap["url"].endswith("/api/chat")
    assert cap["body"]["model"] == "qwen3.6:35b-a3b"
    assert cap["body"]["stream"] is True
    assert cap["body"]["think"] is False
    assert cap["body"]["options"]["num_ctx"] == 8192
    assert cap["body"]["keep_alive"] == "30m"
    assert {"type": "delta", "content": "hi"} in evts
    assert any(e["type"] == "done" for e in evts)

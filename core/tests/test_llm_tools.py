"""役割: Ollama tool_calls → tool_call イベント変換と、chat_stream の tools 引数のテスト。"""
import asyncio
import json
from hisho_core import llm


async def _collect(aiter):
    return [e async for e in aiter]


async def _lines(objs):
    for o in objs:
        yield (json.dumps(o) + "\n").encode()


def test_tool_calls_become_tool_call_events():
    objs = [
        {"message": {"tool_calls": [
            {"id": "call_1", "function": {"name": "forget_memories", "arguments": {"query": "猫"}}}]}},
        {"message": {"content": ""}, "done": True, "done_reason": "tool_calls"},
    ]
    events = asyncio.run(_collect(llm.iter_ollama_events(_lines(objs))))
    tc = [e for e in events if e["type"] == "tool_call"]
    assert len(tc) == 1
    assert tc[0]["name"] == "forget_memories"
    assert tc[0]["arguments"] == {"query": "猫"}
    assert any(e["type"] == "done" for e in events)


def test_plain_content_still_delta():
    objs = [{"message": {"content": "はい"}}, {"message": {"content": ""}, "done": True}]
    events = asyncio.run(_collect(llm.iter_ollama_events(_lines(objs))))
    assert [e for e in events if e["type"] == "delta"][0]["content"] == "はい"

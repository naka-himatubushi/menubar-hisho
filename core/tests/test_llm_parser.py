"""ollama NDJSON → 中立イベント。thinking除去・delta・done・error を検証。"""
import json
import pytest
from hisho_core.llm import iter_ollama_events


async def _lines(objs):
    """オブジェクトをNDJSON bytes行として yield する。"""
    for o in objs:
        yield (json.dumps(o) + "\n").encode()


async def _collect(objs):
    """iter_ollama_events の結果を list に集める。"""
    return [e async for e in iter_ollama_events(_lines(objs))]


async def test_deltas_and_done():
    """複数の delta とそれに続く done イベントを検証。"""
    evts = await _collect([
        {"message": {"role": "assistant", "content": "や"}, "done": False},
        {"message": {"role": "assistant", "content": "あ"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True,
         "done_reason": "stop", "eval_count": 5},
    ])
    assert [e for e in evts if e["type"] == "delta"] == [
        {"type": "delta", "content": "や"}, {"type": "delta", "content": "あ"}]
    done = [e for e in evts if e["type"] == "done"][0]
    assert done["finish_reason"] == "stop" and done["eval_count"] == 5


async def test_thinking_is_dropped():
    """message.thinking は delta として出さない。message.content のみを emit。"""
    evts = await _collect([
        {"message": {"role": "assistant", "thinking": "内心...", "content": ""}, "done": False},
        {"message": {"role": "assistant", "content": "答え"}, "done": False},
        {"done": True},
    ])
    deltas = [e["content"] for e in evts if e["type"] == "delta"]
    assert deltas == ["答え"]  # thinking は出ない


async def test_error_field():
    """error フィールドが present なら error イベントを emit して return。"""
    evts = await _collect([{"error": "model not found"}])
    assert evts == [{"type": "error", "message": "model not found"}]


async def test_done_without_done_reason_fallback_to_stop():
    """C5: done=true だが done_reason がない場合、finish_reason='stop' にフォール。eval_count は含める。"""
    evts = await _collect([{"done": True, "eval_count": 5}])
    assert len(evts) == 1
    assert evts[0] == {"type": "done", "finish_reason": "stop", "eval_count": 5}

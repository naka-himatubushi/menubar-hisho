"""OpenAI chunk 構造と SSE 整形・[DONE]・error frame を検証。"""
import json
from hisho_core.sse import sse, chunk, DONE, error_frame


def test_sse_frame_format():
    line = sse({"a": 1})
    assert line == 'data: {"a": 1}\n\n'


def test_done_sentinel():
    assert DONE == "data: [DONE]\n\n"


def test_chunk_shape_first_delta():
    c = chunk("id1", "m", 100, delta={"role": "assistant"}, finish_reason=None)
    assert c["object"] == "chat.completion.chunk"
    assert c["id"] == "id1" and c["model"] == "m" and c["created"] == 100
    assert c["choices"][0]["delta"] == {"role": "assistant"}
    assert c["choices"][0]["finish_reason"] is None


def test_chunk_final_has_finish_reason():
    c = chunk("id1", "m", 100, delta={}, finish_reason="stop")
    assert c["choices"][0]["finish_reason"] == "stop"


def test_error_frame():
    """C4 override: error_frame must have OpenAI-shaped error with param and code."""
    e = error_frame("boom")
    assert e["error"]["message"] == "boom"
    assert e["error"]["type"] == "hisho_error"
    assert e["error"]["param"] is None and e["error"]["code"] is None

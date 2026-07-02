"""役割: server のツールループ (キーワードゲート・実行・決定的報告・索引スキップ) の統合テスト。"""
import json
from fastapi.testclient import TestClient
from hisho_core.server import create_app
from hisho_core.config import load_config
from hisho_core.store import Store


def _sse_text(resp_bytes):
    return resp_bytes.decode("utf-8", "replace")


def _make_chat_fn(script):
    """script: 呼び出し回数ごとに返すイベント列のリスト。tools 受領を記録。"""
    calls = {"tools_seen": [], "n": 0}

    async def chat_fn(messages, *, model, ollama_host, num_ctx, keep_alive, think=False, tools=None):
        calls["tools_seen"].append(tools)
        seq = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for e in seq:
            yield e

    return chat_fn, calls


def test_no_forget_keyword_passes_no_tools(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn([[{"type": "delta", "content": "はい"}, {"type": "done", "finish_reason": "stop"}]])
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s1", "messages": [{"role": "user", "content": "今日の天気は"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert calls["tools_seen"][0] is None  # 忘却語なし → tools 渡さない


def test_forget_keyword_runs_tool_and_appends_deterministic_line(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    if not store.rag_enabled:
        return
    # 1回目: tool_call を返す / 2回目: 最終回答
    script = [
        [{"type": "tool_call", "id": "c1", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
        [{"type": "delta", "content": "承知しました"}, {"type": "done", "finish_reason": "stop"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    # forget_memories を件数固定の fake に差し替え (embed/DB に依存させない)
    import hisho_core.tools as toolsmod

    async def fake_forget(args, **kw):
        return {"count": 2, "matched": 2, "truncated": False, "items": ["猫A", "猫B"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s2", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        body = _sse_text(r.content)
    assert calls["tools_seen"][0] is not None      # 1回目は tools 渡す
    assert "[記憶を 2件 忘れました]" in body        # 決定的報告
    assert "承知しました" in body                   # モデルの最終回答も出る


# --- テスト用ヘルパ ---
async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True

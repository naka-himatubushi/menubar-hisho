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


def test_external_source_never_gets_tools_even_with_keyword(tmp_path):
    """破壊ツールは popover 経路限定。external から忘却語で来ても tools を渡さない (安全)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn([[{"type": "delta", "content": "ok"}, {"type": "done", "finish_reason": "stop"}]])
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "sx", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "external"})
        assert r.status_code == 200
    assert calls["tools_seen"][0] is None  # external → 忘却語があっても tools なし


def test_forget_keyword_runs_tool_and_appends_deterministic_line(tmp_path):
    # fake_forget を注入するので RAG 依存なし → rag_enabled ガードは不要 (M3 assert を常に走らせる)
    store = Store(str(tmp_path / "t.db"))
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


def test_tool_call_every_round_terminates_with_stop(tmp_path):
    """モデルが毎回 tool_call を返し続ける (最終回答を出さない) 場合でも
    MAX_TOOL_ITERS で打ち切られ、finish_reason は "tool_calls" のまま
    クライアントへ漏れず "stop" に正規化される (Important #1)。"""
    store = Store(str(tmp_path / "t.db"))  # fake_forget 注入で RAG 不要
    # 常に forget_memories を呼び続けるスクリプト (毎ラウンド同じ)
    round_events = [
        {"type": "tool_call", "id": "cX", "name": "forget_memories", "arguments": {"query": "猫"}},
        {"type": "done", "finish_reason": "tool_calls"},
    ]
    script = [round_events, round_events, round_events, round_events]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def fake_forget(args, **kw):
        return {"count": 1, "matched": 1, "truncated": False, "items": ["猫A"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s3", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
        body = _sse_text(r.content)
    # ループは MAX_TOOL_ITERS 回で打ち切られる (無限ループしない)
    assert calls["n"] <= 3
    # 最終フレームの finish_reason は "stop" (tool_calls のまま漏れない)
    frames = [json.loads(line[len("data: "):]) for line in body.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    finish_reasons = [f["choices"][0]["finish_reason"] for f in frames
                      if f["choices"][0]["finish_reason"] is not None]
    assert finish_reasons, "finish_reason を含むフレームが無い"
    assert finish_reasons[-1] == "stop"
    assert "tool_calls" not in finish_reasons


def test_unknown_tool_breaks_cleanly_with_stop(tmp_path):
    """モデルが未登録のツール名を呼んだ場合、サーバはエラーにせず
    ループを break し、finish_reason を "stop" に正規化して完了する (Important #1, #2)。"""
    store = Store(str(tmp_path / "t.db"))
    script = [
        [{"type": "tool_call", "id": "cU", "name": "nonexistent_tool", "arguments": {}},
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s4", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
        body = _sse_text(r.content)
    assert "error" not in body.lower() or "hisho_error" not in body  # クライアントへエラーは伝播しない
    frames = [json.loads(line[len("data: "):]) for line in body.splitlines()
              if line.startswith("data: ") and line != "data: [DONE]"]
    finish_reasons = [f["choices"][0]["finish_reason"] for f in frames
                      if f["choices"][0]["finish_reason"] is not None]
    assert finish_reasons and finish_reasons[-1] == "stop"


def test_tool_raises_is_logged_and_reported_as_error(tmp_path, caplog):
    """ツール実行中に例外が発生した場合、サーバ側で exc_info 付きログを残し、
    クライアントには error_frame を送って完了する (Important #3)。"""
    import logging
    store = Store(str(tmp_path / "t.db"))
    script = [
        [{"type": "tool_call", "id": "cE", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def raising_forget(args, **kw):
        raise RuntimeError("boom: db error")

    app.state.tool_registry = {"forget_memories": raising_forget}
    with caplog.at_level(logging.WARNING, logger="hisho"):
        with TestClient(app) as c:
            r = c.post("/v1/chat/completions", json={"session_id": "s5", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                       headers={"X-Hisho-Source": "popover"})
            assert r.status_code == 200
            body = _sse_text(r.content)
    assert "hisho_error" in body  # クライアントには error_frame が届く
    # サーバ側ログに例外が exc_info 付きで記録される
    matching = [rec for rec in caplog.records if "forget_memories" in rec.message and rec.exc_info]
    assert matching, "tool 例外が exc_info 付きでログされていない"


# --- テスト用ヘルパ ---
async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True

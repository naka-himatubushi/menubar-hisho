"""役割: server のツールループ (キーワードゲート・実行・決定的報告・索引スキップ) の統合テスト。"""
import json
import anyio
import httpx
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


def test_deterministic_fallback_forgets_when_model_hallucinates(tmp_path):
    """安全性の要: モデルが tool を呼ばず「消しました」と幻覚しても、サーバが決定的に
    forget を実行する (実測: qwen3.6 は memories 無しでも ~25% 幻覚し素通りする)。"""
    store = Store(str(tmp_path / "t.db"))
    # モデルは tool_call を出さず content だけ返す (= 幻覚。実削除しないのに「消した」)
    chat_fn, calls = _make_chat_fn([[{"type": "delta", "content": "削除しました。件数は0件です。"},
                                     {"type": "done", "finish_reason": "stop"}]])
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {"q": None}

    async def fake_forget(args, **kw):
        called["q"] = args.get("query")
        return {"count": 1, "matched": 1, "truncated": False, "items": ["ハムスターのハム太"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "fb", "messages": [{"role": "user", "content": "ハムスターのこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        body = _sse_text(r.content)
    assert called["q"] == "ハムスター"          # フォールバックが query を抽出して forget を実行
    assert "[記憶を 1件 忘れました]" in body      # 決定的報告 = モデルの幻覚「0件」でなく実際の1件


def test_negation_does_not_trigger_forget(tmp_path):
    """「忘れないで」(否定) は imperative ゲートに掛からず tools を渡さない = 誤削除しない。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn([[{"type": "delta", "content": "承知しました"}, {"type": "done", "finish_reason": "stop"}]])
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "neg", "messages": [{"role": "user", "content": "この予定は忘れないで"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert calls["tools_seen"][0] is None  # 「忘れないで」→ tools なし (誤削除防止)


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


async def test_forget_success_skips_indexing_and_reports_count(tmp_path, monkeypatch):
    """H1 正常系: forget が成功した往復は索引されず、決定的な件数行が出る。"""
    from hisho_core import server as server_mod

    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    script = [
        [{"type": "tool_call", "id": "c1", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
        [{"type": "delta", "content": "承知しました"}, {"type": "done", "finish_reason": "stop"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def fake_forget(args, **kw):
        return {"count": 2, "matched": 2, "truncated": False, "items": ["猫A", "猫B"]}

    app.state.tool_registry = {"forget_memories": fake_forget}

    indexed = []

    async def spy_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    async def fake_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        return []

    monkeypatch.setattr(server_mod.rag, "index_turn", spy_index)
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "s6", "messages": [{"role": "user", "content": "猫のこと忘れて"}]})
        body = r.text
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert "[記憶を 2件 忘れました]" in body
    assert indexed == []  # 忘却往復は索引されない


async def test_forget_embed_failure_still_skips_indexing(tmp_path, monkeypatch):
    """H1 回帰ガード (Critical): forget が {"error": "embed_failed"} を返しても
    (成功していなくても) 索引してはいけない。ここが直前まで抜けていたバグ。"""
    from hisho_core import server as server_mod

    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    script = [
        [{"type": "tool_call", "id": "c1", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
        [{"type": "delta", "content": "すみません、うまくいきませんでした"}, {"type": "done", "finish_reason": "stop"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def failing_forget(args, **kw):
        return {"error": "embed_failed", "message": "今 記憶を整理できません"}

    app.state.tool_registry = {"forget_memories": failing_forget}

    indexed = []

    async def spy_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    async def fake_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        return []

    monkeypatch.setattr(server_mod.rag, "index_turn", spy_index)
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "s7", "messages": [{"role": "user", "content": "猫のこと忘れて"}]})
        assert r.status_code == 200
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert indexed == []  # 失敗しても「猫忘れて」往復を再索引してはいけない (自己汚染バグの再現防止)


def test_forget_truncated_result_reports_real_total(tmp_path):
    """silent cap 禁止: truncated な結果はモデルの narrative に頼らず、
    決定的な行が実際の該当件数と上限到達を明示する。"""
    store = Store(str(tmp_path / "t.db"))
    script = [
        [{"type": "tool_call", "id": "c1", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
        [{"type": "delta", "content": "承知しました"}, {"type": "done", "finish_reason": "stop"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def fake_forget(args, **kw):
        return {"count": 15, "matched": 23, "truncated": True, "items": ["猫A"] * 15}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s8", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        body = _sse_text(r.content)
    assert "[記憶を 15件 忘れました (該当 23件中、上限まで)]" in body


# --- テスト用ヘルパ ---
async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True

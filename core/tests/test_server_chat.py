"""チャット: SSE 出力・全ターン記録・popover合成・切断時partial を、fake chat_fn で検証。"""
import httpx, pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app

def _make(tmp_path, chat_fn):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    async def probe():
        return {"reachable": True, "version": "x", "model_present": True,
                "model_loaded": True, "models": [cfg.chat_model]}
    return store, create_app(store, cfg, chat_fn=chat_fn, probe_fn=probe)

async def _fake_ok(messages, **kw):
    for ch in ["こん", "にちは"]:
        yield {"type": "delta", "content": ch}
    yield {"type": "done", "finish_reason": "stop", "eval_count": 2}

async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

async def test_stream_and_logging(tmp_path):
    store, app = _make(tmp_path, _fake_ok)
    async with await _client(app) as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"messages": [{"role": "user", "content": "やあ"}],
                               "session_id": "s1", "stream": True})
        body = r.text
        assert "こん" in body and "にちは" in body and "data: [DONE]" in body
    rows = store.conn.execute(
        "SELECT role, content, status, json_extract(meta,'$.source') AS src "
        "FROM turns WHERE session_id='s1' ORDER BY seq").fetchall()
    assert [(x["role"], x["content"], x["status"]) for x in rows] == [
        ("user", "やあ", "complete"), ("assistant", "こんにちは", "complete")]
    assert rows[0]["src"] == "popover"

async def test_external_source_default(tmp_path):
    store, app = _make(tmp_path, _fake_ok)
    async with await _client(app) as c:
        await c.post("/v1/chat/completions",
                     json={"messages": [{"role": "user", "content": "hi"}], "session_id": "e1"})
    src = store.conn.execute(
        "SELECT json_extract(meta,'$.source') AS s FROM turns "
        "WHERE session_id='e1' AND role='user'").fetchone()["s"]
    assert src == "external"

async def test_error_event_records_error_status(tmp_path):
    async def _fake_err(messages, **kw):
        yield {"type": "delta", "content": "途中"}
        yield {"type": "error", "message": "boom"}
    store, app = _make(tmp_path, _fake_err)
    async with await _client(app) as c:
        r = await c.post("/v1/chat/completions",
                         json={"messages": [{"role": "user", "content": "x"}], "session_id": "p1"})
        assert '"error"' in r.text
    a = store.conn.execute(
        "SELECT content, status FROM turns WHERE session_id='p1' AND role='assistant'").fetchone()
    assert a["content"] == "途中" and a["status"] == "error"


# 注: C1 のクライアント切断 → status='partial' 経路は自動テスト化していない。
# httpx ASGITransport は実ソケットを持たないため、クライアント側の cancel
# (asyncio.wait_for タイムアウト / stream の途中 break) が
# サーバ側 gen() の finally へ確実に伝播しないことを実測で確認済み
# (前者=finally 未実行で status が 'streaming' のまま / 後者=サーバが完走し
# 'complete' になる)。よって flaky/誤解を招くテストは載せず、この経路は
# core/SMOKE.md の「⚠️ C1: クライアント切断時の部分ログ記録確認」で手動検証する。

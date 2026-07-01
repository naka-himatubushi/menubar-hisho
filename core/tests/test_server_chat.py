"""チャット: SSE 出力・全ターン記録・popover合成・切断時partial を、fake chat_fn で検証。"""
import asyncio
import json as _json
import httpx
from starlette.requests import Request
from fastapi.routing import APIRoute
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


def _build_request(app, body: dict) -> Request:
    """テスト用 Starlette Request を構築する。"""
    body_bytes = _json.dumps(body).encode()
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/chat/completions",
        "query_string": b"",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body_bytes)).encode()),
        ],
        "app": app,
    }

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    return Request(scope, receive=receive)


def _find_chat_endpoint(app):
    """FastAPI app から /v1/chat/completions のエンドポイント関数を取得する。"""
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path == "/v1/chat/completions":
            return route.endpoint
    raise RuntimeError("/v1/chat/completions route not found")

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


async def test_cancel_records_partial(tmp_path):
    """gen() を途中で aclose() すると finally が確実に走り status='partial' で記録される (F1 検証)。

    アプローチ: StreamingResponse.body_iterator (= gen() async generator) を直接駆動し、
    role チャンクと content チャンクを消費した後に aclose() を呼ぶ。
    これにより GeneratorExit が gen() の内部 yield 点に投入され、finally ブロックが実行される。
    httpx ASGITransport 経由のクライアント切断は finally への伝播が保証されないため使わない。
    """
    async def _slow(messages, **kw):
        yield {"type": "delta", "content": "途中まで"}
        await asyncio.sleep(10)   # 完了しない
        yield {"type": "done", "finish_reason": "stop", "eval_count": 1}

    store, app = _make(tmp_path, _slow)
    request = _build_request(app, {"messages": [{"role": "user", "content": "hi"}], "session_id": "c1"})
    chat_ep = _find_chat_endpoint(app)

    response = await chat_ep(request)
    it = response.body_iterator

    # role チャンク消費 → gen() が再開して delta を処理し content チャンク yield で停止
    await it.__anext__()   # role chunk: delta={"role": "assistant"}
    await it.__anext__()   # content chunk: delta={"content": "途中まで"}  (acc 末追加済み)

    # aclose() が GeneratorExit を yield 点に投入 → finally が実行される
    await it.aclose()
    # finally 内の anyio.CancelScope(shield=True) + to_thread が完了するまで待つ
    await asyncio.sleep(0.05)

    a = store.conn.execute(
        "SELECT content, status FROM turns WHERE session_id='c1' AND role='assistant'"
    ).fetchone()
    assert a["status"] == "partial", f"expected partial, got {a['status']}"
    assert a["content"] == "途中まで", f"expected '途中まで', got {a['content']!r}"


async def test_popover_dedup_messages(tmp_path):
    """popover source では現在の user ターンが messages に2回含まれず、先頭が system message である (T-dedup 検証)。"""
    captured: dict = {}

    async def _capture(messages, **kw):
        captured["messages"] = list(messages)
        yield {"type": "done", "finish_reason": "stop", "eval_count": 1}

    store, app = _make(tmp_path, _capture)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        await c.post(
            "/v1/chat/completions",
            headers={"X-Hisho-Source": "popover"},
            json={"messages": [{"role": "user", "content": "こんにちは"}], "session_id": "d1"},
        )

    msgs = captured["messages"]
    assert msgs, "chat_fn was not called"
    # persona system message が先頭
    assert msgs[0]["role"] == "system", f"first message role={msgs[0]['role']!r}, expected 'system'"
    # 現在の user ターンが重複していない
    user_occurrences = [m for m in msgs if m["role"] == "user" and m["content"] == "こんにちは"]
    assert len(user_occurrences) == 1, (
        f"user turn appeared {len(user_occurrences)} times; recent[:-1] dedup failed. messages={msgs}"
    )

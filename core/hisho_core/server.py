"""FastAPI アプリ組立。/healthz, /v1/models, /v1/chat/completions を提供。"""
from __future__ import annotations
import asyncio
import logging
import time
import uuid
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from . import context
from . import llm
from .config import Config
from .store import Store
from .sse import sse, chunk, DONE, error_frame

logger = logging.getLogger("hisho")


def _now_ms() -> int:
    return int(time.time() * 1000)


async def _default_probe(config: Config) -> dict:
    """Ollama に /api/version, /api/tags, /api/ps を叩いて健康状態を取得。例外時は reachable=False。"""
    import httpx
    out = {
        "reachable": False,
        "version": None,
        "model_present": False,
        "model_loaded": False,
        "models": [],
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            # /api/version
            v = await c.get(f"{config.ollama_host}/api/version")
            out["reachable"] = v.status_code == 200
            out["version"] = v.json().get("version") if v.status_code == 200 else None

            # /api/tags
            tags = await c.get(f"{config.ollama_host}/api/tags")
            names = (
                [m["name"] for m in tags.json().get("models", [])]
                if tags.status_code == 200
                else []
            )
            out["models"] = names
            out["model_present"] = any(
                n.split(":")[0] == config.chat_model.split(":")[0]
                or n == config.chat_model
                for n in names
            )

            # /api/ps
            ps = await c.get(f"{config.ollama_host}/api/ps")
            loaded = (
                [m["name"] for m in ps.json().get("models", [])]
                if ps.status_code == 200
                else []
            )
            out["model_loaded"] = config.chat_model in loaded
    except Exception:
        pass
    return out


def create_app(
    store: Store, config: Config, *, chat_fn=None, probe_fn=None
) -> FastAPI:
    """FastAPI アプリを組立。

    Args:
        store: Store インスタンス
        config: Config インスタンス
        chat_fn: LLM チャット関数 (既定: llm.chat_stream)
        probe_fn: 健康状態プローブ関数 (既定: _default_probe(config))

    Returns:
        FastAPI アプリ
    """
    app = FastAPI()
    app.state.store = store
    app.state.config = config
    app.state.chat_fn = chat_fn or llm.chat_stream
    app.state.probe_fn = probe_fn or (lambda: _default_probe(config))
    app.state._probe_cache = {"t": 0.0, "v": None}
    app.state.write_lock = asyncio.Lock()

    async def _probe() -> dict:
        """probe_fn の結果を ~3秒キャッシュ。"""
        cache = app.state._probe_cache
        if (
            cache["v"] is not None
            and (time.time() - cache["t"]) < 3.0
        ):
            return cache["v"]
        v = await app.state.probe_fn()
        cache["t"], cache["v"] = time.time(), v
        return v

    @app.get("/healthz")
    async def healthz():
        """層状の健康状態。core=常に true, ollama={reachable, version}, model={present, loaded}。"""
        p = await _probe()
        return {
            "core": True,
            "ollama": {"reachable": p["reachable"], "version": p["version"]},
            "model": {"present": p["model_present"], "loaded": p["model_loaded"]},
        }

    @app.get("/v1/models")
    async def models():
        """モデル一覧。probe の models または config.chat_model にフォールバック。"""
        p = await _probe()
        names = p["models"] or [config.chat_model]
        return {"object": "list", "data": [{"id": n, "object": "model"} for n in names]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        """SSE ストリーミングチャット。user ターンを記録→LLM 呼出→assistant ターン確定(finally 安全)。"""
        cfg = app.state.config
        store = app.state.store
        body = await request.json()
        source = request.headers.get("X-Hisho-Source", "external")
        model = body.get("model") or cfg.chat_model
        session_id = body.get("session_id") or f"sess-{uuid.uuid4().hex[:12]}"
        msgs_in = body.get("messages", [])
        user_message = msgs_in[-1]["content"] if msgs_in else ""

        now = _now_ms()
        async with app.state.write_lock:
            await anyio.to_thread.run_sync(store.get_or_create_session, session_id, now)
            await anyio.to_thread.run_sync(store.append_user_turn, session_id, user_message, now, source)

        if source == "popover":
            recent = await anyio.to_thread.run_sync(store.recent_turns, session_id, cfg.history_replay_turns)
            recent_wo_last = recent[:-1] if recent and recent[-1]["role"] == "user" else recent
            messages = context.build_messages(recent_wo_last, user_message, cfg.num_ctx, cfg.response_reserve)
        else:
            messages = msgs_in

        async with app.state.write_lock:
            assistant_id = await anyio.to_thread.run_sync(store.add_assistant_placeholder, session_id, model, _now_ms())

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        async def gen():
            acc: list[str] = []
            status = "partial"          # default partial; only success promotes to complete
            finish = "stop"
            try:
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={"role": "assistant"}, finish_reason=None))
                async for evt in app.state.chat_fn(messages, model=model, ollama_host=cfg.ollama_host,
                                                   num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False):
                    if evt["type"] == "delta":
                        acc.append(evt["content"])
                        yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": evt["content"]}, finish_reason=None))
                    elif evt["type"] == "error":
                        status = "error"
                        yield sse(error_frame(evt["message"]))
                        return
                    elif evt["type"] == "done":
                        finish = evt.get("finish_reason", "stop")
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason=finish))
                yield DONE
                status = "complete"
            except asyncio.CancelledError:   # BaseException-derived: NOT caught by except Exception
                raise
            except Exception as ex:
                status = "error"
                try:
                    yield sse(error_frame(str(ex)))
                except Exception:
                    logger.warning("failed to send error frame to client", exc_info=True)
            finally:
                async with app.state.write_lock:
                    await anyio.to_thread.run_sync(store.finalize_turn, assistant_id, "".join(acc), None, status, _now_ms())
                    await anyio.to_thread.run_sync(store.touch_session, session_id, _now_ms())

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app

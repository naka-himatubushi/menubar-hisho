"""FastAPI アプリ組立。/healthz, /v1/models, /v1/chat/completions を提供。"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
import uuid
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from . import context
from . import llm
from . import rag
from . import tools as tools_module
from .config import Config
from .store import Store
from .sse import sse, chunk, DONE, error_frame

logger = logging.getLogger("hisho")

MAX_TOOL_ITERS = 3


def _now_ms() -> int:
    return int(time.time() * 1000)


_FORGET_TAIL = re.compile(r"(のこと|について|の情報|に関する|を|は|が|も|、|。|\s)+$")


def _forget_query(user_message: str) -> str:
    """決定的フォールバック用に user 発話から忘却対象語を抽出する。
    例: 「ハムスターのこと忘れて」→「ハムスター」。抽出できなければ全文を返す。"""
    head = re.split(r"忘れ|消し|消去|削除|覚えなくて", user_message)[0]
    return _FORGET_TAIL.sub("", head).strip() or user_message


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
        logger.debug("ollama probe failed", exc_info=True)
    return out


async def warmup_when_ready(probe, warmup, *, attempts=120, interval=2.0,
                            sleep=asyncio.sleep) -> bool:
    """ollama が reachable になり warm-up が成功するまで繰り返す。戻り値=成功したか。"""
    for _ in range(attempts):
        try:
            p = await probe()
            if p.get("reachable") and await warmup():
                return True
        except Exception:
            logger.debug("warmup attempt failed", exc_info=True)
        await sleep(interval)
    return False


def create_app(
    store: Store, config: Config, *, chat_fn=None, probe_fn=None,
    warmup_fn=None, unload_fn=None,
) -> FastAPI:
    """FastAPI アプリを組立。

    Args:
        store: Store インスタンス
        config: Config インスタンス
        chat_fn: LLM チャット関数 (既定: llm.chat_stream)
        probe_fn: 健康状態プローブ関数 (既定: _default_probe(config))
        warmup_fn: warm-up 関数 (既定: llm.warmup with config 値)
        unload_fn: アンロード関数 (既定: llm.unload with config 値)

    Returns:
        FastAPI アプリ
    """
    import contextlib
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(
            warmup_when_ready(app.state.probe_fn, app.state.warmup_fn))
        backfill_task = asyncio.create_task(
            rag.backfill(app.state.store, app.state.write_lock, config=config))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        backfill_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await backfill_task
        try:
            await asyncio.wait_for(app.state.unload_fn(), timeout=2.0)
        except Exception:
            logger.debug("unload on shutdown failed", exc_info=True)

    app = FastAPI(lifespan=_lifespan)
    app.state.store = store
    app.state.config = config
    app.state.chat_fn = chat_fn or llm.chat_stream
    app.state.probe_fn = probe_fn or (lambda: _default_probe(config))
    app.state._probe_cache = {"t": 0.0, "v": None}
    app.state.write_lock = asyncio.Lock()
    app.state.tool_registry = tools_module.REGISTRY
    # imperative 形に限定して否定 (「忘れないで」「消さないで」) を除外。
    # フォールバックが誤って削除しないための一次ゲート。
    app.state.forget_intent = re.compile(r"忘れて|消して|消去|削除|覚えなくて")
    app.state.warmup_fn = warmup_fn or (lambda: llm.warmup(
        model=config.chat_model, ollama_host=config.ollama_host,
        num_ctx=config.num_ctx, keep_alive=config.keep_alive))
    app.state.unload_fn = unload_fn or (lambda: llm.unload(
        model=config.chat_model, ollama_host=config.ollama_host))

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
            "model": {"present": p["model_present"], "loaded": p["model_loaded"],
                      "name": config.chat_model},
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
        user_message = (msgs_in[-1].get("content") or "") if msgs_in else ""
        if not isinstance(user_message, str):
            user_message = str(user_message)

        now = _now_ms()
        async with app.state.write_lock:
            await anyio.to_thread.run_sync(store.get_or_create_session, session_id, now)
            user_turn_id = await anyio.to_thread.run_sync(store.append_user_turn, session_id, user_message, now, source)

        is_forget = source == "popover" and bool(app.state.forget_intent.search(user_message))
        if source == "popover":
            # 忘却要求ターンは過去メモを注入しない: 削除対象の事実が context に居ると
            # モデルが forget ツールを呼ばず「消しました」と幻覚する (実測: qwen3.6)。
            # ツールは DB を自前検索するので context のメモは不要。
            memories = [] if is_forget else await rag.retrieve(
                store, user_message, config=cfg, exclude_session_id=session_id)
            recent = await anyio.to_thread.run_sync(store.recent_turns, session_id, cfg.history_replay_turns)
            recent_wo_last = recent[:-1] if recent and recent[-1]["role"] == "user" else recent
            messages = context.build_messages(recent_wo_last, user_message,
                                              cfg.num_ctx, cfg.response_reserve,
                                              memories=memories)
        else:
            messages = msgs_in

        use_tools = tools_module.TOOL_SPECS if is_forget else None

        async with app.state.write_lock:
            assistant_id = await anyio.to_thread.run_sync(store.add_assistant_placeholder, session_id, model, _now_ms(), source)

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        async def gen():
            acc: list[str] = []
            status = "partial"          # default partial; only success promotes to complete
            finish = "stop"
            forget_fired = False
            forget_count = 0
            forget_result: dict | None = None
            tool_used_forget = False   # H1: forget を「試みた」かどうか(成否・例外を問わない)
            convo = list(messages)      # tool ラウンドで伸ばす作業用コピー
            tools_for_round = use_tools

            async def _index_pair():
                """user + assistant ターンを索引(失敗しても無害)。"""
                try:
                    await rag.index_turn(store, app.state.write_lock, user_turn_id,
                                         session_id, user_message, config=cfg)
                    text = "".join(acc)
                    if text:
                        await rag.index_turn(store, app.state.write_lock, assistant_id,
                                             session_id, text, config=cfg)
                except Exception:
                    logger.warning("index after chat failed", exc_info=True)

            try:
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={"role": "assistant"}, finish_reason=None))
                for _ in range(MAX_TOOL_ITERS):
                    pending_tool = None
                    async for evt in app.state.chat_fn(
                            convo, model=model, ollama_host=cfg.ollama_host,
                            num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False,
                            tools=tools_for_round):
                        if evt["type"] == "delta":
                            acc.append(evt["content"])
                            yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": evt["content"]}, finish_reason=None))
                        elif evt["type"] == "tool_call":
                            pending_tool = evt
                        elif evt["type"] == "error":
                            status = "error"
                            yield sse(error_frame(evt["message"]))
                            return
                        elif evt["type"] == "done":
                            finish = evt.get("finish_reason", "stop")
                    if pending_tool is None:
                        break  # ツール呼び出しなし = 通常の最終回答で完了
                    fn = app.state.tool_registry.get(pending_tool["name"])
                    if fn is None:
                        logger.warning("unknown tool called: %s", pending_tool.get("name"))
                        break
                    if pending_tool["name"] == "forget_memories":
                        # H1: 試行した時点でフラグを立てる。成否・例外に関わらず
                        # finally での索引スキップ判定はこのフラグだけを見る。
                        tool_used_forget = True
                    try:
                        result = await fn(pending_tool.get("arguments") or {},
                                          store=store, config=cfg, write_lock=app.state.write_lock)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("tool %s failed", pending_tool["name"], exc_info=True)
                        status = "error"
                        yield sse(error_frame(f"tool {pending_tool['name']} failed"))
                        return
                    if pending_tool["name"] == "forget_memories" and "error" not in result:
                        forget_fired = True
                        forget_count = result.get("count", 0)
                        forget_result = result
                    convo = convo + [
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": pending_tool.get("id"), "type": "function",
                             "function": {"name": pending_tool["name"],
                                          "arguments": pending_tool.get("arguments") or {}}}]},
                        {"role": "tool", "content": json.dumps(result, ensure_ascii=False)},
                    ]
                    tools_for_round = None  # 2周目以降はツール無し (無限ループ防止の一助)
                # 決定的フォールバック (安全性の要): 明示的忘却意図なのにモデルが forget を
                # 呼ばなかった場合 (qwen3.6 は memories 無しでも ~25% 幻覚で素通りする) →
                # サーバが確実に forget を実行し、「消した」という嘘が実削除なしで通るのを防ぐ。
                if use_tools and not tool_used_forget:
                    tool_used_forget = True   # H1: この往復も索引しない (試行した)
                    try:
                        fb = await app.state.tool_registry["forget_memories"](
                            {"query": _forget_query(user_message)},
                            store=store, config=cfg, write_lock=app.state.write_lock)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("fallback forget failed", exc_info=True)
                        fb = None
                    if isinstance(fb, dict) and "error" not in fb:
                        forget_fired = True
                        forget_count = fb.get("count", 0)
                        forget_result = fb
                if finish == "tool_calls":
                    # ループが tool_call のまま終了した (MAX_TOOL_ITERS 到達 or 未知ツールで break) →
                    # クライアントには非ツールの終端理由を返す (H-Task5-1)
                    finish = "stop"
                if forget_fired:
                    # silent cap 禁止 (spec §5): truncated の場合はモデルの narrative に頼らず
                    # 決定的な行で実際の該当件数と上限到達を明示する。
                    if forget_result and forget_result.get("truncated"):
                        matched = forget_result.get("matched", forget_count)
                        line = f"\n\n[記憶を {forget_count}件 忘れました (該当 {matched}件中、上限まで)]"
                    else:
                        line = f"\n\n[記憶を {forget_count}件 忘れました]"
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
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
                with anyio.CancelScope(shield=True):
                    async with app.state.write_lock:
                        await anyio.to_thread.run_sync(store.finalize_turn, assistant_id, "".join(acc), None, status, _now_ms())
                        await anyio.to_thread.run_sync(store.touch_session, session_id, _now_ms())
                if source == "popover" and not tool_used_forget:   # H1: 忘却を試みた往復は(成否問わず)索引しない
                    asyncio.create_task(_index_pair())

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/history")
    async def history(session_id: str | None = None):
        """セッション一覧またはセッション内ターン一覧を取得(読み取り専用)。"""
        store = app.state.store
        if session_id is None:
            return {"sessions": await anyio.to_thread.run_sync(store.list_sessions, 100)}
        return {"session_id": session_id, "turns": await anyio.to_thread.run_sync(store.recent_turns, session_id, 500)}

    return app

"""FastAPI アプリ組立。/healthz, /v1/models を提供。"""
from __future__ import annotations
import time
from fastapi import FastAPI
from . import llm
from .config import Config
from .store import Store


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

    return app

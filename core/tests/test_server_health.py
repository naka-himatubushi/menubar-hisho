"""/healthz 層状ボディと /v1/models を、probe を注入して検証(ollama不要)。"""
import httpx
import pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app


def _app(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    async def probe():
        return {"reachable": True, "version": "0.31.1",
                "model_present": True, "model_loaded": False,
                "models": ["qwen3.6:35b-a3b"]}
    return create_app(store, cfg, chat_fn=None, probe_fn=probe)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


@pytest.mark.asyncio
async def test_healthz_layered(tmp_path):
    app = _app(tmp_path)
    async with await _client(app) as c:
        r = await c.get("/healthz")
        j = r.json()
        assert j["core"] is True
        assert j["ollama"]["reachable"] is True and j["ollama"]["version"] == "0.31.1"
        assert j["model"]["present"] is True and j["model"]["loaded"] is False


@pytest.mark.asyncio
async def test_models_list(tmp_path):
    app = _app(tmp_path)
    async with await _client(app) as c:
        r = await c.get("/v1/models")
        ids = [m["id"] for m in r.json()["data"]]
        assert "qwen3.6:35b-a3b" in ids

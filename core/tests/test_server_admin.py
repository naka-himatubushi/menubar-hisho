"""役割: POST /v1/admin/model/unload と /v1/admin/model/load エンドポイントの検証。
unload_fn / warmup_fn を注入して、ルートが正しく呼び出し・レスポンスを返すことを確認。"""
import pytest
import httpx
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app


def _make_app(tmp_path, *, warmup_ok: bool = True, unload_ok: bool = True):
    """テスト用アプリ。warmup_fn / unload_fn を fake で差し替え、呼び出し回数を記録する。"""
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    calls = {"warmup": 0, "unload": 0}

    async def probe():
        return {"reachable": True, "version": "0.31", "model_present": True,
                "model_loaded": False, "models": [cfg.chat_model]}

    async def warmup_fn():
        calls["warmup"] += 1
        return warmup_ok

    async def unload_fn():
        calls["unload"] += 1
        return unload_ok

    app = create_app(store, cfg, probe_fn=probe, warmup_fn=warmup_fn, unload_fn=unload_fn)
    return app, calls


@pytest.mark.asyncio
async def test_unload_endpoint_calls_unload_fn_and_returns_true(tmp_path):
    app, calls = _make_app(tmp_path, unload_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/v1/admin/model/unload")
    assert r.status_code == 200
    assert r.json() == {"unloaded": True}
    assert calls["unload"] == 1


@pytest.mark.asyncio
async def test_load_endpoint_calls_warmup_fn_and_returns_true(tmp_path):
    app, calls = _make_app(tmp_path, warmup_ok=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/v1/admin/model/load")
    assert r.status_code == 200
    assert r.json() == {"loaded": True}
    assert calls["warmup"] == 1


@pytest.mark.asyncio
async def test_unload_endpoint_returns_false_when_unload_fails(tmp_path):
    app, calls = _make_app(tmp_path, unload_ok=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/v1/admin/model/unload")
    assert r.status_code == 200
    assert r.json() == {"unloaded": False}
    assert calls["unload"] == 1  # ハードコードでなく実際に unload_fn を呼んでいる


@pytest.mark.asyncio
async def test_load_endpoint_returns_false_when_warmup_fails(tmp_path):
    app, calls = _make_app(tmp_path, warmup_ok=False)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    ) as c:
        r = await c.post("/v1/admin/model/load")
    assert r.status_code == 200
    assert r.json() == {"loaded": False}
    assert calls["warmup"] == 1  # ハードコードでなく実際に warmup_fn を呼んでいる

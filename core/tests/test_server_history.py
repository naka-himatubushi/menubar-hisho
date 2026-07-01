"""/history: セッション一覧と1セッションのターン列を検証。"""
import httpx
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app


def _make(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    store.get_or_create_session("s1", 100)
    store.append_user_turn("s1", "Q1", 100, source="popover")
    aid = store.add_assistant_placeholder("s1", "m", 101)
    store.finalize_turn(aid, "A1", 2, "complete", 102)
    store.touch_session("s1", 102)
    async def probe():
        return {"reachable": True, "version": "x", "model_present": True,
                "model_loaded": True, "models": [cfg.chat_model]}
    return create_app(store, cfg, chat_fn=None, probe_fn=probe)


async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def test_history_sessions_and_turns(tmp_path):
    app = _make(tmp_path)
    async with await _client(app) as c:
        s = (await c.get("/history")).json()["sessions"]
        assert any(x["id"] == "s1" for x in s)
        t = (await c.get("/history", params={"session_id": "s1"})).json()["turns"]
        assert [(x["role"], x["content"]) for x in t] == [("user", "Q1"), ("assistant", "A1")]

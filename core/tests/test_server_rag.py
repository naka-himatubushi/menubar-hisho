"""チャット経路の RAG 配線: retrieve が呼ばれ system に載る / 完了後に index タスクが走る。"""
import anyio
import httpx
import pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app


def _cfg(tmp_path):
    return load_config(env={"HISHO_DB": str(tmp_path / "t.db")})


async def _fake_chat(messages, **kw):
    _fake_chat.seen = messages
    yield {"type": "delta", "content": "はい、カレーですね"}
    yield {"type": "done", "finish_reason": "stop", "eval_count": 1}


async def test_popover_chat_injects_memories_and_indexes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)

    retrieved = ["私の好物はカレーライスです"]
    indexed = []

    async def fake_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        return retrieved

    async def fake_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    from hisho_core import server as server_mod
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(server_mod.rag, "index_turn", fake_index)

    app = create_app(store, cfg, chat_fn=_fake_chat)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "sess-x", "stream": True,
                               "messages": [{"role": "user", "content": "夕飯どうしよう、おすすめは?"}]})
        assert r.status_code == 200

    system = _fake_chat.seen[0]["content"]
    assert "カレーライス" in system                      # 記憶が注入された
    await anyio.sleep(0.05)                              # fire-and-forget の索引を待つ
    assert any("夕飯どうしよう" in c for c in indexed)    # user ターンが索引対象になった


async def test_external_chat_has_no_rag(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    called = []

    async def fake_retrieve(*a, **kw):
        called.append(1)
        return []

    from hisho_core import server as server_mod
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)

    app = create_app(store, cfg, chat_fn=_fake_chat)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         json={"stream": True,
                               "messages": [{"role": "user", "content": "外部ツールからの質問です"}]})
        assert r.status_code == 200
    assert called == []  # external では検索も注入もしない

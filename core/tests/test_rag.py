"""rag.py: fake ollama クライアントで embed 形状・索引・検索・backfill・失敗時の無害性を検証。"""
import asyncio
import struct
import pytest
from hisho_core import rag
from hisho_core.config import load_config
from hisho_core.store import Store


def _cfg(tmp_path, **extra):
    env = {"HISHO_DB": str(tmp_path / "t.db"), **extra}
    return load_config(env=env)


class _FakeEmbedClient:
    """/api/embed を真似る。単語→固定4次元ベクトルの決定的マップ。"""
    def __init__(self, dim=4):
        self.dim = dim
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        vecs = []
        for text in json["input"]:
            v = [0.0] * self.dim
            v[hash(text) % self.dim] = 1.0
            vecs.append(v)
        class R:
            status_code = 200
            def json(self_inner):
                return {"embeddings": vecs}
        return R()

    async def aclose(self):
        pass


async def test_embed_returns_float32_blobs():
    fake = _FakeEmbedClient()
    out = await rag.embed(["こんにちは"], model="bge-m3",
                          ollama_host="http://127.0.0.1:11434",
                          client_factory=lambda: fake)
    assert out is not None and len(out) == 1
    assert len(out[0]) == 4 * 4  # float32 × 4
    assert fake.calls[0]["url"].endswith("/api/embed")
    assert fake.calls[0]["json"]["model"] == "bge-m3"


async def test_embed_failure_returns_none():
    class _Boom:
        async def post(self, url, json=None):
            import httpx
            raise httpx.ConnectError("down")
        async def aclose(self):
            pass
    out = await rag.embed(["x"], model="bge-m3", ollama_host="http://127.0.0.1:1",
                          client_factory=lambda: _Boom())
    assert out is None


async def test_index_and_retrieve_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    store.get_or_create_session("sess-a", 1000)
    t1 = store.append_user_turn("sess-a", "私の好物はカレーライスです", 1000, "popover")
    lock = asyncio.Lock()
    fake = _FakeEmbedClient()

    ok = await rag.index_turn(store, lock, t1, "sess-a", "私の好物はカレーライスです",
                              config=cfg, client_factory=lambda: fake)
    assert ok is True

    hits = await rag.retrieve(store, "私の好物はカレーライスです", config=cfg,
                              exclude_session_id="sess-b", client_factory=lambda: fake)
    assert hits == ["私の好物はカレーライスです"]

    # 同一セッションは除外される(直近 replay と二重にならない)
    hits_same = await rag.retrieve(store, "私の好物はカレーライスです", config=cfg,
                                   exclude_session_id="sess-a", client_factory=lambda: fake)
    assert hits_same == []


async def test_retrieve_disabled_returns_empty(tmp_path):
    cfg = _cfg(tmp_path, HISHO_RAG="0")
    store = Store(cfg.db_path, vec_dim=4)
    hits = await rag.retrieve(store, "何か", config=cfg, exclude_session_id=None,
                              client_factory=lambda: _FakeEmbedClient())
    assert hits == []


async def test_backfill_indexes_pending(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    store.get_or_create_session("sess-a", 1000)
    store.append_user_turn("sess-a", "バックフィル対象のターンです", 1000, "popover")
    store.append_user_turn("sess-a", "こちらも索引対象のターン", 2000, "popover")
    lock = asyncio.Lock()
    n = await rag.backfill(store, lock, config=cfg,
                           client_factory=lambda: _FakeEmbedClient())
    assert n == 2
    assert store.unindexed_popover_turns(10) == []

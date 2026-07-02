"""役割: forget_memories ツールの単体テスト (閾値・cap・turn 除外・embed 失敗)。"""
import asyncio
import struct
from hisho_core import tools
from hisho_core.config import load_config


class FakeStore:
    def __init__(self, hits):
        self._hits = hits
        self.soft_deleted = None
        self.turns_forgotten = None

    def search_forgettable(self, vec, k):
        return self._hits

    def soft_delete_chunks(self, ids, now):
        self.soft_deleted = list(ids)

    def mark_turns_forgotten(self, ids):
        self.turns_forgotten = list(ids)


class _Lock:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _cfg():
    return load_config()


async def _fake_embed_ok(texts, **kw):
    return [struct.pack("<4f", 0.1, 0.1, 0.1, 0.1)]


async def _fake_embed_fail(texts, **kw):
    return None


def test_forget_respects_threshold_and_marks_turns():
    store = FakeStore(hits=[
        {"id": 5, "content": "猫の名前はモチ", "source_type": "turn", "source_id": 13, "distance": 0.2},
        {"id": 9, "content": "遠い無関係な記憶", "source_type": "document", "source_id": 1, "distance": 0.9},
    ])
    out = asyncio.run(tools.forget_memories(
        {"query": "猫"}, store=store, config=_cfg(), write_lock=_Lock(),
        embed=_fake_embed_ok, now_ms=100))
    assert out["count"] == 1          # 0.9 は閾値超過で除外
    assert out["matched"] == 1
    assert store.soft_deleted == [5]
    assert store.turns_forgotten == [13]  # turn 由来のみ
    assert out["items"] == ["猫の名前はモチ"]


def test_forget_embed_failure_is_safe():
    store = FakeStore(hits=[])
    out = asyncio.run(tools.forget_memories(
        {"query": "猫"}, store=store, config=_cfg(), write_lock=_Lock(),
        embed=_fake_embed_fail, now_ms=100))
    assert "error" in out
    assert store.soft_deleted is None  # 何も消さない

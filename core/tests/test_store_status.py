"""役割: latest_status_chunk (sensors 全滅時の最終既知値取得) の store 層テスト。"""
import struct
from hisho_core.store import Store


def _vec(*floats):
    return struct.pack(f"<{len(floats)}f", *floats)


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"), vec_dim=4)


def test_latest_status_chunk_returns_none_when_empty(tmp_path):
    s = _store(tmp_path)
    if not s.rag_enabled:
        return  # sqlite-vec 無し環境ではスキップ
    assert s.latest_status_chunk() is None


def test_latest_status_chunk_returns_active_content(tmp_path):
    s = _store(tmp_path)
    if not s.rag_enabled:
        return
    s.add_chunk("status", 1, None, "現在の状況: 全機OK", _vec(0.1, 0.1, 0.1, 0.1), "bge-m3", 4)
    assert s.latest_status_chunk() == "現在の状況: 全機OK"


def test_latest_status_chunk_ignores_forgotten(tmp_path):
    s = _store(tmp_path)
    if not s.rag_enabled:
        return
    cid = s.add_chunk("status", 1, None, "古い状況", _vec(0.1, 0.1, 0.1, 0.1), "bge-m3", 4)
    s.soft_delete_chunks([cid], 999)
    assert s.latest_status_chunk() is None

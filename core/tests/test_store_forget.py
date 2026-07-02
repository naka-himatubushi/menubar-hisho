"""役割: forget 機能の store 層 (migration v3・soft-delete・replay 除外) のテスト。"""
import struct

from hisho_core.store import Store


def _mk_store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_migration_v3_adds_status_columns(tmp_path):
    s = _mk_store(tmp_path)
    if not s.rag_enabled:
        return  # sqlite-vec 無し環境ではスキップ
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "status" in cols
    assert "forgotten_at" in cols
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] >= 3


def _vec(store, floats):
    return struct.pack(f"<{len(floats)}f", *floats)


def test_soft_delete_and_search_excludes(tmp_path):
    s = _mk_store(tmp_path)
    if not s.rag_enabled:
        return
    dim = s.vec_dim
    v = _vec(s, [0.1] * dim)
    cid = s.add_chunk("document", 1, None, "猫の名前はモチ", v, "bge-m3", dim)
    # soft-delete 前は search_forgettable で拾える
    hits = s.search_forgettable(v, 5)
    assert any(h["id"] == cid for h in hits)
    s.soft_delete_chunks([cid], 123)
    # 後は search_chunks / search_forgettable から消える
    assert all(h["id"] != cid for h in s.search_forgettable(v, 5))
    assert all(r["content"] != "猫の名前はモチ" for r in s.search_chunks(v, 5))
    row = s.conn.execute("SELECT status, forgotten_at FROM chunks WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "forgotten" and row["forgotten_at"] == 123


def test_mark_turns_forgotten_excludes_from_replay(tmp_path):
    s = _mk_store(tmp_path)
    s.get_or_create_session("sess-1", 1)
    tid = s.append_user_turn("sess-1", "私は猫を飼っている", 1, "popover")
    assert any(t["content"] == "私は猫を飼っている" for t in s.recent_turns("sess-1", 10))
    s.mark_turns_forgotten([tid])
    assert all(t["content"] != "私は猫を飼っている" for t in s.recent_turns("sess-1", 10))

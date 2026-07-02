"""役割: forget 機能の store 層 (migration v3・soft-delete・replay 除外) のテスト。"""
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

"""Store 初期化で WAL・STRICT スキーマ・FTS・user_version が整うことを検証。"""
from hisho_core.store import Store


def test_schema_and_pragmas(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"sessions", "turns", "turns_fts"} <= tables
    assert s.user_version() == 1
    s.close()


def test_idempotent_open(tmp_path):
    p = str(tmp_path / "t.db")
    Store(p).close()
    s = Store(p)  # 再オープンで壊れない
    assert s.user_version() == 1
    s.close()

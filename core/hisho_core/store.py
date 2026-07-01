"""SQLite の唯一のスキーマ所有者。会話ターンの記録・取得を担う(WAL, STRICT)。"""
from __future__ import annotations
import json
import sqlite3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, title TEXT,
    created_at INTEGER NOT NULL, last_activity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete',
    model TEXT, token_count INTEGER,
    created_at INTEGER NOT NULL, completed_at INTEGER,
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_session_seq ON turns(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    content, content='turns', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._bootstrap()

    def _bootstrap(self) -> None:
        c = self.conn
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA synchronous = NORMAL")
        c.executescript(_SCHEMA_V1)
        if self.user_version() < 1:
            c.execute("PRAGMA user_version = 1")
        c.commit()

    def user_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()

    def get_or_create_session(self, session_id: str, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO sessions(id, created_at, last_activity) VALUES(?,?,?) "
            "ON CONFLICT(id) DO NOTHING", (session_id, now_ms, now_ms))
        self.conn.commit()

    def next_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM turns WHERE session_id=?",
            (session_id,)).fetchone()
        return row["n"]

    def append_user_turn(self, session_id: str, content: str, now_ms: int, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO turns(session_id, seq, role, content, status, created_at, completed_at, meta) "
            "VALUES(?, (SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?), 'user', ?, 'complete', ?, ?, ?)",
            (session_id, session_id, content, now_ms, now_ms, json.dumps({"source": source})))
        self.conn.commit()
        return cur.lastrowid

    def add_assistant_placeholder(self, session_id: str, model: str, now_ms: int) -> int:
        cur = self.conn.execute(
            "INSERT INTO turns(session_id, seq, role, content, status, model, created_at) "
            "VALUES(?, (SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?), 'assistant', '', 'streaming', ?, ?)",
            (session_id, session_id, model, now_ms))
        self.conn.commit()
        return cur.lastrowid

    def finalize_turn(self, turn_id: int, content: str, token_count: int | None, status: str,
                      completed_at_ms: int) -> None:
        self.conn.execute(
            "UPDATE turns SET content=?, token_count=?, status=?, completed_at=? WHERE id=?",
            (content, token_count, status, completed_at_ms, turn_id))
        self.conn.commit()

    def recent_turns(self, session_id: str, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM turns WHERE session_id=? AND status='complete' "
            "ORDER BY seq DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def touch_session(self, session_id: str, now_ms: int) -> None:
        self.conn.execute("UPDATE sessions SET last_activity=? WHERE id=?", (now_ms, session_id))
        self.conn.commit()

    def list_sessions(self, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, title, last_activity FROM sessions WHERE status != 'deleted' "
            "ORDER BY last_activity DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

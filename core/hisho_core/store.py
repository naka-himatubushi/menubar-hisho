"""SQLite の唯一のスキーマ所有者。会話ターンの記録・取得を担う(WAL, STRICT)。v2 で RAG テーブルを追加。"""
from __future__ import annotations
import json
import logging
import sqlite3

logger = logging.getLogger("hisho")

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
    def __init__(self, db_path: str, vec_dim: int = 1024):
        self.vec_dim = vec_dim
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._bootstrap()
        self._init_rag()

    def _bootstrap(self) -> None:
        c = self.conn
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA synchronous = NORMAL")
        c.execute("PRAGMA wal_autocheckpoint = 1000")
        c.executescript(_SCHEMA_V1)
        if self.user_version() < 1:
            c.execute("PRAGMA user_version = 1")
        c.commit()

    def user_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def _init_rag(self) -> None:
        """v2 additive 移行: sqlite-vec をロードし chunks/embeddings/vec0 を作る。
        拡張ロード失敗時は rag_enabled=False で通常動作を続ける(クラッシュ禁止)。"""
        self.rag_enabled = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except Exception:
            logger.warning("sqlite-vec load failed — RAG disabled", exc_info=True)
            return
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    session_id TEXT,
                    content TEXT NOT NULL,
                    token_count INTEGER,
                    meta TEXT NOT NULL DEFAULT '{{}}'
                ) STRICT;
                CREATE UNIQUE INDEX IF NOT EXISTS uq_chunks_source
                    ON chunks(source_type, source_id);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vec BLOB NOT NULL,
                    PRIMARY KEY (chunk_id, model)
                ) STRICT;
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_bge_m3
                    USING vec0(embedding float[{self.vec_dim}]);
                PRAGMA user_version = 2;
            """)
            self.conn.commit()
        self.rag_enabled = True

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

    def add_assistant_placeholder(self, session_id: str, model: str, now_ms: int,
                                   source: str = "external") -> int:
        cur = self.conn.execute(
            "INSERT INTO turns(session_id, seq, role, content, status, model, created_at, meta) "
            "VALUES(?, (SELECT COALESCE(MAX(seq),0)+1 FROM turns WHERE session_id=?), 'assistant', '', 'streaming', ?, ?, ?)",
            (session_id, session_id, model, now_ms, json.dumps({"source": source})))
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

    def add_chunk(self, source_type: str, source_id: int, session_id: str,
                  content: str, vec: bytes, model: str, dim: int) -> int:
        """chunk + embedding + vec0 索引を 1 txn で追加。戻り値 chunk id。重複 source は既存 id を返す。
        (DO NOTHING 後の lastrowid/rowcount は挙動が曖昧なため、SELECT 先行で明示分岐する。)"""
        row = self.conn.execute(
            "SELECT id FROM chunks WHERE source_type=? AND source_id=?",
            (source_type, source_id)).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO chunks(source_type, source_id, session_id, content) VALUES(?,?,?,?)",
            (source_type, source_id, session_id, content))
        cid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO embeddings(chunk_id, model, dim, vec) VALUES(?,?,?,?)",
            (cid, model, dim, vec))
        self.conn.execute(
            "INSERT INTO vec_chunks_bge_m3(rowid, embedding) VALUES(?,?)",
            (cid, vec))
        self.conn.commit()
        return cid

    def search_chunks(self, query_vec: bytes, k: int,
                      exclude_session_id: str | None = None) -> list[dict]:
        """vec0 kNN → chunks join。exclude_session_id は現在の会話(直近 replay 済)を除くため。"""
        rows = self.conn.execute(
            "SELECT c.content, c.session_id, c.source_type, v.distance "
            "FROM vec_chunks_bge_m3 v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND v.k = ? "
            "ORDER BY v.distance",
            (query_vec, k + 16)).fetchall()  # 除外・再ランク分を見込み多めに取る
        out = []
        for content, session_id, source_type, distance in rows:
            if exclude_session_id is not None and session_id == exclude_session_id:
                continue
            # 会話 turn の自己エコー除外: 過去のほぼ同一質問は情報ゼロ (質問が質問を引く問題)
            if source_type == "turn" and distance < 0.15:
                continue
            out.append({"content": content, "session_id": session_id,
                        "source_type": source_type, "distance": distance})
        # 知識 (document/status 等) に最低 2 枠を保証し、残りは距離順で埋める。
        # 距離ボーナス方式は長文知識が短文 turn に負けがちなため、枠の保証で確実にする。
        knowledge = [h for h in out if h["source_type"] != "turn"]
        reserved = knowledge[:min(2, k)]
        rest = sorted((h for h in out if h not in reserved), key=lambda h: h["distance"])
        return (reserved + rest)[:k]

    def unindexed_popover_turns(self, limit: int = 50) -> list[dict]:
        """未索引の popover complete ターン(10文字以上)を古い順に返す(backfill 用)。"""
        rows = self.conn.execute(
            "SELECT t.id, t.session_id, t.content FROM turns t "
            "LEFT JOIN chunks c ON c.source_type='turn' AND c.source_id = t.id "
            "WHERE c.id IS NULL AND t.status='complete' "
            "  AND length(t.content) >= 10 "
            "  AND t.content NOT LIKE '%?' AND t.content NOT LIKE '%？' "
            "  AND json_extract(t.meta, '$.source') = 'popover' "
            "ORDER BY t.id LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "session_id": r[1], "content": r[2]} for r in rows]

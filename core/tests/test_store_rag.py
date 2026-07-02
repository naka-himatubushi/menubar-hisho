"""store v2 (RAG テーブル): 移行・chunk 追加・kNN 検索・未索引抽出を tmp DB で検証。"""
import struct
from hisho_core.store import Store


def _vec(*floats):
    return struct.pack(f"<{len(floats)}f", *floats)


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"), vec_dim=4)  # テストは 4 次元で軽く


def test_migration_v2_and_rag_enabled(tmp_path):
    s = _store(tmp_path)
    assert s.rag_enabled is True
    ver = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'")}
    assert {"chunks", "embeddings"} <= tables


def test_existing_tables_untouched(tmp_path):
    s = _store(tmp_path)
    # v1 のテーブルと索引がそのまま生きている
    s.get_or_create_session("sess-a", 1000)
    s.append_user_turn("sess-a", "こんにちは", 1000, "popover")
    assert s.recent_turns("sess-a", 10)[0]["content"] == "こんにちは"


def test_add_and_search_chunks(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "私の好物はカレーです", 1000, "popover")
    t2 = s.append_user_turn("sess-a", "明日は雨らしい", 2000, "popover")
    s.add_chunk("turn", t1, "sess-a", "私の好物はカレーです", _vec(1, 0, 0, 0), "bge-m3", 4)
    s.add_chunk("turn", t2, "sess-a", "明日は雨らしい", _vec(0, 1, 0, 0), "bge-m3", 4)

    hits = s.search_chunks(_vec(0.9, 0.1, 0, 0), k=1)
    assert len(hits) == 1
    assert hits[0]["content"] == "私の好物はカレーです"
    assert hits[0]["session_id"] == "sess-a"


def test_search_excludes_session(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "カレー", 1000, "popover")
    s.add_chunk("turn", t1, "sess-a", "カレー", _vec(1, 0, 0, 0), "bge-m3", 4)

    assert s.search_chunks(_vec(1, 0, 0, 0), k=3, exclude_session_id="sess-a") == []
    assert len(s.search_chunks(_vec(1, 0, 0, 0), k=3, exclude_session_id="sess-b")) == 1


def test_unindexed_popover_turns(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "popover の発話", 1000, "popover")
    s.append_user_turn("sess-a", "外部ツールの発話", 2000, "external")  # 対象外
    s.append_user_turn("sess-a", "短い", 3000, "popover")               # 10 文字未満は対象外

    rows = s.unindexed_popover_turns(limit=10)
    assert [r["id"] for r in rows] == [t1]

    s.add_chunk("turn", t1, "sess-a", "popover の発話", _vec(0, 0, 0, 1), "bge-m3", 4)
    assert s.unindexed_popover_turns(limit=10) == []

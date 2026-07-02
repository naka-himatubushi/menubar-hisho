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
    assert ver >= 3
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

    hits = s.search_chunks(_vec(0.7, 0.3, 0, 0), k=1)  # 自己エコー域(距離<0.15)より遠いクエリ
    assert len(hits) == 1
    assert hits[0]["content"] == "私の好物はカレーです"
    assert hits[0]["session_id"] == "sess-a"


def test_search_excludes_session(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "カレー", 1000, "popover")
    s.add_chunk("turn", t1, "sess-a", "カレー", _vec(1, 0, 0, 0), "bge-m3", 4)

    assert s.search_chunks(_vec(0.7, 0.3, 0, 0), k=3, exclude_session_id="sess-a") == []
    assert len(s.search_chunks(_vec(0.7, 0.3, 0, 0), k=3, exclude_session_id="sess-b")) == 1


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


def test_knowledge_ranks_above_similar_turn(tmp_path):
    """document/status は多少遠くても会話 turn より優先される (距離ボーナス 0.2)。"""
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "会話でカレーの話をした", 1000, "popover")
    s.add_chunk("turn", t1, "sess-a", "会話でカレーの話をした", _vec(0.8, 0.2, 0, 0), "bge-m3", 4)
    s.add_chunk("status", 1, None, "現在のバックアップ状況: 全機器OK", _vec(0.6, 0.4, 0, 0), "bge-m3", 4)

    hits = s.search_chunks(_vec(1, 0, 0, 0), k=2)
    assert any(h["source_type"] == "status" for h in hits)  # turn の方が近くても知識枠で必ず入る


def test_turn_self_echo_is_filtered(tmp_path):
    """距離 <0.15 の turn (=ほぼ同一質問) は返さない。知識なら同距離でも返す。"""
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "バックアップできてる?と聞いた", 1000, "popover")
    s.add_chunk("turn", t1, "sess-a", "バックアップできてる?と聞いた", _vec(1, 0, 0, 0), "bge-m3", 4)
    assert s.search_chunks(_vec(1, 0, 0, 0), k=3) == []

    s.add_chunk("document", 1, None, "重要な事実", _vec(0, 1, 0, 0), "bge-m3", 4)
    hits = s.search_chunks(_vec(0, 1, 0, 0), k=3)
    assert hits[0]["content"] == "重要な事実"  # 知識は距離0でも自己エコー扱いしない
    assert all(h["distance"] >= 0.15 for h in hits if h["source_type"] == "turn")


def test_unindexed_skips_questions(tmp_path):
    """「?」「？」で終わる popover 発話は backfill 対象に載らない。"""
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    s.append_user_turn("sess-a", "私の猫の名前を覚えてますか？", 1000, "popover")
    s.append_user_turn("sess-a", "what is my cat name?", 2000, "popover")
    t3 = s.append_user_turn("sess-a", "猫の名前はモチといいます", 3000, "popover")
    assert [r["id"] for r in s.unindexed_popover_turns(10)] == [t3]


def test_status_always_takes_first_slot(tmp_path):
    """status は document より遠くても必ず含まれる (現況の鮮度優先)。"""
    s = _store(tmp_path)
    s.add_chunk("document", 1, None, "近い文書1", _vec(1, 0, 0, 0), "bge-m3", 4)
    s.add_chunk("document", 2, None, "近い文書2", _vec(0.9, 0.1, 0, 0), "bge-m3", 4)
    s.add_chunk("status", 1, None, "現在の状況", _vec(0, 0, 0, 1), "bge-m3", 4)

    hits = s.search_chunks(_vec(1, 0, 0, 0), k=2)
    assert hits[0]["source_type"] == "status"
    assert hits[1]["content"] == "近い文書1"

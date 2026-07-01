"""ターン記録の一連(user同期→assistantプレースホルダ→finalize)と取得を検証。"""
from hisho_core.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_turn_lifecycle(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess1", 1000)
    assert s.next_seq("sess1") == 1
    uid = s.append_user_turn("sess1", "こんにちは", 1000, source="popover")
    aid = s.add_assistant_placeholder("sess1", "qwen3.6:35b-a3b", 1001)
    # プレースホルダは streaming で recent に出ない
    assert [t["content"] for t in s.recent_turns("sess1", 10)] == ["こんにちは"]
    s.finalize_turn(aid, "やあ", token_count=3, status="complete", completed_at_ms=1002)
    rows = s.recent_turns("sess1", 10)
    assert [(r["role"], r["content"]) for r in rows] == [("user", "こんにちは"), ("assistant", "やあ")]
    assert s.next_seq("sess1") == 3
    s.close()


def test_partial_on_disconnect(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("s", 1)
    aid = s.add_assistant_placeholder("s", "m", 2)
    s.finalize_turn(aid, "途中まで", token_count=None, status="partial", completed_at_ms=3)
    row = s.conn.execute("SELECT status, content FROM turns WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "partial" and row["content"] == "途中まで"
    s.close()


def test_source_recorded_in_meta(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("s", 1)
    uid = s.append_user_turn("s", "hi", 1, source="external")
    meta = s.conn.execute("SELECT meta FROM turns WHERE id=?", (uid,)).fetchone()["meta"]
    assert '"source"' in meta and "external" in meta
    s.close()

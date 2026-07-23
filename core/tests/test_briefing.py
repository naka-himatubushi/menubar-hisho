"""役割: briefing topic (朝ブリーフィング = 既存 all の実測結果 + 期限リストの残日数) の全層テスト。
- _guess_topic: 「おはよう」等のパターン発火/非発火
- sensors.deadlines_report / load_briefing_targets: 残日数計算(未来/当日/超過)・
  briefing_targets.json 欠損/壊れ JSON/形式不正エントリの安全な丸め (now は注入して決定的にテスト)
- tools.check_status: briefing 分岐 (all 相当の実測 + 期限セクションの合成)
- server 統合: 事前実測→SENSOR_NOTE 注入・RAG 両方向スキップ
実 subprocess / 実 ollama / 実時刻(datetime.now)は一切使わない (すべて fake / 注入)。"""
import asyncio
import json
from datetime import datetime

import anyio
import httpx
from fastapi.testclient import TestClient

from hisho_core import sensors
from hisho_core import server as server_mod
from hisho_core import tools
from hisho_core.config import load_config
from hisho_core.context import SENSOR_NOTE
from hisho_core.server import create_app, _guess_topic
from hisho_core.store import Store


# 固定「今」= 2026-07-23。sensors.now_header 等と同じ思想で now を注入し、
# datetime.now (壁時計) には一切依存させない。
def _fixed_now():
    return datetime(2026, 7, 23, 9, 0)


# --- _guess_topic (パターン発火/非発火) ---

def test_guess_topic_briefing_ohayou():
    assert _guess_topic("おはよう") == "briefing"


def test_guess_topic_briefing_ohayou_gozaimasu_prefix_match():
    # 「おはよう」が前方一致で含まれる丁寧形でも発火する (search なので部分一致)
    assert _guess_topic("おはようございます") == "briefing"


def test_guess_topic_briefing_other_trigger_phrases():
    assert _guess_topic("朝の報告お願いします") == "briefing"
    assert _guess_topic("ブリーフィングして") == "briefing"
    assert _guess_topic("今日の状況を教えて") == "briefing"


def test_guess_topic_briefing_does_not_fire_on_ordinary_sentences():
    assert _guess_topic("モチの好物は何?") == "all"        # 語ゼロ一致 → all (既存仕様)
    assert _guess_topic("今日はいい天気ですね") == "all"    # 「今日」はあるが「今日の状況」ではない


def test_guess_topic_briefing_plus_other_group_falls_to_all():
    # briefing + backup の 2 群一致 → all (briefing は all を包含する上位 topic なので
    # 「測りすぎ」ではなく「期限セクションが付かない」側に倒れるだけ = 安全側)
    assert _guess_topic("おはよう、バックアップの調子は?") == "all"


def test_sensor_intent_gate_fires_for_briefing_words(tmp_path):
    """ゲート ⊇ パターンの不変条件(test_sensor_gate_covers_all_topic_pattern_words と同じ主旨)を
    briefing 語について自己完結で確認する。"""
    store = Store(str(tmp_path / "t.db"))
    app = create_app(store, load_config(), chat_fn=None)
    for word in ("おはよう", "朝の報告", "ブリーフィング", "今日の状況"):
        assert app.state.sensor_intent.search(word), f"briefing 語 {word!r} がゲートに無い"


# --- sensors.deadlines_report / load_briefing_targets (残日数計算) ---

def test_deadline_future_shows_days_remaining(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [
        {"label": "AWS 課金Lab 停止期限", "date": "2026-08-10"}]}))
    out = sensors.deadlines_report(p, now=_fixed_now)
    assert out == "⏰ AWS 課金Lab 停止期限: あと18日 (2026-08-10)"


def test_deadline_today_shows_honjitsu(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [{"label": "本日締切", "date": "2026-07-23"}]}))
    out = sensors.deadlines_report(p, now=_fixed_now)
    assert out == "⏰ 本日締切: 本日 (2026-07-23)"


def test_deadline_overdue_shows_warning_with_elapsed_days(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [{"label": "過去の締切", "date": "2026-07-20"}]}))
    out = sensors.deadlines_report(p, now=_fixed_now)
    assert out == "⚠️ 過去の締切: 超過3日 (2026-07-20)"


def test_deadline_multiple_entries_joined_by_newline_and_keep_order(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [
        {"label": "AWS 課金Lab 停止期限", "date": "2026-08-10"},
        {"label": "AWS SAA 受験", "date": "2026-11-30"},
    ]}))
    out = sensors.deadlines_report(p, now=_fixed_now)
    lines = out.split("\n")
    assert lines == [
        "⏰ AWS 課金Lab 停止期限: あと18日 (2026-08-10)",
        "⏰ AWS SAA 受験: あと130日 (2026-11-30)",
    ]


# --- briefing_targets.json 欠損/壊れ JSON/形式不正 (例外にならず「期限リストなし」) ---

def test_deadlines_report_missing_file_returns_placeholder(tmp_path):
    out = sensors.deadlines_report(tmp_path / "does-not-exist.json", now=_fixed_now)
    assert out == sensors.BRIEFING_NO_DEADLINES == "期限リストなし"


def test_deadlines_report_corrupt_json_returns_placeholder_not_exception(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text("{ this is not valid json ,,, ")
    out = sensors.deadlines_report(p, now=_fixed_now)
    assert out == sensors.BRIEFING_NO_DEADLINES


def test_deadlines_report_wrong_root_shape_returns_placeholder(tmp_path):
    p = tmp_path / "briefing_targets.json"
    for bad in ('{"deadlines": "not-a-list"}', "{}", "[]", "null"):
        p.write_text(bad)
        assert sensors.deadlines_report(p, now=_fixed_now) == sensors.BRIEFING_NO_DEADLINES


def test_load_briefing_targets_skips_malformed_entries_keeps_valid(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [
        {"label": "OK", "date": "2026-08-10"},
        {"label": "日付なし"},
        {"date": "2026-08-10"},
        "文字列だけ",
        {"label": "型違い", "date": 20260810},
    ]}))
    items = sensors.load_briefing_targets(p)
    assert [i["label"] for i in items] == ["OK"]


def test_deadline_bad_date_format_is_skipped_not_raised(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": [
        {"label": "壊れた日付", "date": "2026/08/10"},   # スラッシュ区切りは非対応形式
        {"label": "OK", "date": "2026-08-10"},
    ]}))
    out = sensors.deadlines_report(p, now=_fixed_now)
    assert "壊れた日付" not in out
    assert "OK" in out


def test_deadlines_report_empty_deadlines_list_returns_placeholder(tmp_path):
    p = tmp_path / "briefing_targets.json"
    p.write_text(json.dumps({"deadlines": []}))
    assert sensors.deadlines_report(p, now=_fixed_now) == sensors.BRIEFING_NO_DEADLINES


# --- tools.check_status: briefing 分岐 (all 相当の実測 + 期限セクションの合成) ---

class FakeStoreWithStatus:
    def __init__(self, status_chunk=None):
        self._status = status_chunk

    def latest_status_chunk(self):
        return self._status


def _cfg(tmp_path, **extra_env):
    env = {"HISHO_DB": str(tmp_path / "secretary.db"),
           "HISHO_BRIEFING_TARGETS": str(tmp_path / "briefing_targets.json")}
    env.update(extra_env)
    return load_config(env=env)


def test_check_status_briefing_is_all_measurement_plus_deadlines(tmp_path, monkeypatch):
    """briefing = 既存 all の実測結果 + 期限セクション (design point 3, 4)。
    all 台帳を実測しつつ、期限リストの残日数を1レポートに合成する。"""
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "MacBook", "cmd": "echo hi"}]}')
    (tmp_path / "briefing_targets.json").write_text(
        json.dumps({"deadlines": [{"label": "AWS SAA 受験", "date": "2026-11-30"}]}))
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": "MacBook", "output": "稼働中"}])

    out = asyncio.run(tools.check_status(
        {"topic": "briefing"}, store=FakeStoreWithStatus(), config=_cfg(tmp_path),
        write_lock=None))

    assert out["topic"] == "briefing"
    assert "実測" in out["report"]                 # all 相当のヘッダ
    assert "【MacBook】" in out["report"]
    assert "稼働中" in out["report"]
    assert "AWS SAA 受験" in out["report"]          # 期限セクション
    assert "2026-11-30" in out["report"]


def test_check_status_briefing_missing_deadlines_file_shows_placeholder_not_error(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "MacBook", "cmd": "echo hi"}]}')
    # briefing_targets.json はわざと置かない
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": "MacBook", "output": "稼働中"}])

    out = asyncio.run(tools.check_status(
        {"topic": "briefing"}, store=FakeStoreWithStatus(), config=_cfg(tmp_path),
        write_lock=None))

    assert out["topic"] == "briefing"
    assert "稼働中" in out["report"]
    assert "期限リストなし" in out["report"]


def test_check_status_briefing_does_not_run_library_search(tmp_path, monkeypatch):
    """briefing は all 相当であり、all と同じく書庫検索を含まない (検索語が無いのに
    jarvis find が走る経路の否定。test_check_status_all_topic_does_not_run_library_search と対。"""
    ran = []
    monkeypatch.setattr(sensors, "library_search", lambda q, d: ran.append(q))
    monkeypatch.setattr(sensors, "run_all", lambda items: [])

    out = asyncio.run(tools.check_status(
        {"topic": "briefing"}, store=FakeStoreWithStatus(), config=_cfg(tmp_path),
        write_lock=None))

    assert ran == []
    assert out["topic"] == "briefing"


def test_check_status_briefing_reuses_all_failure_fallback_to_last_known_value(tmp_path, monkeypatch):
    """briefing の実測部分は all と同じ「全滅時は最終既知値」フォールバックを継承する
    (_measure_ledger_topic を共有しているため)。"""
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "false"}]}')
    monkeypatch.setattr(
        sensors, "run_all",
        lambda items: [{"name": "TestMac", "output": "実測失敗: タイムアウト (8秒)"}])

    out = asyncio.run(tools.check_status(
        {"topic": "briefing"}, store=FakeStoreWithStatus("昨日の最終収集: 全機OK"),
        config=_cfg(tmp_path), write_lock=None))

    assert "実測できなかったため最終既知値" in out["report"]
    assert "昨日の最終収集: 全機OK" in out["report"]
    assert "期限リストなし" in out["report"]   # briefing_targets.json も未設置


# --- server 統合 (事前実測 → 注入 / RAG 両方向スキップ) ---

def _sse_text(resp_bytes):
    return resp_bytes.decode("utf-8", "replace")


def _make_chat_fn(script):
    """script: 呼び出し回数ごとに返すイベント列のリスト。tools と messages の受領を記録。"""
    calls = {"tools_seen": [], "messages_seen": [], "n": 0}

    async def chat_fn(messages, *, model, ollama_host, num_ctx, keep_alive, think=False, tools=None):
        calls["tools_seen"].append(tools)
        calls["messages_seen"].append(list(messages))
        seq = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for e in seq:
            yield e

    return chat_fn, calls


async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True


def _plain_answer_script(text="おはようございます"):
    return [[{"type": "delta", "content": text}, {"type": "done", "finish_reason": "stop"}]]


def _fake_check_status_briefing(called):
    """topic を記録し、all実測+期限を合成済みの固定レポートを返す fake check_status。"""
    async def fake(args, **kw):
        called["topic"] = args.get("topic")
        called["n"] = called.get("n", 0) + 1
        return {"topic": args.get("topic"),
                "report": ("07:30 実測\n\n【MacBook】\n稼働中"
                           "\n\n⏰ AWS SAA 受験: あと130日 (2026-11-30)")}
    return fake


def test_briefing_phrase_triggers_gate_and_injects_combined_report(tmp_path):
    """「おはよう」→ topic=briefing で check_status が呼ばれ、all実測+期限を合成した
    レポートが SENSOR_NOTE 前置の system メッセージとして注入される。
    モデルにツールは渡さない (他 sensor topic と同じ「要約する係のみ」)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status_briefing(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "brief1", "messages": [{"role": "user", "content": "おはよう"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert called["topic"] == "briefing"
    assert calls["tools_seen"][0] is None            # ツールは一切渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(SENSOR_NOTE)]
    assert len(injected) == 1
    assert "【MacBook】" in injected[0]["content"]
    assert "AWS SAA 受験" in injected[0]["content"]


def test_external_source_does_not_trigger_briefing(tmp_path):
    """実行系の経路は popover 限定 (external から「おはよう」で来ても実測しない)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("おはようございます"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status_briefing(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "brief2", "messages": [{"role": "user", "content": "おはよう"}]},
                   headers={"X-Hisho-Source": "external"})
        assert r.status_code == 200
    assert called.get("n", 0) == 0


async def test_briefing_turn_skips_memories_and_indexing(tmp_path, monkeypatch):
    """briefing ターンも他 sensor 同様 RAG を両方向で遮断する (skip_index は
    is_sensor 相乗り。design point 5): 注入なし・索引なし。"""
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status_briefing(called)}

    retrieved, indexed = [], []

    async def spy_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        retrieved.append(msg)
        return ["古い状態メモ"]

    async def spy_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    monkeypatch.setattr(server_mod.rag, "retrieve", spy_retrieve)
    monkeypatch.setattr(server_mod.rag, "index_turn", spy_index)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions", headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "briefR", "messages": [{"role": "user", "content": "おはよう"}]})
        assert r.status_code == 200
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert retrieved == []   # memories 注入なし
    assert indexed == []     # 索引なし

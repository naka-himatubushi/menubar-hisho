"""役割: server のセンサー系ゲート (決定的事前実測→文脈注入・forget 先勝ち・M2 多層防御) の統合テスト。"""
import logging

import anyio
import httpx
from fastapi.testclient import TestClient

from hisho_core import server as server_mod
from hisho_core.server import create_app, _guess_topic, _TOPIC_PATTERNS
from hisho_core.config import load_config
from hisho_core.context import SENSOR_NOTE
from hisho_core.store import Store


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


def _plain_answer_script(text="順調です"):
    return [[{"type": "delta", "content": text}, {"type": "done", "finish_reason": "stop"}]]


def _fake_check_status(called):
    """topic を記録して固定レポートを返す fake check_status を作る。"""
    async def fake(args, **kw):
        called["topic"] = args.get("topic")
        called["n"] = called.get("n", 0) + 1
        return {"topic": args.get("topic"), "report": "12:00 実測\n\n【MacBook】\n稼働中"}
    return fake


# --- 事前実測 → 文脈注入 ---

def test_sensor_turn_measures_before_model_and_injects_report(tmp_path):
    """安全性の要 #4: 状態質問はモデルに任せず、応答生成の前にサーバが実測し
    レポートを system メッセージとして文脈注入する。モデルにツールは渡さない。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sens1", "messages": [{"role": "user", "content": "マシンの調子どう?"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert called["topic"] == "machines"       # 「マシン」→ machines 群のみ一致
    assert calls["tools_seen"][0] is None      # センサーターンはツールを一切渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(SENSOR_NOTE)]
    assert len(injected) == 1                  # SENSOR_NOTE 前置の追加 system が 1 つ
    assert "12:00 実測" in injected[0]["content"]
    assert "【MacBook】" in injected[0]["content"]


def test_sensor_premeasure_failure_injects_honest_failure_note(tmp_path):
    """実測自体が例外で死んでも応答は継続し、「実測に失敗」の注入で正直に伝える。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("すみません"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def raising_check_status(args, **kw):
        raise RuntimeError("boom")

    app.state.tool_registry = {"check_status": raising_check_status}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sensF", "messages": [{"role": "user", "content": "ディスクの容量は?"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    assert any("実測に失敗しました" in m["content"] for m in sys_msgs)


def test_health_topic_phrase_triggers_sensor_gate_and_injects_report(tmp_path):
    """本番スモーク回帰: 「朝レポート見せて」は health topic 語だが、
    sensor_intent ゲートに health 語が無いと is_sensor=False のまま _guess_topic
    まで到達せず実測されない (一般論で答えてしまう)。popover からの発話で
    check_status が呼ばれ、レポートが system メッセージとして注入されることを検証する。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("異常はありません"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sensH", "messages": [{"role": "user", "content": "朝レポート見せて"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert called.get("topic") == "health"     # 「レポート」→ health 群のみ一致
    assert calls["tools_seen"][0] is None       # センサーターンはツールを一切渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(SENSOR_NOTE)]
    assert len(injected) == 1                   # SENSOR_NOTE 前置の追加 system が 1 つ
    assert "12:00 実測" in injected[0]["content"]


# --- topic 推定 (単体マトリクス) ---

def test_guess_topic_matrix():
    assert _guess_topic("バックアップの状況は?") == "backup"
    assert _guess_topic("ディスクの空きは?") == "storage"
    assert _guess_topic("サーバは生きてる?") == "machines"
    assert _guess_topic("ちゃんと動いてる?") == "machines"
    assert _guess_topic("調子どう?") == "all"                       # topic 語ゼロ → all
    assert _guess_topic("バックアップ用ディスクの容量は?") == "all"  # 2 群一致 → all
    assert _guess_topic("バックアップのマシンは稼働してる?") == "all"  # backup+machines → all
    assert _guess_topic("朝レポート見せて") == "health"
    assert _guess_topic("何か異常出てる?") == "health"
    assert _guess_topic("警報鳴った?") == "health"
    assert _guess_topic("バックアップの異常は?") == "all"   # backup+health 2群 → all


def test_sensor_gate_covers_all_topic_pattern_words(tmp_path):
    """_TOPIC_PATTERNS の各語は sensor_intent ゲートも必ず通ること。
    パターンに足してゲートに足し忘れると、その語の質問が実測されず
    一般論を答える (2026-07-07 の本番スモークで実際に発生した欠陥)。"""
    store = Store(str(tmp_path / "t.db"))
    app = create_app(store, load_config(), chat_fn=None)
    for topic, rx in _TOPIC_PATTERNS:
        for word in rx.pattern.split("|"):
            assert app.state.sensor_intent.search(word), (
                f"topic {topic!r} の語 {word!r} が sensor_intent ゲートに無い")


# --- forget 先勝ち / specs フィルタ ---

def test_forget_keyword_wins_over_sensor_keyword(tmp_path):
    """forget ゲートが先勝ち: forget/sensor 双方の語を含む発話は forget として処理され、
    check_status は一切呼ばれない (既存挙動不変)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("承知しました"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    forget_called = {"q": None}
    status_called = {}

    async def fake_forget(args, **kw):
        forget_called["q"] = args.get("query")
        return {"count": 1, "matched": 1, "truncated": False, "items": ["バックアップの話"]}

    app.state.tool_registry = {"forget_memories": fake_forget,
                               "check_status": _fake_check_status(status_called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sens4", "messages": [{"role": "user", "content": "バックアップの話は消して"}]},
                   headers={"X-Hisho-Source": "popover"})
        body = _sse_text(r.content)
    assert forget_called["q"] is not None       # forget が発火 (フォールバック経由)
    assert status_called.get("n", 0) == 0       # check_status は呼ばれない (forget 先勝ち)
    assert "[記憶を 1件 忘れました]" in body


def test_forget_turn_passes_only_forget_spec(tmp_path):
    """M2 ゲート: 忘却ターンでモデルに渡す specs は forget_memories のみ
    (check_status が混ざると日常語で破壊的ツールが幻覚実行される経路が開く)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("承知しました"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    async def fake_forget(args, **kw):
        return {"count": 0, "matched": 0, "truncated": False, "items": []}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        c.post("/v1/chat/completions",
               json={"session_id": "spec1", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
               headers={"X-Hisho-Source": "popover"})
    tools = calls["tools_seen"][0]
    assert tools is not None
    assert [t["function"]["name"] for t in tools] == ["forget_memories"]


# --- M2 多層防御 (ディスパッチ層) ---

def test_dispatch_rejects_forget_without_intent(tmp_path, caplog):
    """多層防御: 忘却ゲートを通っていないターンでモデルが forget_memories の
    tool_call を出しても、サーバは実行を拒否して warning を残す。"""
    store = Store(str(tmp_path / "t.db"))
    script = [
        [{"type": "tool_call", "id": "cX", "name": "forget_memories", "arguments": {"query": "全部"}},
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)

    executed = {"n": 0}

    async def spy_forget(args, **kw):
        executed["n"] += 1
        return {"count": 99, "matched": 99, "truncated": False, "items": []}

    app.state.tool_registry = {"forget_memories": spy_forget}
    with caplog.at_level(logging.WARNING, logger="hisho"):
        with TestClient(app) as c:
            r = c.post("/v1/chat/completions",
                       json={"session_id": "guard1", "messages": [{"role": "user", "content": "今日の天気は"}]},
                       headers={"X-Hisho-Source": "popover"})
            assert r.status_code == 200
            body = _sse_text(r.content)
    assert executed["n"] == 0                       # forget は実行されない
    assert "忘れました" not in body                  # 決定的報告も出ない
    assert any("forget_memories rejected" in rec.message for rec in caplog.records)


# --- 非発火 ---

def test_unrelated_question_does_not_measure_or_pass_tools(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("モチはカリカリが好きです"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    status_called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(status_called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sens5", "messages": [{"role": "user", "content": "モチの好物は何?"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert status_called.get("n", 0) == 0          # 実測しない
    assert calls["tools_seen"][0] is None          # tools も渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    assert not any(m["content"].startswith(SENSOR_NOTE) for m in sys_msgs)


def test_external_source_never_triggers_sensor_gate(tmp_path):
    """実行系の経路は popover 限定。external から状態語で来ても実測しない (安全)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("ok"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    status_called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(status_called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "sens6", "messages": [{"role": "user", "content": "バックアップの状態は?"}]},
                   headers={"X-Hisho-Source": "external"})
        assert r.status_code == 200
    assert status_called.get("n", 0) == 0
    assert calls["tools_seen"][0] is None


# --- RAG スキップ (memories 注入なし・索引なし) ---

async def test_sensor_turn_skips_memories_and_indexing(tmp_path, monkeypatch):
    """センサーターンは RAG を両方向で遮断する:
    - 注入なし (古い状態記憶が新実測と矛盾して語りを汚染する。実測: gemma4:12b)
    - 索引なし (揮発性の実測値が「既知の事実」として将来に注入される。レビュー#7)"""
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    chat_fn, calls = _make_chat_fn(_plain_answer_script("全機稼働中です"))
    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}

    retrieved = []
    indexed = []

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
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "sensR", "messages": [{"role": "user", "content": "マシンは稼働してる?"}]})
        assert r.status_code == 200
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert retrieved == []   # memories 注入なし
    assert indexed == []     # 索引なし

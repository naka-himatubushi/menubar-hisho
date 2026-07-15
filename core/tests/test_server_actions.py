"""役割: server のアクション状態機械 (提案→確認→実行・破棄・優先順位・非実行経路の証明) の統合テスト。
実行係 (executor) は必ず fake を注入し、実 subprocess は一切走らせない。"""
import json
import logging

import anyio
import httpx
from fastapi.testclient import TestClient

from hisho_core import actions
from hisho_core import server as server_mod
from hisho_core.server import create_app
from hisho_core.config import load_config
from hisho_core.context import ACTION_NOTE
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


def _plain(text="はい、どうぞ"):
    return [[{"type": "delta", "content": text}, {"type": "done", "finish_reason": "stop"}]]


def _write_ledger(tmp_path):
    (tmp_path / "action_targets.json").write_text(json.dumps({
        "backup_ssh": {"macbook": None, "studio": "user@studio-host.example",
                       "mini": "user@mini-host.example"},
        "work_cli": "~/.local/bin/work",
    }))


def _mk_app(tmp_path, script, executor=None):
    """tmp_path を app_support_dir にした app + 記録係を組む。executor は spy 必須。
    (vec_dim は既定の 1024: 非ゲートターンは実 retrieve が走るため、
    実 embed(1024次元) と次元が食い違うと落ちる)"""
    _write_ledger(tmp_path)
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    chat_fn, calls = _make_chat_fn(script)
    executed = []

    def spy_exec(argv):
        executed.append(list(argv))
        return "Backup started."

    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true,
                     action_executor=executor or spy_exec)
    return app, calls, executed


def _post(app, session_id, content, source="popover"):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": session_id, "messages": [{"role": "user", "content": content}]},
                   headers={"X-Hisho-Source": source})
        assert r.status_code == 200
        return _sse_text(r.content)


def _seed_pending(app, session_id, argv=None, display=None):
    pa = actions.PendingAction(
        action="start_backup", args={"machine": "macbook"},
        argv=argv or ["tmutil", "startbackup"],
        display=display or "tmutil startbackup", session_id=session_id)
    app.state.pending_actions.put(session_id, pa)
    return pa


# --- 提案ターン (安全不変条件 1: 初回ターンで実行しない) ---

def test_proposal_turn_never_executes_and_pends(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("承知しました、実行しますね"))
    body = _post(app, "prop1", "バックアップ回しておいて")
    assert executed == []                              # 絶対に実行しない
    assert "実行内容: tmutil startbackup" in body       # サーバ定型の提案文
    assert "はい で実行" in body
    assert "承知しました、実行しますね" not in body       # モデルの content は流さない (定型のみ)
    pa = app.state.pending_actions.pop("prop1")
    assert pa is not None and pa.argv == ["tmutil", "startbackup"]


def test_proposal_turn_publishes_only_action_specs(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain())
    _post(app, "prop2", "バックアップ回しておいて")
    tools = calls["tools_seen"][0]
    assert tools is not None
    assert sorted(t["function"]["name"] for t in tools) == ["fleet_submit", "start_backup"]


def test_model_tool_call_becomes_pending_not_execution(tmp_path):
    """モデルの tool_call は実行されず pending に変換されるだけ (確認なし実行経路の否定)。"""
    script = [
        [{"type": "tool_call", "id": "a1", "name": "start_backup",
          "arguments": {"machine": "studio"}},
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    app, calls, executed = _mk_app(tmp_path, script)
    body = _post(app, "prop3", "スタジオのバックアップ回しておいて")
    assert executed == []                       # tool_call が来ても実行しない
    pa = app.state.pending_actions.pop("prop3")
    assert pa is not None
    assert pa.argv[0] == "ssh"                  # モデル案 (studio) が採用されている
    assert "user@studio-host.example" in pa.argv
    assert "実行内容:" in body


def test_model_invalid_tool_args_fall_back_to_deterministic(tmp_path):
    script = [
        [{"type": "tool_call", "id": "a2", "name": "start_backup",
          "arguments": {"machine": "toaster"}},   # enum 外
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    app, calls, executed = _mk_app(tmp_path, script)
    body = _post(app, "prop4", "バックアップ回しておいて")
    pa = app.state.pending_actions.pop("prop4")
    assert pa is not None and pa.argv == ["tmutil", "startbackup"]  # 決定的構築 (macbook 既定)
    assert executed == []


def test_model_error_still_yields_deterministic_proposal(tmp_path):
    """隠し呼び出しがエラーでも提案は決定的に組み立てられる (不変条件 5)。"""
    script = [[{"type": "error", "message": "ollama down"}]]
    app, calls, executed = _mk_app(tmp_path, script)
    body = _post(app, "prop5", "ミニでバックアップ取って")
    assert "実行内容:" in body
    pa = app.state.pending_actions.pop("prop5")
    assert pa is not None and "user@mini-host.example" in pa.argv


def test_fleet_proposal_uses_full_message_as_task(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain())
    msg = "スタジオでテスト全部やらせておいて"
    body = _post(app, "prop6", msg)
    pa = app.state.pending_actions.pop("prop6")
    assert pa is not None and pa.action == "fleet_submit"
    assert pa.argv[1] == "studio"
    assert pa.argv[2] == msg          # task = 発話全文が argv の 1 要素
    assert executed == []


def test_missing_ledger_reports_cleanly_without_pending(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})  # 台帳をわざと置かない
    store = Store(cfg.db_path)
    chat_fn, calls = _make_chat_fn(_plain())
    executed = []
    app = create_app(store, cfg, chat_fn=chat_fn, probe_fn=lambda: _ok_probe(),
                     warmup_fn=_true, unload_fn=_true,
                     action_executor=lambda argv: executed.append(argv) or "x")
    body = _post(app, "prop7", "バックアップ回しておいて")
    assert "台帳未設置" in body
    assert app.state.pending_actions.pop("prop7") is None   # pending は作られない
    assert executed == []


# --- 確認ターン (実行はここだけ) ---

def test_confirmation_executes_pending_once(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("バックアップを開始しました"))
    _seed_pending(app, "conf1")
    body = _post(app, "conf1", "はい")
    assert executed == [["tmutil", "startbackup"]]     # 確認で 1 回だけ実行
    assert "バックアップを開始しました" in body           # モデルが結果を報告
    assert calls["tools_seen"][0] is None               # 確認ターンにツールは渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(ACTION_NOTE)]
    assert len(injected) == 1                           # 実行結果が文脈注入されている
    assert "Backup started." in injected[0]["content"]
    assert "実行" in injected[0]["content"]              # 実行時刻ヘッダつき定型

    body2 = _post(app, "conf1", "はい")                  # 二度目の「はい」
    assert executed == [["tmutil", "startbackup"]]     # 再実行されない (pop 一回限り)


def test_non_confirmation_discards_pending(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("モチはカリカリが好きです"))
    _seed_pending(app, "disc1")
    body = _post(app, "disc1", "モチの好物は何?")
    assert executed == []                               # 実行されない
    assert "[保留中の操作は取り消しました]" in body        # 破棄を一言添える
    assert "モチはカリカリが好きです" in body              # 通常応答は続く
    assert app.state.pending_actions.pop("disc1") is None  # pending は消えている


def test_confirmation_in_other_session_does_not_execute(tmp_path):
    """pending は session 束縛: 別 session の「はい」では実行されない。"""
    app, calls, executed = _mk_app(tmp_path, _plain())
    _seed_pending(app, "sessA")
    _post(app, "sessB", "はい")
    assert executed == []                                  # 他 session では実行なし
    assert app.state.pending_actions.pop("sessA") is not None  # A の pending は無傷


# --- 優先順位 (forget > 確認 > 提案 > sensor) ---

def test_forget_wins_over_pending_confirmation(tmp_path):
    """pending 中でも forget 発話は forget として処理され、アクションは破棄される。"""
    app, calls, executed = _mk_app(tmp_path, _plain("承知しました"))
    _seed_pending(app, "pri1")

    forget_called = {"n": 0}

    async def fake_forget(args, **kw):
        forget_called["n"] += 1
        return {"count": 1, "matched": 1, "truncated": False, "items": ["猫"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    body = _post(app, "pri1", "猫のこと忘れて")
    assert forget_called["n"] == 1                     # forget が発火
    assert executed == []                              # アクションは実行されない
    assert "[保留中の操作は取り消しました]" in body
    assert app.state.pending_actions.pop("pri1") is None


def test_sensor_question_while_pending_discards_and_measures(tmp_path):
    """pending 中の sensor 質問 → pending 破棄 + 実測は通常どおり走る。"""
    app, calls, executed = _mk_app(tmp_path, _plain("容量は十分です"))
    _seed_pending(app, "pri2")

    status_called = {"n": 0}

    async def fake_check_status(args, **kw):
        status_called["n"] += 1
        return {"topic": args.get("topic"), "report": "12:00 実測\n【mini】空き 50%"}

    app.state.tool_registry = {"check_status": fake_check_status}
    body = _post(app, "pri2", "ディスクの容量は?")
    assert executed == []
    assert status_called["n"] == 1                     # sensor は動く
    assert "[保留中の操作は取り消しました]" in body


def test_new_action_request_while_pending_replaces_it(tmp_path):
    """pending 中に別のアクション要求 → 旧 pending は破棄され、新提案に置き換わる。"""
    app, calls, executed = _mk_app(tmp_path, _plain())
    _seed_pending(app, "rep1", argv=["ssh", "old-dest", "tmutil startbackup"],
                  display="ssh old-dest tmutil startbackup")
    body = _post(app, "rep1", "やっぱりミニでバックアップ取って")
    assert executed == []
    assert "[保留中の操作は取り消しました]" in body       # 旧提案の破棄を明示
    assert "実行内容:" in body                           # 新提案の定型文
    pa = app.state.pending_actions.pop("rep1")
    assert pa is not None and "user@mini-host.example" in pa.argv  # 新 pending に置換


def test_action_gate_beats_sensor_gate(tmp_path):
    """「バックアップ回しておいて」は sensor 語 (バックアップ) も含むが、提案が勝つ。"""
    app, calls, executed = _mk_app(tmp_path, _plain())
    status_called = {"n": 0}

    async def fake_check_status(args, **kw):
        status_called["n"] += 1
        return {"topic": "all", "report": "x"}

    app.state.tool_registry = {"check_status": fake_check_status}
    body = _post(app, "pri3", "バックアップ回しておいて")
    assert "実行内容:" in body                          # 提案になる
    assert status_called["n"] == 0                      # sensor 実測は走らない
    assert app.state.pending_actions.pop("pri3") is not None


def test_status_check_request_routes_to_sensor_not_action(tmp_path):
    """「バックアップの状態を確認して」は確認依頼 (sensor 意図)。
    bare「して」で提案化しない (main 統合レビューで発見した誤ルーティングの再発防止)。"""
    app, calls, executed = _mk_app(tmp_path, _plain("正常です"))
    status_called = {"n": 0}

    async def fake_check_status(args, **kw):
        status_called["n"] += 1
        return {"topic": "backup", "report": "x"}

    app.state.tool_registry = {"check_status": fake_check_status}
    body = _post(app, "pri4", "バックアップの状態を確認して")
    assert status_called["n"] == 1                      # sensor 実測が走る
    assert "実行内容:" not in body                      # 提案にならない
    assert app.state.pending_actions.pop("pri4") is None


def test_bare_backup_request_still_proposes(tmp_path):
    """名詞直結の「バックアップして」は action 提案のまま (ゲート限定強化の裏面保証)。"""
    app, calls, executed = _mk_app(tmp_path, _plain())
    body = _post(app, "pri5", "バックアップして")
    assert "実行内容:" in body
    assert app.state.pending_actions.pop("pri5") is not None
    assert executed == []                               # 提案止まり、実行はしない


# --- 非発火 / 非実行経路の証明 ---

def test_unrelated_message_creates_no_pending(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("晴れです"))
    body = _post(app, "none1", "今日の天気は")
    assert calls["tools_seen"][0] is None
    assert app.state.pending_actions.pop("none1") is None
    assert executed == []
    assert "実行内容:" not in body


def test_external_source_never_proposes_or_executes(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("ok"))
    body = _post(app, "ext1", "バックアップ回しておいて", source="external")
    assert executed == []
    assert app.state.pending_actions.pop("ext1") is None
    assert "実行内容:" not in body
    assert calls["tools_seen"][0] is None


def test_action_tool_call_on_normal_turn_is_not_dispatched(tmp_path, caplog):
    """アクションは tools REGISTRY に居ないので、通常ターンの tool_call でも実行不能
    (確認なし実行の経路がディスパッチ層にも存在しないことの証明)。"""
    script = [
        [{"type": "tool_call", "id": "x1", "name": "start_backup",
          "arguments": {"machine": "macbook"}},
         {"type": "done", "finish_reason": "tool_calls"}],
    ]
    app, calls, executed = _mk_app(tmp_path, script)
    with caplog.at_level(logging.WARNING, logger="hisho"):
        body = _post(app, "none2", "今日の天気は")
    assert executed == []                              # 実行されない
    assert any("unknown tool" in rec.message for rec in caplog.records)


# --- RAG 両方向スキップ ---

async def test_action_turns_skip_memories_and_indexing(tmp_path, monkeypatch):
    """提案/確認ターンは RAG 注入・索引ともスキップ (sensors と同じ)。"""
    _write_ledger(tmp_path)
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    chat_fn, calls = _make_chat_fn(_plain("開始しました"))
    executed = []
    app = create_app(store, cfg, chat_fn=chat_fn, probe_fn=lambda: _ok_probe(),
                     warmup_fn=_true, unload_fn=_true,
                     action_executor=lambda argv: executed.append(argv) or "ok")

    retrieved, indexed = [], []

    async def spy_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        retrieved.append(msg)
        return ["古いメモ"]

    async def spy_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    monkeypatch.setattr(server_mod.rag, "retrieve", spy_retrieve)
    monkeypatch.setattr(server_mod.rag, "index_turn", spy_index)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # 提案ターン
        r1 = await c.post("/v1/chat/completions", headers={"X-Hisho-Source": "popover"},
                          json={"session_id": "rag1", "messages": [{"role": "user", "content": "バックアップ回しておいて"}]})
        assert r1.status_code == 200
        # 確認ターン
        r2 = await c.post("/v1/chat/completions", headers={"X-Hisho-Source": "popover"},
                          json={"session_id": "rag1", "messages": [{"role": "user", "content": "はい"}]})
        assert r2.status_code == 200
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert retrieved == []   # memories 注入なし (両ターン)
    assert indexed == []     # 索引なし (両ターン)
    assert executed and executed[0] == ["tmutil", "startbackup"]  # 確認で実行はされている

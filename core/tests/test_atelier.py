"""役割: atelier (工房発注) action の単体テスト。
repo-key/task-text の決定的抽出 (LLM 非経由) と、提案→確認→executor 呼び出しの
argv 形状を検証する。台帳はテスト用 tmp_path に fake を置く (実パス・実ホスト名は書かない)。
実行 (dispatch script) は fake executor 注入で完全に避ける — 実 subprocess は一切走らせない。"""
import json

import pytest
from fastapi.testclient import TestClient

from hisho_core import actions
from hisho_core import tools as tools_module
from hisho_core.config import load_config
from hisho_core.context import ACTION_NOTE
from hisho_core.server import create_app
from hisho_core.store import Store

REPOS = {
    "library-db": "/tmp/atelier-fixture/library-db",
    "aws-dojo": "/tmp/atelier-fixture/aws-dojo",
    "arch-dojo": "/tmp/atelier-fixture/arch-dojo",
}


def _write_atelier_ledger(tmp_path, repos=None):
    (tmp_path / "atelier_targets.json").write_text(json.dumps({"repos": repos if repos is not None else REPOS}))


# --- (a) extract_atelier_task: task-text の決定的抽出 ---

@pytest.mark.parametrize("text, repo_key, expected", [
    ("library-db に検索機能を実装しといて", "library-db", "検索機能"),
    ("library-db に検索機能を実装しておいて", "library-db", "検索機能"),
    ("aws-dojo で新しいLab教材を追加する機能を実装して", "aws-dojo", "新しいLab教材を追加する機能"),
    ("工房に発注、library-dbにダークモード対応を作っといて", "library-db", "ダークモード対応"),
    ("arch-dojoのバグを直しといて。工房にお願い", "arch-dojo", "バグ"),
    ("library-db に実装しといて", "library-db", ""),        # 空ケース: task なし
    ("library-db用に実装しておいて", "library-db", ""),      # 空ケース (助詞違い)
])
def test_extract_atelier_task_cases(text, repo_key, expected):
    assert actions.extract_atelier_task(text, repo_key) == expected


def test_extract_atelier_task_leaves_unrelated_text_alone_when_no_boilerplate():
    """定型語 (実装して等) が無ければ何も剥がさず素通し (呼び出し側は task 空判定で聞き返す側)。"""
    text = "library-db のことをちょっと聞きたい"
    assert actions.extract_atelier_task(text, "library-db") == "ことをちょっと聞きたい"


# --- (b) repo-key 解決: 一致 / 不一致 → 聞き返し ---

def test_resolve_atelier_repo_matches_single_key(tmp_path):
    _write_atelier_ledger(tmp_path)
    assert actions.resolve_atelier_repo("library-db に実装しといて", tmp_path) == "library-db"
    assert actions.resolve_atelier_repo("aws-dojoに実装しといて", tmp_path) == "aws-dojo"


def test_resolve_atelier_repo_no_match_returns_none(tmp_path):
    _write_atelier_ledger(tmp_path)
    assert actions.resolve_atelier_repo("工房に発注して", tmp_path) is None


def test_resolve_atelier_repo_ambiguous_multiple_match_returns_none(tmp_path):
    """複数 repo-key が同時に言及されたら曖昧とみなし None (安全側で聞き返す —
    server._guess_topic の複数一致→all 送りと同じ考え方)。"""
    _write_atelier_ledger(tmp_path)
    msg = "library-dbとaws-dojoどっちにも実装しといて"
    assert actions.resolve_atelier_repo(msg, tmp_path) is None


def test_resolve_atelier_repo_missing_ledger_raises_action_error(tmp_path):
    with pytest.raises(actions.ActionError, match="台帳未設置"):
        actions.resolve_atelier_repo("library-db に実装しといて", tmp_path)


def test_atelier_repo_keys_lists_ledger_order(tmp_path):
    _write_atelier_ledger(tmp_path)
    assert actions.atelier_repo_keys(tmp_path) == ["library-db", "aws-dojo", "arch-dojo"]


# --- ゲート語 (is_atelier_intent): 採用/不採用の実測 ---

@pytest.mark.parametrize("text", [
    "library-db に検索機能を実装しといて",
    "library-db に検索機能を実装しておいて",
    "aws-dojoに新機能を実装して",
    "工房に発注して",
    "工房に aws-dojo のクイズ採点バグ修正を発注して",   # 長距離: repo 名+タスク句を跨ぐ ({0,40})
    "library-dbにダークモード対応を工房にお願い",
])
def test_atelier_gate_fires_on_adopted_phrases(text):
    assert actions.is_atelier_intent(text) is True


@pytest.mark.parametrize("text", [
    "資料を作っといて",                  # 「作っといて」は bare 採用せず (日常語すぎる)
    "予定を直しといて",                  # 「直しといて」も同様に bare 不採用
    "このAPIはどう実装してありますか",     # 「実装してある/あります」誤爆防止 (否定先読み)
    "このコードもう実装してあった気がする",  # 過去形 (あった) も同様に除外
    "陶芸工房に行ってきた",               # 「工房に」だけでは発火しない (発注動詞を伴わない)
    "今日の天気は",
])
def test_atelier_gate_does_not_fire_on_rejected_or_unrelated_phrases(text):
    assert actions.is_atelier_intent(text) is False


# --- ACTION_SPECS/ACTION_NAMES/REGISTRY: 「隠し呼び出しのみ」構造の確認 ---

def test_atelier_registered_in_specs_but_not_directly_callable():
    names = [s["function"]["name"] for s in actions.ACTION_SPECS]
    assert "atelier" in names
    assert "atelier" not in actions.ACTION_NAMES     # モデル elicitation 経由では作れない
    assert "atelier" not in tools_module.REGISTRY     # 通常ターンの tool_call でも呼べない


# --- server 統合: 提案→確認→executor 呼び出し (フルフロー) ---

def _make_chat_fn(script):
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


def _mk_app(tmp_path, script, repos=None):
    """tmp_path を app_support_dir にした app + 記録係を組む。executor は spy。"""
    _write_atelier_ledger(tmp_path, repos)
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    chat_fn, calls = _make_chat_fn(script)
    executed = []

    def spy_exec(argv):
        executed.append(list(argv))
        return "dispatched: library-db branch=atelier/20260723-090000 out=/tmp/out.md"

    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true,
                     action_executor=spy_exec)
    return app, calls, executed


def _post(app, session_id, content, source="popover"):
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": session_id, "messages": [{"role": "user", "content": content}]},
                   headers={"X-Hisho-Source": source})
        assert r.status_code == 200
        return r.content.decode("utf-8", "replace")


def test_atelier_proposal_never_calls_model_and_pends(tmp_path):
    """repo-key/task-text 抽出は LLM 非経由: 提案ターンで chat_fn は一度も呼ばれない。"""
    app, calls, executed = _mk_app(tmp_path, _plain("承知しました"))
    body = _post(app, "atl1", "library-db に検索機能を実装しといて")
    assert calls["n"] == 0                              # モデルを一切呼ばない
    assert "工房に発注します: repo=library-db" in body
    assert "タスク=「検索機能」" in body
    assert "承知しました" not in body                     # モデルの content は流さない (定型のみ)
    assert executed == []                               # 提案だけでは実行しない
    pa = app.state.pending_actions.pop("atl1")
    assert pa is not None and pa.action == "atelier"
    assert pa.args == {"repo": "library-db", "task": "検索機能"}


def test_atelier_confirmation_executes_with_correct_argv(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("発注しました。通知をお待ちください"))
    _post(app, "atl2", "library-db に検索機能を実装しといて")
    body = _post(app, "atl2", "はい")
    assert len(executed) == 1
    argv = executed[0]
    assert argv[0].endswith("scripts/atelier_dispatch.sh")
    assert argv[1] == "library-db"
    assert argv[2] == "検索機能"
    assert "発注しました" in body
    body2 = _post(app, "atl2", "はい")                   # 二度目の「はい」
    assert len(executed) == 1                            # 再実行されない (pop 一回限り)
    assert "実行待ちの操作はありません" in body2


def test_atelier_execution_report_states_no_merge_fact(tmp_path):
    """執行後に文脈注入される action_report に「merge/検収しない」旨が事実として入っている
    (モデルに発明させず、サーバが事実を渡す — ACTION_NOTE 経由でモデルは要約のみ)。"""
    app, calls, executed = _mk_app(tmp_path, _plain("発注しました"))
    _post(app, "atl3", "library-db に検索機能を実装しといて")
    _post(app, "atl3", "はい")
    sys_msgs = [m for m in calls["messages_seen"][-1] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(ACTION_NOTE)]
    assert len(injected) == 1
    assert "merge" in injected[0]["content"]
    assert "検収は人間が行います" in injected[0]["content"]
    assert "dispatched: library-db" in injected[0]["content"]   # dispatch script の stdout も入っている


def test_atelier_unknown_repo_asks_and_does_not_call_model(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain())
    body = _post(app, "atl4", "工房に発注して")
    assert "どのリポジトリですか?" in body
    assert "library-db" in body and "aws-dojo" in body and "arch-dojo" in body
    assert calls["n"] == 0
    assert executed == []
    assert app.state.pending_actions.pop("atl4") is None   # pending は作られない


def test_atelier_empty_task_asks_and_does_not_call_model(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain())
    body = _post(app, "atl5", "library-db に実装しといて")
    assert "何を発注するか" in body
    assert calls["n"] == 0
    assert executed == []
    assert app.state.pending_actions.pop("atl5") is None


def test_atelier_missing_ledger_reports_cleanly_without_pending(tmp_path):
    """atelier_targets.json が無い場合、提案せず ActionError の文言をそのまま返す。"""
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})   # 台帳をわざと置かない
    store = Store(cfg.db_path)
    chat_fn, calls = _make_chat_fn(_plain())
    executed = []
    app = create_app(store, cfg, chat_fn=chat_fn, probe_fn=lambda: _ok_probe(),
                     warmup_fn=_true, unload_fn=_true,
                     action_executor=lambda argv: executed.append(argv) or "x")
    body = _post(app, "atl6", "library-db に検索機能を実装しといて")
    assert "台帳未設置" in body
    assert app.state.pending_actions.pop("atl6") is None
    assert executed == []


# --- (d) 「はい」以外で破棄される既存フロー維持 (atelier の pending でも同じ挙動) ---

def test_atelier_non_confirmation_discards_pending(tmp_path):
    app, calls, executed = _mk_app(tmp_path, _plain("今日は晴れです"))
    _post(app, "atl7", "library-db に検索機能を実装しといて")
    body = _post(app, "atl7", "今日の天気は?")
    assert executed == []
    assert "[保留中の操作は取り消しました]" in body
    assert "今日は晴れです" in body                        # 通常応答は続く
    assert app.state.pending_actions.pop("atl7") is None


def test_atelier_forget_wins_over_pending_atelier_confirmation(tmp_path):
    """既存の優先順位 (forget > 確認) は atelier の pending にも適用される。"""
    app, calls, executed = _mk_app(tmp_path, _plain("承知しました"))
    _post(app, "atl8", "library-db に検索機能を実装しといて")

    forget_called = {"n": 0}

    async def fake_forget(args, **kw):
        forget_called["n"] += 1
        return {"count": 1, "matched": 1, "truncated": False, "items": ["猫"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    body = _post(app, "atl8", "猫のこと忘れて")
    assert forget_called["n"] == 1
    assert executed == []
    assert "[保留中の操作は取り消しました]" in body
    assert app.state.pending_actions.pop("atl8") is None

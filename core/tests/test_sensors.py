"""役割: sensors.py (台帳ロード・並列実行・整形・timeout/deadline・全滅判定) の単体テスト。
subprocess は monkeypatch で fake に差し替え、実コマンドは一切実行しない。"""
import subprocess
import time
from datetime import datetime

import pytest

from hisho_core import sensors


def _fixed_now():
    return datetime(2024, 1, 1, 14, 5)


class FakePopen:
    """subprocess.Popen の差し替え。timeout_first=True だと最初の communicate
    (timeout 指定あり) で TimeoutExpired を投げ、kill 後の回収呼び出しには応じる。"""
    def __init__(self, stdout="", stderr="", timeout_first=False):
        self.pid = 4242
        self._stdout, self._stderr = stdout, stderr
        self._timeout_first = timeout_first

    def communicate(self, timeout=None):
        if self._timeout_first and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return (self._stdout, self._stderr)


# --- format_report / all_failed (純粋関数) ---

def test_format_report_joins_items_with_blank_lines():
    results = [{"name": "A", "output": "元気"}, {"name": "B", "output": "元気2"}]
    assert sensors.format_report(results, missing=[]) == "【A】\n元気\n\n【B】\n元気2"


def test_format_report_appends_missing_notes():
    text = sensors.format_report([], missing=["x.json が見つかりません"])
    assert text == "x.json が見つかりません"


def test_all_failed_true_when_every_item_failed():
    results = [{"name": "A", "output": "実測失敗: タイムアウト (8秒)"},
               {"name": "B", "output": "実測失敗: boom"}]
    assert sensors.all_failed(results) is True


def test_all_failed_false_when_one_succeeds():
    results = [{"name": "A", "output": "実測失敗: boom"}, {"name": "B", "output": "OK"}]
    assert sensors.all_failed(results) is False


def test_all_failed_false_when_empty():
    assert sensors.all_failed([]) is False


# --- _run_one (Popen は monkeypatch で差し替え) ---

def test_run_one_success(monkeypatch):
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(stdout="稼働中\n"))
    assert sensors._run_one({"name": "A", "cmd": "uptime"}) == {"name": "A", "output": "稼働中"}


def test_run_one_timeout_kills_process_group(monkeypatch):
    """timeout 時は例外を投げず「実測失敗」に丸め、プロセスグループごと kill する
    (shell=True の複合コマンドは孫プロセスが残るため killpg が必要)。"""
    killed = []
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(timeout_first=True))
    monkeypatch.setattr(sensors.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sensors.os, "killpg", lambda pgid, sig: killed.append(pgid))
    out = sensors._run_one({"name": "遅いマシン", "cmd": "sleep 100"})
    assert out == {"name": "遅いマシン",
                   "output": f"実測失敗: タイムアウト ({sensors.COMMAND_TIMEOUT}秒)"}
    assert killed == [4242]  # プロセスグループが kill された


def test_run_one_popen_uses_new_session(monkeypatch):
    """killpg が効く前提 = start_new_session=True で起動していること。"""
    seen = {}

    def spy_popen(*a, **kw):
        seen.update(kw)
        return FakePopen(stdout="ok")

    monkeypatch.setattr(sensors.subprocess, "Popen", spy_popen)
    sensors._run_one({"name": "A", "cmd": "echo"})
    assert seen.get("start_new_session") is True


def test_run_one_exception_is_captured_not_raised(monkeypatch):
    def raise_err(*a, **kw):
        raise OSError("no such host")

    monkeypatch.setattr(sensors.subprocess, "Popen", raise_err)
    out = sensors._run_one({"name": "壊れた機器", "cmd": "ssh nope"})
    assert out["name"] == "壊れた機器"
    assert out["output"].startswith("実測失敗:")


def test_run_one_malformed_item_is_captured(monkeypatch):
    """cmd 欠落など形式不正のエントリでも例外を漏らさない (第二層)。"""
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(stdout="ok"))
    out = sensors._run_one({"name": "cmdなし"})
    assert out["name"] == "cmdなし"
    assert out["output"].startswith("実測失敗:")

    out2 = sensors._run_one("dictですらない")
    assert out2["name"] == "?"
    assert out2["output"].startswith("実測失敗:")


# --- run_all (並列・部分失敗・全体 deadline) ---

def test_run_all_partial_failure_keeps_order(monkeypatch):
    def fake_run_one(item):
        if item["cmd"] == "ok":
            return {"name": item["name"], "output": "順調"}
        return {"name": item["name"], "output": "実測失敗: タイムアウト (8秒)"}

    monkeypatch.setattr(sensors, "_run_one", fake_run_one)
    items = [{"name": "A", "cmd": "ok"}, {"name": "B", "cmd": "bad"}]
    results = sensors.run_all(items)
    assert [r["name"] for r in results] == ["A", "B"]  # 投入順を保つ
    assert results[0]["output"] == "順調"
    assert results[1]["output"] == "実測失敗: タイムアウト (8秒)"


def test_run_all_empty_items():
    assert sensors.run_all([]) == []


def test_run_all_overall_deadline(monkeypatch):
    """topic 全体の deadline を超えたら、待たずに「全体タイムアウト」の行にして返す。"""
    monkeypatch.setattr(sensors, "TOPIC_DEADLINE", 0.2)

    def slow_run_one(item):
        time.sleep(1.0)
        return {"name": item["name"], "output": "遅い"}

    monkeypatch.setattr(sensors, "_run_one", slow_run_one)
    t0 = time.monotonic()
    results = sensors.run_all([{"name": "A", "cmd": "x"}, {"name": "B", "cmd": "y"}])
    elapsed = time.monotonic() - t0
    assert elapsed < 0.9  # 1 秒スリープを待たずに返っている
    assert [r["name"] for r in results] == ["A", "B"]
    assert all(r["output"].startswith("実測失敗: 全体タイムアウト") for r in results)


def test_run_all_deadline_does_not_hit_fast_items(monkeypatch):
    """速い項目は deadline に巻き込まれない (遅い項目だけ全体タイムアウト)。"""
    monkeypatch.setattr(sensors, "TOPIC_DEADLINE", 0.3)

    def mixed_run_one(item):
        if item["cmd"] == "slow":
            time.sleep(1.0)
        return {"name": item["name"], "output": "順調"}

    monkeypatch.setattr(sensors, "_run_one", mixed_run_one)
    results = sensors.run_all([{"name": "遅", "cmd": "slow"}, {"name": "速", "cmd": "fast"}])
    by_name = {r["name"]: r["output"] for r in results}
    assert by_name["速"] == "順調"
    assert by_name["遅"].startswith("実測失敗: 全体タイムアウト")


# --- ledger_items (台帳の読み込み・形式検証) ---

def test_ledger_items_unknown_topic_raises():
    with pytest.raises(ValueError):
        sensors.ledger_items("cpu", "/tmp/doesnt-matter")


def test_ledger_items_reads_backup_devices(tmp_path):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "MacBook", "cmd": "echo hi"}]}')
    items, missing = sensors.ledger_items("backup", tmp_path)
    assert [i["name"] for i in items] == ["MacBook"]
    assert missing == []


def test_ledger_items_reads_machines_and_storage_topics(tmp_path):
    (tmp_path / "backup_targets.json").write_text('{"devices": []}')
    (tmp_path / "sensor_targets.json").write_text(
        '{"topics": {"machines": [{"name": "M1", "cmd": "echo"}], '
        '"storage": [{"name": "S1", "cmd": "echo"}]}}')
    items, missing = sensors.ledger_items("machines", tmp_path)
    assert [i["name"] for i in items] == ["M1"]
    assert missing == []

    items_all, missing_all = sensors.ledger_items("all", tmp_path)
    assert {i["name"] for i in items_all} == {"S1", "M1"}
    assert missing_all == []  # backup_targets.json も存在するので欠落なし


def test_ledger_items_missing_file_reports_but_does_not_raise(tmp_path):
    items, missing = sensors.ledger_items("backup", tmp_path)
    assert items == []
    assert any("backup_targets.json" in m for m in missing)


def test_ledger_items_skips_malformed_entries(tmp_path):
    """形式不正のエントリ (cmd 欠落・非dict・cmd が非文字列) は実行対象に載せず、
    「形式が不正」の説明として報告する。正しいエントリは生き残る。"""
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "OK", "cmd": "echo"}, {"name": "cmdなし"}, '
        '"ただの文字列", {"name": "型違い", "cmd": 42}]}')
    items, missing = sensors.ledger_items("backup", tmp_path)
    assert [i["name"] for i in items] == ["OK"]
    assert len(missing) == 3
    assert all(m.startswith("台帳エントリの形式が不正") for m in missing)


def test_ledger_root_not_dict_is_safe(tmp_path):
    """台帳ルートが配列など想定外の型でも落ちず、形式不正として報告する。"""
    (tmp_path / "backup_targets.json").write_text('[{"name": "A", "cmd": "echo"}]')
    items, missing = sensors.ledger_items("backup", tmp_path)
    assert items == []
    assert any("形式が不正" in m for m in missing)


def test_sensor_ledger_topics_not_dict_is_safe(tmp_path):
    (tmp_path / "sensor_targets.json").write_text('{"topics": ["x"]}')
    items, missing = sensors.ledger_items("machines", tmp_path)
    assert items == []
    assert any("形式が不正" in m for m in missing)


def test_sensor_ledger_topic_value_not_list_is_safe(tmp_path):
    (tmp_path / "sensor_targets.json").write_text('{"topics": {"machines": "壊れてる"}}')
    items, missing = sensors.ledger_items("machines", tmp_path)
    assert items == []
    assert any("形式が不正" in m for m in missing)


# --- measure (トップレベル関数) ---

def test_measure_unknown_topic_raises():
    with pytest.raises(ValueError):
        sensors.measure("cpu", "/tmp/doesnt-matter")


def test_measure_missing_ledger_returns_message_not_exception(tmp_path):
    out = sensors.measure("backup", tmp_path, now=_fixed_now)
    assert out.startswith("14:05 実測")
    assert "backup_targets.json" in out
    assert "見つかりません" in out


def test_measure_all_reports_both_missing_ledgers(tmp_path):
    out = sensors.measure("all", tmp_path, now=_fixed_now)
    assert "backup_targets.json" in out
    assert "sensor_targets.json" in out


def test_measure_formats_header_and_items(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "echo hi"}]}')
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": "TestMac", "output": "順調"}])
    out = sensors.measure("backup", tmp_path, now=_fixed_now)
    assert out == "14:05 実測\n\n【TestMac】\n順調"


def test_measure_partial_failure_shown_per_item(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "OK機", "cmd": "echo hi"}, {"name": "NG機", "cmd": "true"}]}')

    def fake_run_all(items):
        return [{"name": "OK機", "output": "稼働中"},
                {"name": "NG機", "output": "実測失敗: タイムアウト (8秒)"}]

    monkeypatch.setattr(sensors, "run_all", fake_run_all)
    out = sensors.measure("backup", tmp_path, now=_fixed_now)
    assert "【OK機】\n稼働中" in out
    assert "【NG機】\n実測失敗: タイムアウト (8秒)" in out


def test_measure_all_topic_notes_missing_sensor_ledger(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "echo hi"}]}')
    # sensor_targets.json はわざと置かない
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": "TestMac", "output": "順調"}])
    out = sensors.measure("all", tmp_path, now=_fixed_now)
    assert "sensor_targets.json" in out
    assert "見つかりません" in out

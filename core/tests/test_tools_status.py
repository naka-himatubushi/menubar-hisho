"""役割: check_status ツール (実測本体・不正 topic 正規化・全滅時の最終既知値フォールバック) の単体テスト。
subprocess は monkeypatch で sensors.run_all を差し替え、実コマンドは実行しない。"""
import asyncio

from hisho_core import tools
from hisho_core import sensors
from hisho_core.config import load_config


class FakeStoreWithStatus:
    def __init__(self, status_chunk=None):
        self._status = status_chunk

    def latest_status_chunk(self):
        return self._status


def _cfg(tmp_path):
    return load_config(env={"HISHO_DB": str(tmp_path / "secretary.db")})


def test_check_status_reports_success(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "echo hi"}]}')
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": "TestMac", "output": "アイドル"}])

    out = asyncio.run(tools.check_status(
        {"topic": "backup"}, store=FakeStoreWithStatus(), config=_cfg(tmp_path), write_lock=None))
    assert out["topic"] == "backup"
    assert "実測" in out["report"]
    assert "【TestMac】" in out["report"]
    assert "アイドル" in out["report"]


def test_check_status_falls_back_to_last_known_value_when_all_fail(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "false"}]}')
    monkeypatch.setattr(
        sensors, "run_all",
        lambda items: [{"name": "TestMac", "output": "実測失敗: タイムアウト (8秒)"}])

    out = asyncio.run(tools.check_status(
        {"topic": "backup"}, store=FakeStoreWithStatus("昨日の最終収集: 全機OK"),
        config=_cfg(tmp_path), write_lock=None))
    assert "実測できなかったため最終既知値" in out["report"]
    assert "昨日の最終収集: 全機OK" in out["report"]


def test_check_status_all_failed_but_no_last_known_value(tmp_path, monkeypatch):
    """status チャンクが無い場合は、フォールバック文を足さず実測失敗のみ返す(嘘をつかない)。"""
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "TestMac", "cmd": "false"}]}')
    monkeypatch.setattr(
        sensors, "run_all",
        lambda items: [{"name": "TestMac", "output": "実測失敗: タイムアウト (8秒)"}])

    out = asyncio.run(tools.check_status(
        {"topic": "backup"}, store=FakeStoreWithStatus(None),
        config=_cfg(tmp_path), write_lock=None))
    assert "実測できなかったため最終既知値" not in out["report"]
    assert "実測失敗" in out["report"]


def test_check_status_invalid_topic_normalizes_to_all(tmp_path, monkeypatch):
    (tmp_path / "backup_targets.json").write_text(
        '{"devices": [{"name": "A", "cmd": "echo hi"}]}')
    (tmp_path / "sensor_targets.json").write_text(
        '{"topics": {"machines": [{"name": "B", "cmd": "echo hi"}], "storage": []}}')
    monkeypatch.setattr(sensors, "run_all",
                        lambda items: [{"name": it["name"], "output": "ok"} for it in items])

    out = asyncio.run(tools.check_status(
        {"topic": "not-a-real-topic"}, store=FakeStoreWithStatus(),
        config=_cfg(tmp_path), write_lock=None))
    assert out["topic"] == "all"  # LLM由来の不正値をコマンドに混ぜず安全側(all)へ正規化


def test_check_status_missing_topic_arg_defaults_to_all(tmp_path, monkeypatch):
    monkeypatch.setattr(sensors, "run_all", lambda items: [])
    out = asyncio.run(tools.check_status({}, store=FakeStoreWithStatus(),
                                          config=_cfg(tmp_path), write_lock=None))
    assert out["topic"] == "all"


def test_check_status_missing_ledger_reports_clearly_without_crashing(tmp_path):
    out = asyncio.run(tools.check_status(
        {"topic": "backup"}, store=FakeStoreWithStatus(), config=_cfg(tmp_path), write_lock=None))
    assert "backup_targets.json" in out["report"]
    assert "見つかりません" in out["report"]

"""config.py のデフォルト値・env 上書き・DB dir 作成・briefing_targets 初期化を検証。"""
import os, stat
from hisho_core.config import load_config, ensure_db_dir, ensure_briefing_targets

def test_defaults():
    c = load_config(env={})
    assert c.port == 51100
    assert c.ollama_host == "http://127.0.0.1:11434"
    assert c.chat_model == "gemma4:12b"
    assert c.num_ctx == 8192
    assert c.db_path.endswith("Library/Application Support/Hisho/secretary.db")
    assert c.response_reserve == 1024
    assert c.history_replay_turns == 20
    assert c.keep_alive == "30m"
    assert c.briefing_targets_path.endswith("Library/Application Support/Hisho/briefing_targets.json")

def test_reads_os_environ_by_default():
    c = load_config()
    assert isinstance(c.port, int) and c.chat_model

def test_env_override():
    c = load_config(env={"HISHO_PORT": "51200", "OLLAMA_HOST": "http://127.0.0.1:9999",
                         "HISHO_DB": "/tmp/x.db"})
    assert c.port == 51200
    assert c.ollama_host == "http://127.0.0.1:9999"
    assert c.db_path == "/tmp/x.db"

def test_ensure_db_dir_mode(tmp_path):
    db = tmp_path / "sub" / "secretary.db"
    ensure_db_dir(str(db))
    assert (tmp_path / "sub").is_dir()
    assert stat.S_IMODE(os.stat(tmp_path / "sub").st_mode) == 0o700


def test_briefing_targets_path_env_override():
    c = load_config(env={"HISHO_BRIEFING_TARGETS": "/tmp/x-briefing.json"})
    assert c.briefing_targets_path == "/tmp/x-briefing.json"


def test_ensure_briefing_targets_copies_from_example(tmp_path):
    example = tmp_path / "example.json"
    example.write_text('{"deadlines": [{"label": "テスト", "date": "2026-01-01"}]}')
    target = tmp_path / "sub" / "briefing_targets.json"
    ensure_briefing_targets(str(target), example_path=example)
    assert target.is_file()
    assert target.read_text() == example.read_text()


def test_ensure_briefing_targets_noop_when_already_exists(tmp_path):
    example = tmp_path / "example.json"
    example.write_text('{"deadlines": [{"label": "example", "date": "2026-01-01"}]}')
    target = tmp_path / "briefing_targets.json"
    target.write_text('{"deadlines": [{"label": "既存の中身", "date": "2026-02-02"}]}')
    ensure_briefing_targets(str(target), example_path=example)
    assert "既存の中身" in target.read_text()   # 上書きしない


def test_ensure_briefing_targets_noop_when_example_missing(tmp_path):
    target = tmp_path / "briefing_targets.json"
    ensure_briefing_targets(str(target), example_path=tmp_path / "does-not-exist.json")
    assert not target.exists()   # 例外にもならず、ただ何もしない


def test_ensure_briefing_targets_repo_example_file_exists_and_is_valid_json():
    """リポジトリ同梱の config/briefing_targets.example.json 自体が実在し、
    _load_json (json.loads) でパース可能な厳格 JSON であることを保証する
    (コピー後に自分自身の読み込みが失敗するような自己矛盾を防ぐ)。"""
    import json
    from hisho_core.config import _BRIEFING_EXAMPLE
    assert _BRIEFING_EXAMPLE.is_file()
    data = json.loads(_BRIEFING_EXAMPLE.read_text())
    assert isinstance(data["deadlines"], list) and len(data["deadlines"]) >= 1
    for d in data["deadlines"]:
        assert isinstance(d["label"], str) and isinstance(d["date"], str)

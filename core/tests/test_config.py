"""config.py のデフォルト値・env 上書き・DB dir 作成を検証。"""
import os, stat
from hisho_core.config import load_config, ensure_db_dir

def test_defaults():
    c = load_config(env={})
    assert c.port == 51100
    assert c.ollama_host == "http://127.0.0.1:11434"
    assert c.chat_model == "qwen3.6:35b-a3b"
    assert c.num_ctx == 8192
    assert c.db_path.endswith("Library/Application Support/Hisho/secretary.db")
    assert c.response_reserve == 1024
    assert c.history_replay_turns == 20
    assert c.keep_alive == "30m"

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

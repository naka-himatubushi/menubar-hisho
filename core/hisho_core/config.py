"""環境変数とデフォルトから不変な設定を組み立て、DB ディレクトリを用意する。"""
from __future__ import annotations
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

_DEFAULT_DB = str(Path.home() / "Library" / "Application Support" / "Hisho" / "secretary.db")

@dataclass(frozen=True)
class Config:
    port: int
    ollama_host: str
    db_path: str
    chat_model: str
    num_ctx: int
    response_reserve: int
    history_replay_turns: int
    keep_alive: str

def load_config(env: Mapping[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        port=int(e.get("HISHO_PORT", "51100")),
        ollama_host=e.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        db_path=e.get("HISHO_DB", _DEFAULT_DB),
        chat_model=e.get("HISHO_MODEL", "qwen3.6:35b-a3b"),
        num_ctx=int(e.get("HISHO_NUM_CTX", "8192")),
        response_reserve=int(e.get("HISHO_RESPONSE_RESERVE", "1024")),
        history_replay_turns=int(e.get("HISHO_HISTORY_TURNS", "20")),
        keep_alive=e.get("HISHO_KEEP_ALIVE", "30m"),
    )

def ensure_db_dir(db_path: str) -> None:
    parent = Path(db_path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o700)

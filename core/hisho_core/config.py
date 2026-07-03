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
    embed_model: str   # 埋め込みモデル名(ollama)
    rag_enabled: bool  # False なら RAG 経路をスキップ
    rag_top_k: int     # 検索で返す上位件数

def load_config(env: Mapping[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        port=int(e.get("HISHO_PORT", "51100")),
        ollama_host=e.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        db_path=e.get("HISHO_DB", _DEFAULT_DB),
        chat_model=e.get("HISHO_MODEL", "gemma4:26b-a4b-it-q8_0"),
        num_ctx=int(e.get("HISHO_NUM_CTX", "8192")),
        response_reserve=int(e.get("HISHO_RESPONSE_RESERVE", "1024")),
        history_replay_turns=int(e.get("HISHO_HISTORY_TURNS", "20")),
        keep_alive=e.get("HISHO_KEEP_ALIVE", "30m"),
        embed_model=e.get("HISHO_EMBED_MODEL", "bge-m3"),
        rag_enabled=e.get("HISHO_RAG", "1") == "1",
        rag_top_k=int(e.get("HISHO_RAG_TOP_K", "5")),
    )

def ensure_db_dir(db_path: str) -> None:
    parent = Path(db_path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o700)

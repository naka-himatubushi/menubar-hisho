"""環境変数とデフォルトから不変な設定を組み立て、DB ディレクトリを用意する。"""
from __future__ import annotations
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

logger = logging.getLogger("hisho")

_DEFAULT_DB = str(Path.home() / "Library" / "Application Support" / "Hisho" / "secretary.db")
# 書庫 (Library-DB) リポジトリの場所。library topic の `uv run jarvis find` はここを cwd に実行する
_DEFAULT_LIBRARY_DIR = str(Path.home() / "sandbox" / "library-db")
# briefing topic (朝ブリーフィング) の期限リスト。sensor_targets.json と同じ
# Application Support/Hisho/ 配下に置く流儀 (env 上書き可)
_DEFAULT_BRIEFING_TARGETS = str(Path.home() / "Library" / "Application Support" / "Hisho" / "briefing_targets.json")
# リポジトリ同梱の初期テンプレート。core/hisho_core/config.py から見て repo root/config/
_BRIEFING_EXAMPLE = Path(__file__).resolve().parent.parent.parent / "config" / "briefing_targets.example.json"

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
    library_db_dir: str  # 書庫 (Library-DB) リポジトリの場所。書庫検索 (jarvis find) の cwd
    briefing_targets_path: str  # briefing topic の期限リスト JSON のパス

def load_config(env: Mapping[str, str] | None = None) -> Config:
    e = os.environ if env is None else env
    return Config(
        port=int(e.get("HISHO_PORT", "51100")),
        ollama_host=e.get("OLLAMA_HOST", "http://127.0.0.1:11434"),
        db_path=e.get("HISHO_DB", _DEFAULT_DB),
        chat_model=e.get("HISHO_MODEL", "gemma4:12b"),
        num_ctx=int(e.get("HISHO_NUM_CTX", "8192")),
        response_reserve=int(e.get("HISHO_RESPONSE_RESERVE", "1024")),
        history_replay_turns=int(e.get("HISHO_HISTORY_TURNS", "20")),
        keep_alive=e.get("HISHO_KEEP_ALIVE", "30m"),
        embed_model=e.get("HISHO_EMBED_MODEL", "bge-m3"),
        rag_enabled=e.get("HISHO_RAG", "1") == "1",
        rag_top_k=int(e.get("HISHO_RAG_TOP_K", "5")),
        library_db_dir=e.get("HISHO_LIBRARY_DIR", _DEFAULT_LIBRARY_DIR),
        briefing_targets_path=e.get("HISHO_BRIEFING_TARGETS", _DEFAULT_BRIEFING_TARGETS),
    )

def ensure_db_dir(db_path: str) -> None:
    parent = Path(db_path).expanduser().parent
    parent.mkdir(parents=True, exist_ok=True)
    parent.chmod(0o700)


def ensure_briefing_targets(path: str, example_path: Path | str = _BRIEFING_EXAMPLE) -> None:
    """briefing_targets.json が無ければリポジトリ同梱の example からコピーして初期化する。

    backup_targets.json / sensor_targets.json は人間が手で作る前提だが、briefing の
    期限リストは初回起動で自動生成し、すぐ動く状態にする(中身は後から人間が編集する)。
    example が無い場合(パッケージ配布物には config/ が同梱されない構成もあり得る)や
    コピー中の例外は握りつぶさずログだけ残して継続する — 欠損時は sensors.deadlines_report
    が「期限リストなし」に丸めるため、ここで起動を止める理由はない。
    """
    target = Path(path).expanduser()
    if target.exists():
        return
    try:
        example = Path(example_path)
        if not example.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(example.read_text())
    except Exception:
        logger.warning("briefing_targets.json の初期化に失敗", exc_info=True)

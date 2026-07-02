"""秘書の長期記憶にプロフィール事実を種まきする CLI。

使い方:
    <python> scripts/seed_memory.py "事実その1" "事実その2" ...
    <python> scripts/seed_memory.py --file facts.txt   # 1 行 1 事実

各事実を ollama (bge-m3) で埋め込み、chunks に source_type='document' で索引する。
個人情報はこのスクリプトの引数 → ローカル DB にのみ入る (リポジトリには書かない)。
"""
from __future__ import annotations

import asyncio
import logging
import sys

sys.path.insert(0, "core")  # リポジトリ直下から実行する前提

from hisho_core import rag
from hisho_core.config import load_config
from hisho_core.store import Store

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("hisho.seed")


def _load_facts(argv: list[str]) -> list[str]:
    if len(argv) >= 2 and argv[1] == "--file":
        with open(argv[2], encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]
    return [a for a in argv[1:] if a.strip()]


async def main() -> int:
    facts = _load_facts(sys.argv)
    if not facts:
        logger.info("使い方: seed_memory.py \"事実1\" \"事実2\" ... または --file facts.txt")
        return 1

    config = load_config()
    store = Store(config.db_path)
    if not store.rag_enabled:
        logger.error("RAG 無効 (sqlite-vec ロード失敗?) — 種まき不可")
        return 1

    # document ソースの既存最大 id の続きから採番 (再実行しても重複しない)
    row = store.conn.execute(
        "SELECT COALESCE(MAX(source_id), 0) FROM chunks WHERE source_type='document'"
    ).fetchone()
    next_id = row[0] + 1

    blobs = await rag.embed(facts, model=config.embed_model, ollama_host=config.ollama_host)
    if blobs is None:
        logger.error("embedding 失敗 — ollama 稼働と bge-m3 導入を確認")
        return 1

    for i, (fact, blob) in enumerate(zip(facts, blobs)):
        store.add_chunk("document", next_id + i, None, fact,
                        blob, config.embed_model, store.vec_dim)
        logger.info("seeded: %s", fact[:50])

    total = store.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    logger.info("完了: %d 件追加 (chunks 合計 %d)", len(facts), total)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

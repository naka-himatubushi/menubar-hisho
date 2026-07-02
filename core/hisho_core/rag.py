"""RAG 層: ollama /api/embed による埋め込みと、chunks/vec0 への索引・kNN 検索。
すべて失敗安全 — embedding/検索が死んでもチャット本体は素通りで動く。"""
from __future__ import annotations

import asyncio
import logging
import struct

import anyio
import httpx

logger = logging.getLogger("hisho")


def _to_blob(floats: list[float]) -> bytes:
    return struct.pack(f"<{len(floats)}f", *floats)


async def embed(texts, *, model, ollama_host, client_factory=None):
    """texts を埋め込み float32-LE blob のリストで返す。失敗は None(例外を上げない)。"""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=30.0))
    client = factory()
    try:
        r = await client.post(f"{ollama_host}/api/embed",
                              json={"model": model, "input": list(texts)})
        if r.status_code != 200:
            logger.warning("embed failed: status %s", r.status_code)
            return None
        vecs = r.json().get("embeddings") or []
        if len(vecs) != len(texts):
            return None
        return [_to_blob(v) for v in vecs]
    except httpx.HTTPError:
        logger.warning("embed failed", exc_info=True)
        return None
    finally:
        await client.aclose()


async def index_turn(store, write_lock, turn_id, session_id, content, *,
                     config, client_factory=None) -> bool:
    """1 ターンを索引。RAG 無効・embed 失敗は False で静かに帰る。"""
    if not config.rag_enabled or not getattr(store, "rag_enabled", False):
        return False
    if len(content) < 10:
        return False
    blobs = await embed([content], model=config.embed_model,
                        ollama_host=config.ollama_host, client_factory=client_factory)
    if not blobs:
        return False
    async with write_lock:
        await anyio.to_thread.run_sync(
            store.add_chunk, "turn", turn_id, session_id, content,
            blobs[0], config.embed_model, store.vec_dim)
    return True


async def retrieve(store, user_message, *, config, exclude_session_id,
                   client_factory=None) -> list[str]:
    """user_message に関連する過去記憶 top-k の content を返す。失敗は []。"""
    if not config.rag_enabled or not getattr(store, "rag_enabled", False):
        return []
    blobs = await embed([user_message], model=config.embed_model,
                        ollama_host=config.ollama_host, client_factory=client_factory)
    if not blobs:
        return []
    hits = await anyio.to_thread.run_sync(
        lambda: store.search_chunks(blobs[0], config.rag_top_k, exclude_session_id))
    return [h["content"] for h in hits]


async def backfill(store, write_lock, *, config, batch=20, client_factory=None) -> int:
    """未索引の popover ターンをまとめて索引(起動時)。索引済み件数を返す。"""
    if not config.rag_enabled or not getattr(store, "rag_enabled", False):
        return 0
    done = 0
    while True:
        rows = await anyio.to_thread.run_sync(store.unindexed_popover_turns, batch)
        if not rows:
            return done
        for row in rows:
            ok = await index_turn(store, write_lock, row["id"], row["session_id"],
                                  row["content"], config=config,
                                  client_factory=client_factory)
            if not ok:
                return done  # ollama 死亡等 — 次回起動でリトライ
            done += 1

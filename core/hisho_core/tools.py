"""役割: 秘書 JARVIS のツール群。LLM が tool-calling で呼ぶ副作用付き操作を登録する。
現状は forget_memories (記憶の soft-delete) のみ。将来 sensors 系を REGISTRY に足す。"""
from __future__ import annotations

import logging
import time

import anyio

from . import rag

logger = logging.getLogger("hisho")

FORGET_THRESHOLD = 0.85   # 距離(L2,非正規化)。実測校正: 「猫」直接一致≈0.82, 関連≈0.55-0.80, 無関係≈0.91+。0.85 で topic を捕捉し犬/カレー等は残す。soft-delete 可逆。
MAX_FORGET = 15

TOOL_SPECS = [{
    "type": "function",
    "function": {
        "name": "forget_memories",
        "description": (
            "ユーザーが特定の記憶を明示的に「忘れて/消して/覚えなくていい」と要求した時だけ呼ぶ。"
            "query には忘れる対象を表す語句を入れる (例: 猫、私の好物)。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "忘れる対象の語句"}},
            "required": ["query"],
        },
    },
}]


async def forget_memories(args, *, store, config, write_lock, embed=rag.embed, now_ms=None):
    """query に意味マッチする active な turn/document チャンクを soft-delete する。
    戻り値: {count, matched, truncated, items} / embed 失敗時 {error, message}。"""
    query = (args or {}).get("query", "")
    query = query.strip() if isinstance(query, str) else ""
    if not query:
        return {"count": 0, "matched": 0, "truncated": False, "items": []}

    blobs = await embed([query], model=config.embed_model, ollama_host=config.ollama_host)
    if not blobs:
        logger.warning("forget: embed 失敗")
        return {"error": "embed_failed", "message": "今 記憶を整理できません"}

    hits = await anyio.to_thread.run_sync(
        lambda: store.search_forgettable(blobs[0], MAX_FORGET * 3))
    matched = [h for h in hits if h["distance"] < FORGET_THRESHOLD]
    truncated = len(matched) > MAX_FORGET
    chosen = matched[:MAX_FORGET]
    if not chosen:
        return {"count": 0, "matched": 0, "truncated": False, "items": []}

    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    chunk_ids = [h["id"] for h in chosen]
    turn_ids = [h["source_id"] for h in chosen if h["source_type"] == "turn"]
    async with write_lock:
        await anyio.to_thread.run_sync(store.soft_delete_chunks, chunk_ids, ts)
        await anyio.to_thread.run_sync(store.mark_turns_forgotten, turn_ids)

    return {
        "count": len(chosen),
        "matched": len(matched),
        "truncated": truncated,
        "items": [h["content"][:60] for h in chosen],
    }


REGISTRY = {"forget_memories": forget_memories}

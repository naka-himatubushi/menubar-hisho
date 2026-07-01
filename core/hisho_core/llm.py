"""ollama /api/chat(NDJSON) を消費し中立イベントに変換。thinking は表示/記録しない。"""
from __future__ import annotations

import json
from typing import AsyncIterator


async def iter_ollama_events(raw_lines: AsyncIterator[bytes]) -> AsyncIterator[dict]:
    """
    Ollama の /api/chat streaming レスポンス (NDJSON) を消費し、
    中立的なイベントフォーマットに変換して yield。

    - message.thinking は無視（delta として emit しない）
    - message.content から delta イベントを生成
    - done=true で done イベントを生成（done_reason → finish_reason、デフォルト "stop"）
    - error フィールドが present なら error イベントを emit して return
    - 空行はスキップ

    Args:
        raw_lines: Ollama ストリーミングレスポンスの bytes 行

    Yields:
        dict: {"type":"delta","content":str} | {"type":"done","finish_reason":str,"eval_count":int|None} | {"type":"error","message":str}
    """
    async for raw in raw_lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue

        obj = json.loads(line)

        # error フィールドが present なら error イベントを emit して終了
        if "error" in obj:
            yield {"type": "error", "message": str(obj["error"])}
            return

        # message.content から delta イベントを生成（thinking は無視）
        msg = obj.get("message") or {}
        content = msg.get("content") or ""
        if content:
            yield {"type": "delta", "content": content}

        # done=true で done イベントを生成して終了
        if obj.get("done"):
            yield {
                "type": "done",
                "finish_reason": obj.get("done_reason") or "stop",
                "eval_count": obj.get("eval_count")
            }
            return

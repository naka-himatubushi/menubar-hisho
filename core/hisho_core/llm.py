"""ollama /api/chat(NDJSON) を消費し中立イベントに変換。thinking は表示/記録しない。"""
from __future__ import annotations

import httpx
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
        dict: {"type":"delta","content":str} | {"type":"tool_call","id":str|None,"name":str|None,"arguments":dict} | {"type":"done","finish_reason":str,"eval_count":int|None} | {"type":"error","message":str}
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

        # message.tool_calls から tool_call イベントを生成
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            yield {"type": "tool_call", "id": tc.get("id"),
                   "name": fn.get("name"), "arguments": fn.get("arguments") or {}}

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


async def _as_bytes(aiter_str):
    """
    httpx.Response.aiter_lines() は str を返す（newline が削除されている）。
    iter_ollama_events に渡す前に str → bytes 変換するアダプタ。
    """
    async for s in aiter_str:
        yield (s + "\n").encode()


async def chat_stream(messages, *, model, ollama_host, num_ctx, keep_alive,
                      think: bool = False, tools=None, client_factory=None):
    """
    Ollama /api/chat にストリーミング POST を送り、イベントを yield。

    Args:
        messages: chat messages list
        model: ollama model name
        ollama_host: ollama base URL (e.g. "http://127.0.0.1:11434")
        num_ctx: context window size
        keep_alive: keep alive duration (e.g. "30m")
        think: enable thinking mode
        tools: optional Ollama tools 定義のリスト (None/空なら body に含めない)
        client_factory: optional async client factory (default: httpx.AsyncClient with custom timeout)

    Yields:
        dict: events from iter_ollama_events (delta, tool_call, done, error)
    """
    body = {
        "model": model,
        "messages": messages,
        "stream": True,
        "think": think,
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx},
    }
    if tools:
        body["tools"] = tools
    factory = client_factory or (lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)))
    client = factory()
    try:
        async with client.stream("POST", f"{ollama_host}/api/chat", json=body) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                yield {"type": "error", "message": f"ollama {resp.status_code}: {text[:200]}"}
                return
            async for evt in iter_ollama_events(_as_bytes(resp.aiter_lines())):
                yield evt
    finally:
        await client.aclose()


async def warmup(*, model, ollama_host, num_ctx, keep_alive, client_factory=None) -> bool:
    """1トークン生成でモデルを VRAM にロードし cold start を隠す。失敗は False(例外を上げない)。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx, "num_predict": 1},
    }
    factory = client_factory or (lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)))
    client = factory()
    try:
        r = await client.post(f"{ollama_host}/api/chat", json=body)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        await client.aclose()


async def unload(*, model, ollama_host, client_factory=None) -> bool:
    """keep_alive:0 で即アンロードを要求(graceful 終了時のベストエフォート)。"""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    client = factory()
    try:
        r = await client.post(f"{ollama_host}/api/generate",
                              json={"model": model, "keep_alive": 0})
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        await client.aclose()

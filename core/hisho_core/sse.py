"""OpenAI 互換 chat.completion.chunk の生成と SSE 行整形(手書きフレーミング)。

Pure functions for OpenAI-style Server-Sent Events formatting and chunk construction.
- DONE: Sentinel string for stream completion.
- sse(data): Format a dict as SSE line.
- chunk(): Build OpenAI chat.completion.chunk structure.
- error_frame(): Build OpenAI-shaped error response with param and code fields.
"""
from __future__ import annotations

import json


DONE = "data: [DONE]\n\n"


def sse(data: dict) -> str:
    """Format a dict as an SSE data line.

    Args:
        data: Dictionary to serialize.

    Returns:
        SSE-formatted line: "data: {json}\n\n"
    """
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"


def chunk(
    id: str,
    model: str,
    created: int,
    delta: dict | None,
    finish_reason: str | None,
) -> dict:
    """Build an OpenAI chat.completion.chunk structure.

    Args:
        id: Unique chunk identifier.
        model: Model name.
        created: Unix timestamp.
        delta: Content delta; None becomes {}.
        finish_reason: Completion reason or None.

    Returns:
        OpenAI-compatible chunk dict with choices array.
    """
    return {
        "id": id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": delta or {},
                "finish_reason": finish_reason,
            }
        ],
    }


def error_frame(message: str) -> dict:
    """Build an OpenAI-shaped error response.

    C4 override: includes param and code fields to match OpenAI error shape.

    Args:
        message: Human-readable error message.

    Returns:
        Error dict with type, message, param, and code.
    """
    return {
        "error": {
            "message": message,
            "type": "hisho_error",
            "param": None,
            "code": None,
        }
    }

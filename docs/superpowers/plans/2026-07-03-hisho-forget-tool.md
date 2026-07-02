# JARVIS 自己記憶削除ツール (forget) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JARVIS 自身が会話中に「忘れて」と言われたら、該当する長期記憶 (RAG chunks) を実際に soft-delete し、実削除件数を事実として報告する。

**Architecture:** Ollama native tool-calling を使ったエージェントループ。通常の streaming `/api/chat` に (忘却意図の語を含むターンだけ) `tools` を渡し、モデルが `forget_memories` を呼んだら server がツールを実行して結果を messages に注入し、2回目の streaming で最終回答を返す。削除は soft (status カラム) で可逆。

**Tech Stack:** Python 3.13 (同梱 CPython), FastAPI, httpx, SQLite (STRICT + WAL) + sqlite-vec (vec0), Ollama (`/api/chat` tools, `/api/embed` bge-m3), pytest, anyio。

## Global Constraints

- 設計仕様: `docs/specs/2026-07-02-hisho-forget-tool-design.md` (この plan の一次ソース)
- テスト実行: `core/.venv/bin/python -m pytest core/tests/ -q` (リポジトリ直下から)
- print 禁止 → `logging.getLogger("hisho")` を使う
- 例外を握りつぶさない (`except: pass` 禁止、最低 `logger.warning(..., exc_info=True)`)
- 全ファイル先頭に役割 docstring
- SQLite 書込は `app.state.write_lock` 下で `anyio.to_thread.run_sync` 経由 (既存パターン踏襲)
- soft-delete は物理削除しない (chunks.status='forgotten')。vec0/embeddings は触らない
- **既定値**: `FORGET_THRESHOLD = 0.40` (距離。小さいほど近い。高精度側=過少削除寄り。要スモーク校正)、`MAX_FORGET = 15`、`MAX_TOOL_ITERS = 3`
- **忘却意図の正規表現**: `忘れ|消し|消して|覚えなくて|削除`
- **repo ポリシー (重要)**: 実装は Sonnet サブエージェント。**サブエージェントは commit しない**。各タスクの「Commit」ステップは controller がレビュー後に実行する。サブエージェントは working tree に変更を残すだけ
- 破壊的挙動テストは実 Ollama に依存させない。`chat_fn` / `embed` は fake を注入 (既存 `FakeStreamer` 相当のパターン)

---

## File Structure

- Modify `core/hisho_core/store.py` — migration v3 (status/forgotten_at)、`search_chunks` に status フィルタ、新メソッド3つ
- Modify `core/hisho_core/llm.py` — `iter_ollama_events` に tool_call 検出、`chat_stream` に `tools` 引数
- Create `core/hisho_core/tools.py` — ツールレジストリ + `forget_memories`
- Modify `core/hisho_core/server.py` — キーワード事前ゲート、tool ループ、決定的報告行、forget 発火時の索引スキップ
- Modify `core/hisho_core/context.py` — persona 精密化
- Test: `core/tests/test_store_forget.py`, `core/tests/test_llm_tools.py`, `core/tests/test_tools.py`, `core/tests/test_server_forget.py` (既存 pytest 構成に合わせる)

---

## Task 1: migration v3 + search_chunks の status フィルタ

**Files:**
- Modify: `core/hisho_core/store.py` (`_init_rag` の v2 ブロック直後、`search_chunks` の WHERE)
- Test: `core/tests/test_store_forget.py`

**Interfaces:**
- Produces: 移行後 `chunks` に `status TEXT NOT NULL DEFAULT 'active'` と `forgotten_at INTEGER`。`search_chunks(query_vec, k, exclude_session_id=None)` は `status='active'` のみ返す (シグネチャ不変)

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_store_forget.py`:
```python
"""役割: forget 機能の store 層 (migration v3・soft-delete・replay 除外) のテスト。"""
from hisho_core.store import Store


def _mk_store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def test_migration_v3_adds_status_columns(tmp_path):
    s = _mk_store(tmp_path)
    if not s.rag_enabled:
        return  # sqlite-vec 無し環境ではスキップ
    cols = {r[1] for r in s.conn.execute("PRAGMA table_info(chunks)").fetchall()}
    assert "status" in cols
    assert "forgotten_at" in cols
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] >= 3
```

- [ ] **Step 2: 失敗を確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_store_forget.py::test_migration_v3_adds_status_columns -q`
Expected: FAIL (status カラムが無い)

- [ ] **Step 3: migration v3 を追加**

`store.py` の `_init_rag` 内、v2 ブロック (`PRAGMA user_version = 2;` の executescript) と `self.rag_enabled = True` の間に挿入:
```python
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 3:
            self.conn.executescript("""
                ALTER TABLE chunks ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
                ALTER TABLE chunks ADD COLUMN forgotten_at INTEGER;
                PRAGMA user_version = 3;
            """)
            self.conn.commit()
```

- [ ] **Step 4: パスを確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_store_forget.py::test_migration_v3_adds_status_columns -q`
Expected: PASS

- [ ] **Step 5: search_chunks に status フィルタを追加**

`store.py` の `search_chunks` の SQL、`WHERE v.embedding MATCH ? AND v.k = ?` に続けて `AND c.status = 'active'` を足す (JOIN 済み chunks c への post-filter)。
> 注 (vec0 の落とし穴): vec0 kNN は MATCH 後に post-filter するため、forgotten が上位を占めると active 結果が k 未満になり得る。retrieval では許容 (件数が減るだけ)。

- [ ] **Step 6: Commit (controller が実行)**

```bash
git add core/hisho_core/store.py core/tests/test_store_forget.py
git commit -m "feat(store): migration v3 で chunks に status/forgotten_at を追加し retrieval を active のみに"
```

---

## Task 2: store の forget メソッド3つ

**Files:**
- Modify: `core/hisho_core/store.py` (`add_chunk` 付近にメソッド追加)
- Test: `core/tests/test_store_forget.py`

**Interfaces:**
- Produces:
  - `search_forgettable(query_vec: bytes, k: int) -> list[dict]` — `status='active'` かつ `source_type IN ('turn','document')` の kNN。各 dict は `{id, content, source_type, source_id, distance}`
  - `soft_delete_chunks(chunk_ids: list[int], now_ms: int) -> None` — status='forgotten', forgotten_at=now_ms
  - `mark_turns_forgotten(turn_ids: list[int]) -> None` — turns.status='forgotten' (recent_turns の status='complete' 条件から外れる)

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_store_forget.py` に追記:
```python
import struct


def _vec(store, floats):
    return struct.pack(f"<{len(floats)}f", *floats)


def test_soft_delete_and_search_excludes(tmp_path):
    s = _mk_store(tmp_path)
    if not s.rag_enabled:
        return
    dim = s.vec_dim
    v = _vec(s, [0.1] * dim)
    cid = s.add_chunk("document", 1, None, "猫の名前はモチ", v, "bge-m3", dim)
    # soft-delete 前は search_forgettable で拾える
    hits = s.search_forgettable(v, 5)
    assert any(h["id"] == cid for h in hits)
    s.soft_delete_chunks([cid], 123)
    # 後は search_chunks / search_forgettable から消える
    assert all(h["id"] != cid for h in s.search_forgettable(v, 5))
    assert all(r["content"] != "猫の名前はモチ" for r in s.search_chunks(v, 5))
    row = s.conn.execute("SELECT status, forgotten_at FROM chunks WHERE id=?", (cid,)).fetchone()
    assert row["status"] == "forgotten" and row["forgotten_at"] == 123


def test_mark_turns_forgotten_excludes_from_replay(tmp_path):
    s = _mk_store(tmp_path)
    s.get_or_create_session("sess-1", 1)
    tid = s.append_user_turn("sess-1", "私は猫を飼っている", 1, "popover")
    assert any(t["content"] == "私は猫を飼っている" for t in s.recent_turns("sess-1", 10))
    s.mark_turns_forgotten([tid])
    assert all(t["content"] != "私は猫を飼っている" for t in s.recent_turns("sess-1", 10))
```

- [ ] **Step 2: 失敗を確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_store_forget.py -q`
Expected: FAIL (メソッド未定義)

- [ ] **Step 3: メソッドを実装**

`store.py` の `add_chunk` の後に追加:
```python
    def search_forgettable(self, query_vec: bytes, k: int) -> list[dict]:
        """忘却候補の kNN。active かつ turn/document のみ。status(現況) は対象外。"""
        rows = self.conn.execute(
            "SELECT c.id, c.content, c.source_type, c.source_id, v.distance "
            "FROM vec_chunks_bge_m3 v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND v.k = ? "
            "AND c.status = 'active' AND c.source_type IN ('turn','document') "
            "ORDER BY v.distance", (query_vec, k)).fetchall()
        return [dict(r) for r in rows]

    def soft_delete_chunks(self, chunk_ids: list[int], now_ms: int) -> None:
        """chunks を status='forgotten' に (物理削除しない)。vec0/embeddings は残す。"""
        if not chunk_ids:
            return
        ph = ",".join("?" * len(chunk_ids))
        self.conn.execute(
            f"UPDATE chunks SET status='forgotten', forgotten_at=? WHERE id IN ({ph})",
            (now_ms, *chunk_ids))
        self.conn.commit()

    def mark_turns_forgotten(self, turn_ids: list[int]) -> None:
        """turn を status='forgotten' に → recent_turns(status='complete') の replay から外す。"""
        if not turn_ids:
            return
        ph = ",".join("?" * len(turn_ids))
        self.conn.execute(
            f"UPDATE turns SET status='forgotten' WHERE id IN ({ph})", tuple(turn_ids))
        self.conn.commit()
```

- [ ] **Step 4: パスを確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_store_forget.py -q`
Expected: PASS (全 store テスト)

- [ ] **Step 5: Commit (controller)**

```bash
git add core/hisho_core/store.py core/tests/test_store_forget.py
git commit -m "feat(store): forget 用の search_forgettable / soft_delete_chunks / mark_turns_forgotten"
```

---

## Task 3: tools.py (`forget_memories` + レジストリ)

**Files:**
- Create: `core/hisho_core/tools.py`
- Test: `core/tests/test_tools.py`

**Interfaces:**
- Consumes: `store.search_forgettable`, `store.soft_delete_chunks`, `store.mark_turns_forgotten` (Task 2)、`rag.embed`
- Produces:
  - `TOOL_SPECS: list[dict]` — Ollama tools 形式
  - `async forget_memories(args: dict, *, store, config, write_lock, embed=rag.embed, now_ms=None) -> dict` — 戻り値 `{count, matched, truncated, items}` または `{error, message}`
  - `REGISTRY: dict[str, callable]` — `{"forget_memories": forget_memories}`
  - 定数 `FORGET_THRESHOLD=0.40`, `MAX_FORGET=15`

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_tools.py`:
```python
"""役割: forget_memories ツールの単体テスト (閾値・cap・turn 除外・embed 失敗)。"""
import asyncio
import struct
from hisho_core import tools
from hisho_core.config import load_config


class FakeStore:
    def __init__(self, hits):
        self._hits = hits
        self.soft_deleted = None
        self.turns_forgotten = None

    def search_forgettable(self, vec, k):
        return self._hits

    def soft_delete_chunks(self, ids, now):
        self.soft_deleted = list(ids)

    def mark_turns_forgotten(self, ids):
        self.turns_forgotten = list(ids)


class _Lock:
    async def __aenter__(self): return self
    async def __aexit__(self, *a): return False


def _cfg():
    return load_config()


async def _fake_embed_ok(texts, **kw):
    return [struct.pack("<4f", 0.1, 0.1, 0.1, 0.1)]


async def _fake_embed_fail(texts, **kw):
    return None


def test_forget_respects_threshold_and_marks_turns():
    store = FakeStore(hits=[
        {"id": 5, "content": "猫の名前はモチ", "source_type": "turn", "source_id": 13, "distance": 0.2},
        {"id": 9, "content": "遠い無関係な記憶", "source_type": "document", "source_id": 1, "distance": 0.9},
    ])
    out = asyncio.run(tools.forget_memories(
        {"query": "猫"}, store=store, config=_cfg(), write_lock=_Lock(),
        embed=_fake_embed_ok, now_ms=100))
    assert out["count"] == 1          # 0.9 は閾値超過で除外
    assert out["matched"] == 1
    assert store.soft_deleted == [5]
    assert store.turns_forgotten == [13]  # turn 由来のみ
    assert out["items"] == ["猫の名前はモチ"]


def test_forget_embed_failure_is_safe():
    store = FakeStore(hits=[])
    out = asyncio.run(tools.forget_memories(
        {"query": "猫"}, store=store, config=_cfg(), write_lock=_Lock(),
        embed=_fake_embed_fail, now_ms=100))
    assert "error" in out
    assert store.soft_deleted is None  # 何も消さない
```

- [ ] **Step 2: 失敗を確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_tools.py -q`
Expected: FAIL (`hisho_core.tools` 無し)

- [ ] **Step 3: tools.py を実装**

```python
"""役割: 秘書 JARVIS のツール群。LLM が tool-calling で呼ぶ副作用付き操作を登録する。
現状は forget_memories (記憶の soft-delete) のみ。将来 sensors 系を REGISTRY に足す。"""
from __future__ import annotations

import logging
import time

import anyio

from . import rag

logger = logging.getLogger("hisho")

FORGET_THRESHOLD = 0.40   # 距離。小さいほど近い。高精度側=過少削除寄り。要スモーク校正
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
```

- [ ] **Step 4: パスを確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_tools.py -q`
Expected: PASS

- [ ] **Step 5: Commit (controller)**

```bash
git add core/hisho_core/tools.py core/tests/test_tools.py
git commit -m "feat(tools): forget_memories ツール (閾値・cap・turn 除外・embed 安全)"
```

---

## Task 4: llm の tool イベント対応

**Files:**
- Modify: `core/hisho_core/llm.py` (`iter_ollama_events`, `chat_stream`)
- Test: `core/tests/test_llm_tools.py`

**Interfaces:**
- Produces:
  - `iter_ollama_events` が `message.tool_calls` を見たら各要素を `{"type":"tool_call","id","name","arguments"}` で yield (arguments は Ollama がパース済みの dict)
  - `chat_stream(..., tools=None, ...)` — `tools` があれば body に含める。無ければ従来と同一

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_llm_tools.py`:
```python
"""役割: Ollama tool_calls → tool_call イベント変換と、chat_stream の tools 引数のテスト。"""
import asyncio
import json
from hisho_core import llm


async def _collect(aiter):
    return [e async for e in aiter]


async def _lines(objs):
    for o in objs:
        yield (json.dumps(o) + "\n").encode()


def test_tool_calls_become_tool_call_events():
    objs = [
        {"message": {"tool_calls": [
            {"id": "call_1", "function": {"name": "forget_memories", "arguments": {"query": "猫"}}}]}},
        {"message": {"content": ""}, "done": True, "done_reason": "tool_calls"},
    ]
    events = asyncio.run(_collect(llm.iter_ollama_events(_lines(objs))))
    tc = [e for e in events if e["type"] == "tool_call"]
    assert len(tc) == 1
    assert tc[0]["name"] == "forget_memories"
    assert tc[0]["arguments"] == {"query": "猫"}
    assert any(e["type"] == "done" for e in events)


def test_plain_content_still_delta():
    objs = [{"message": {"content": "はい"}}, {"message": {"content": ""}, "done": True}]
    events = asyncio.run(_collect(llm.iter_ollama_events(_lines(objs))))
    assert [e for e in events if e["type"] == "delta"][0]["content"] == "はい"
```

- [ ] **Step 2: 失敗を確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_llm_tools.py -q`
Expected: FAIL (tool_call イベントが出ない)

- [ ] **Step 3: iter_ollama_events に tool_call 検出を追加**

`llm.py` の `iter_ollama_events`、`msg = obj.get("message") or {}` の直後、content 処理の前に挿入:
```python
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            yield {"type": "tool_call", "id": tc.get("id"),
                   "name": fn.get("name"), "arguments": fn.get("arguments") or {}}
```

- [ ] **Step 4: chat_stream に tools 引数を追加**

`chat_stream` のシグネチャに `tools=None` を追加 (`think` の後、`client_factory` の前)。body 組立の後に:
```python
    if tools:
        body["tools"] = tools
```

- [ ] **Step 5: パスを確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_llm_tools.py -q`
Expected: PASS

- [ ] **Step 6: Commit (controller)**

```bash
git add core/hisho_core/llm.py core/tests/test_llm_tools.py
git commit -m "feat(llm): tool_calls を tool_call イベント化し chat_stream に tools 引数"
```

---

## Task 5: server のツールループ + キーワードゲート + 決定的報告 + 索引スキップ

**Files:**
- Modify: `core/hisho_core/server.py` (import、`chat` ハンドラ、`gen`)
- Test: `core/tests/test_server_forget.py`

**Interfaces:**
- Consumes: `tools.TOOL_SPECS`, `tools.REGISTRY` (Task 3)、`chat_fn(..., tools=...)` (Task 4)
- Produces: popover かつ忘却語マッチ時のみ tools を渡す。tool_call → 実行 → 結果注入 → 再 stream。forget 発火時は `_index_pair` スキップ + 末尾に `[記憶を N件 忘れました]` を決定的付加。`MAX_TOOL_ITERS=3`

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_server_forget.py`:
```python
"""役割: server のツールループ (キーワードゲート・実行・決定的報告・索引スキップ) の統合テスト。"""
import json
from fastapi.testclient import TestClient
from hisho_core.server import create_app
from hisho_core.config import load_config
from hisho_core.store import Store


def _sse_text(resp_bytes):
    return resp_bytes.decode("utf-8", "replace")


def _make_chat_fn(script):
    """script: 呼び出し回数ごとに返すイベント列のリスト。tools 受領を記録。"""
    calls = {"tools_seen": [], "n": 0}

    async def chat_fn(messages, *, model, ollama_host, num_ctx, keep_alive, think=False, tools=None):
        calls["tools_seen"].append(tools)
        seq = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for e in seq:
            yield e

    return chat_fn, calls


def test_no_forget_keyword_passes_no_tools(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn([[{"type": "delta", "content": "はい"}, {"type": "done", "finish_reason": "stop"}]])
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s1", "messages": [{"role": "user", "content": "今日の天気は"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert calls["tools_seen"][0] is None  # 忘却語なし → tools 渡さない


def test_forget_keyword_runs_tool_and_appends_deterministic_line(tmp_path):
    store = Store(str(tmp_path / "t.db"))
    if not store.rag_enabled:
        return
    # 1回目: tool_call を返す / 2回目: 最終回答
    script = [
        [{"type": "tool_call", "id": "c1", "name": "forget_memories", "arguments": {"query": "猫"}},
         {"type": "done", "finish_reason": "tool_calls"}],
        [{"type": "delta", "content": "承知しました"}, {"type": "done", "finish_reason": "stop"}],
    ]
    chat_fn, calls = _make_chat_fn(script)
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    # forget_memories を件数固定の fake に差し替え (embed/DB に依存させない)
    import hisho_core.tools as toolsmod

    async def fake_forget(args, **kw):
        return {"count": 2, "matched": 2, "truncated": False, "items": ["猫A", "猫B"]}

    app.state.tool_registry = {"forget_memories": fake_forget}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions", json={"session_id": "s2", "messages": [{"role": "user", "content": "猫のこと忘れて"}]},
                   headers={"X-Hisho-Source": "popover"})
        body = _sse_text(r.content)
    assert calls["tools_seen"][0] is not None      # 1回目は tools 渡す
    assert "[記憶を 2件 忘れました]" in body        # 決定的報告
    assert "承知しました" in body                   # モデルの最終回答も出る


# --- テスト用ヘルパ ---
async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True
```

- [ ] **Step 2: 失敗を確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_server_forget.py -q`
Expected: FAIL (tools 未対応・決定的行なし)

- [ ] **Step 3: server に import とゲートを追加**

`server.py` 冒頭の import 群に:
```python
import re
from . import tools as tools_module
```
`create_app` 内、`app.state.write_lock = asyncio.Lock()` の近くに:
```python
    app.state.tool_registry = tools_module.REGISTRY
    app.state.forget_intent = re.compile(r"忘れ|消し|消して|覚えなくて|削除")
```

- [ ] **Step 4: chat ハンドラで tools を条件付きで決定**

`chat` ハンドラ、`messages` を組んだ後 (popover 分岐の後)、`assistant_id` を作る前に:
```python
        use_tools = (
            tools_module.TOOL_SPECS
            if source == "popover" and app.state.forget_intent.search(user_message)
            else None)
```

- [ ] **Step 5: gen() をツールループに書き換える**

`gen()` を以下に置換 (既存の finalize/index の finally 構造は維持しつつ、stream をループ化):
```python
        async def gen():
            acc: list[str] = []
            status = "partial"
            finish = "stop"
            forget_fired = False
            forget_count = 0
            convo = list(messages)  # tool ラウンドで伸ばす作業用コピー

            async def _index_pair():
                try:
                    await rag.index_turn(store, app.state.write_lock, user_turn_id,
                                         session_id, user_message, config=cfg)
                    text = "".join(acc)
                    if text:
                        await rag.index_turn(store, app.state.write_lock, assistant_id,
                                             session_id, text, config=cfg)
                except Exception:
                    logger.warning("index after chat failed", exc_info=True)

            try:
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={"role": "assistant"}, finish_reason=None))
                for _ in range(tools_module_MAX := 3):  # MAX_TOOL_ITERS
                    pending_tool = None
                    async for evt in app.state.chat_fn(
                            convo, model=model, ollama_host=cfg.ollama_host,
                            num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False,
                            tools=use_tools):
                        if evt["type"] == "delta":
                            acc.append(evt["content"])
                            yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": evt["content"]}, finish_reason=None))
                        elif evt["type"] == "tool_call":
                            pending_tool = evt
                        elif evt["type"] == "error":
                            status = "error"
                            yield sse(error_frame(evt["message"]))
                            return
                        elif evt["type"] == "done":
                            finish = evt.get("finish_reason", "stop")
                    if pending_tool is None:
                        break  # ツール呼び出しなし = 通常の最終回答で完了
                    fn = app.state.tool_registry.get(pending_tool["name"])
                    if fn is None:
                        break
                    result = await fn(pending_tool.get("arguments") or {},
                                      store=store, config=cfg, write_lock=app.state.write_lock)
                    if pending_tool["name"] == "forget_memories" and "error" not in result:
                        forget_fired = True
                        forget_count = result.get("count", 0)
                    convo = convo + [
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": pending_tool.get("id"), "type": "function",
                             "function": {"name": pending_tool["name"],
                                          "arguments": pending_tool.get("arguments") or {}}}]},
                        {"role": "tool", "content": __import__("json").dumps(result, ensure_ascii=False)},
                    ]
                    use_tools = None  # 2周目以降はツール無し (無限ループ防止の一助)
                if forget_fired:
                    line = f"\n\n[記憶を {forget_count}件 忘れました]"
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason=finish))
                yield DONE
                status = "complete"
            except asyncio.CancelledError:
                raise
            except Exception as ex:
                status = "error"
                try:
                    yield sse(error_frame(str(ex)))
                except Exception:
                    logger.warning("failed to send error frame to client", exc_info=True)
            finally:
                with anyio.CancelScope(shield=True):
                    async with app.state.write_lock:
                        await anyio.to_thread.run_sync(store.finalize_turn, assistant_id, "".join(acc), None, status, _now_ms())
                        await anyio.to_thread.run_sync(store.touch_session, session_id, _now_ms())
                if source == "popover" and not forget_fired:   # H1: 忘却往復は索引しない
                    asyncio.create_task(_index_pair())
```
> 注: `tools_module_MAX := 3` は `MAX_TOOL_ITERS`。定数化して `tools_module` かローカルに置いても良い (readability)。`json.dumps` は先頭 import 済みなら `json.dumps` を直接使う (`__import__` は避け、ファイル冒頭の `import` を使う)。

- [ ] **Step 6: パスを確認 + 全体回帰**

Run: `core/.venv/bin/python -m pytest core/tests/test_server_forget.py -q`
Expected: PASS
Run: `core/.venv/bin/python -m pytest core/tests/ -q`
Expected: 既存 62 + 新規すべて PASS (回帰なし)

- [ ] **Step 7: Commit (controller)**

```bash
git add core/hisho_core/server.py core/tests/test_server_forget.py
git commit -m "feat(server): forget ツールループ・キーワードゲート・決定的報告・索引スキップ"
```

---

## Task 6: persona 精密化

**Files:**
- Modify: `core/hisho_core/context.py` (`PERSONA`)
- Test: なし (文字列変更。既存 context テストが壊れないことだけ確認)

**Interfaces:**
- Produces: PERSONA の「手段を持ちません」を「忘却は実行できる/それ以外は未実装で装わない」に置換

- [ ] **Step 1: PERSONA を修正**

`context.py` の PERSONA、以下の2文
```
"あなたはコマンド実行やリアルタイム確認の手段を持ちません。「確認しました」等、"
"実行したかのような表現は使わず、根拠は与えられたメモの内容と収集時刻だけを述べます。"
```
を次に置換:
```python
    "記憶の忘却は forget 機能で実際に実行でき、実行後は結果 (件数) を事実として報告します。"
    "それ以外の実行手段 (バックアップ起動やリアルタイム確認など) はまだ持たないため、"
    "「確認しました」等の実行したかのような表現は使わず、根拠は与えられたメモの内容と収集時刻だけを述べます。"
```

- [ ] **Step 2: 回帰確認**

Run: `core/.venv/bin/python -m pytest core/tests/ -q`
Expected: 全 PASS (context テストが PERSONA 文言に過度に依存していないこと。壊れたら該当 assert を新文言に更新)

- [ ] **Step 3: Commit (controller)**

```bash
git add core/hisho_core/context.py
git commit -m "feat(context): persona を忘却実行可・他は未実装と明示に精密化"
```

---

## Task 7: スモーク + 手動 e2e (tool-calling 信頼性の実測)

**Files:**
- 変更なし (実測タスク)。結果は `core/SMOKE.md` に追記

**Interfaces:**
- Consumes: 完成した .app / core

- [ ] **Step 1: core 同梱ツリー再ビルド + .app リビルド**

Run:
```bash
cd ~/sandbox/menubar-hisho && scripts/build_core.sh \
 && (cd HishoApp && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodegen generate) \
 && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project HishoApp/HishoApp.xcodeproj -scheme HishoApp -configuration Debug -derivedDataPath build/derived build \
 && killall Hisho 2>/dev/null; open build/derived/Build/Products/Debug/Hisho.app
```
Expected: `** BUILD SUCCEEDED **`、メニューバーに JARVIS
> 注: core を変えたので `build_core.sh` 必須 (忘れると古いツリーを embed)。

- [ ] **Step 2: tool-calling 信頼性スモーク (qwen3.6:35b-a3b)**

popover で以下を試し、`~/Library/Application Support/Hisho/secretary.db` を確認:
- 「猫のこと忘れて」→ tool 発火し `[記憶を N件 忘れました]` が出るか。DB で該当 chunks が status='forgotten' か
- 「今日は良い天気だね」→ tool が誤爆しない (忘却語ゲートで tools を渡さない)
- FORGET_THRESHOLD の校正: 「猫」で狙ったチャンクが拾え、無関係が巻き込まれない距離か。必要なら `tools.py` の閾値を調整して再ビルド

- [ ] **Step 3: 結果を SMOKE.md に記録**

`core/SMOKE.md` に「forget ツール tool-calling 実測 (発火率・誤爆・閾値の決定値)」を追記。false-negative (呼ぶべき時に呼ばない) が頻発するなら §8 のフォールバック (ゲート済みターンで tool 強制 or キーワード直実行) を別タスクで検討する旨も記載。

- [ ] **Step 4: Commit (controller)**

```bash
git add core/SMOKE.md core/hisho_core/tools.py
git commit -m "chore: forget tool-calling スモーク結果と閾値校正を記録"
```

---

## Self-Review (この plan を spec と突き合わせた結果)

- **Spec §4 (migration)** → Task 1 ✓ / **§5 (forget tool)** → Task 3 + Task 2 (store) ✓ / **§6 (機構: ループ M2/M3/H1/write_lock)** → Task 5 + Task 4 (llm) ✓ / **§7 (persona)** → Task 6 ✓ / **§8 (失敗条件)** → キーワードゲート=Task5, embed 安全=Task3, replay 除外=Task2, 再汚染防止=Task5 ✓ / **§9 (テスト)** → 各 Task の Step1 ✓ / スモーク=Task 7 ✓
- **Placeholder scan**: なし (全ステップに実コード)。唯一 Task5 の `tools_module_MAX := 3` は可読性のため定数化推奨と注記済
- **型整合**: `search_forgettable` の戻り (id/content/source_type/source_id/distance) は Task2 定義 ↔ Task3 消費で一致。`forget_memories(args, *, store, config, write_lock, ...)` は Task3 定義 ↔ Task5 呼出で一致。`chat_stream(..., tools=None)` は Task4 定義 ↔ Task5 呼出で一致。tool_call イベント `{type,id,name,arguments}` は Task4 産出 ↔ Task5 消費で一致
- **既知の実装注意** (executor へ): Task5 の gen() 置換は既存の finally/shield 構造を保つこと。`json` はファイル冒頭 import を使う。vec0 post-filter で結果が k 未満になり得る点は Task1 注記済

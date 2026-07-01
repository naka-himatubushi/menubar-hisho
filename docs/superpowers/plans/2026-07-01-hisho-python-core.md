# Hisho Python Core 実装プラン (Plan 1 / 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **実装ポリシー:** plan=Opus / 実装=Sonnet・Haiku サブエージェント。**サブエージェントは commit しない**(step の commit はレビュー通過後に親が実行、または明示許可時のみ)。

**Goal:** ローカル ollama の前に立ち、全会話を SQLite に記録しながら OpenAI 互換 SSE を吐くローカル秘書サーバ (`hisho_core`) を単体で完動させる。

**Architecture:** FastAPI + uvicorn を `127.0.0.1:51100` に常駐。`server.py` がオーケストレーション、`llm.py` が ollama `/api/chat`(NDJSON) を消費して OpenAI SSE を再発行、`store.py` が SQLite(WAL) の唯一のスキーマ所有者。純粋関数(NDJSON パーサ・SSE 整形・コンテキスト合成)を I/O から分離し、ネットワーク無しでテストする。

**Tech Stack:** Python 3.13(将来 .app に python-build-standalone を同梱)、FastAPI、uvicorn(uvloop+httptools)、httpx、標準ライブラリ `sqlite3`、pytest + pytest-asyncio + httpx ASGITransport。SSE は手書き。ORM/クライアント SDK 不使用。

## Global Constraints

- Python **3.13**。deps は `fastapi` + `uvicorn`(uvloop, httptools のみ) + `httpx`。テストのみ `pytest`, `pytest-asyncio`。
- サーバは **`127.0.0.1` のみ bind**(`0.0.0.0` 禁止)。認証なし(loopback が境界)。
- チャットモデルは単一設定値 **`qwen3.6:35b-a3b`**(実 pull 済 ~23GB)。host/model は `llm.py`/`config.py` の一箇所のみ。
- ollama は **native `/api/chat` を消費**し、core は **独自 OpenAI SSE を再発行**。
- **推論モデル対策**: 既定 `think:false`。`thinking` 内容は表示にもログにも**絶対に連結しない**。ログは `message.content` のみ。
- **num_ctx を毎回明示**(既定 8192)。履歴は budget 内に切り詰め、system/persona と直近を必ず残す。
- **全ターンを SQLite に記録**。user は呼び出し前に同期記録、assistant はプレースホルダ→stream 終了後に `finally` で確定(正常=complete / 切断=partial / 例外=error)。
- DB は `~/Library/Application Support/Hisho/secretary.db`、dir mode `700`。WAL + `busy_timeout=5000` + `synchronous=NORMAL`。
- `X-Hisho-Source: popover` の時だけ秘書挙動(persona+履歴)をサーバ側合成。無い時は passthrough で `source='external'` 記録。
- タイムスタンプは INTEGER unix-epoch-millis(UTC)。
- 成果物に実名を書かない(persona は「ユーザー専属の秘書」等)。

---

### Task 1: プロジェクト雛形 + config.py

**Files:**
- Create: `core/pyproject.toml`
- Create: `core/hisho_core/__init__.py`
- Create: `core/hisho_core/config.py`
- Test: `core/tests/test_config.py`

**Interfaces:**
- Produces: `Config` (frozen dataclass) with fields `port:int`, `ollama_host:str`, `db_path:str`, `chat_model:str`, `num_ctx:int`, `response_reserve:int`, `history_replay_turns:int`, `keep_alive:str`. `load_config(env: Mapping[str,str] | None = None) -> Config`. `ensure_db_dir(db_path: str) -> None` (creates parent dir mode 0o700).

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_config.py
"""config.py のデフォルト値・env 上書き・DB dir 作成を検証。"""
import os, stat
from hisho_core.config import load_config, ensure_db_dir

def test_defaults():
    c = load_config(env={})
    assert c.port == 51100
    assert c.ollama_host == "http://127.0.0.1:11434"
    assert c.chat_model == "qwen3.6:35b-a3b"
    assert c.num_ctx == 8192
    assert c.db_path.endswith("Library/Application Support/Hisho/secretary.db")

def test_env_override():
    c = load_config(env={"HISHO_PORT": "51200", "OLLAMA_HOST": "http://127.0.0.1:9999",
                         "HISHO_DB": "/tmp/x.db"})
    assert c.port == 51200
    assert c.ollama_host == "http://127.0.0.1:9999"
    assert c.db_path == "/tmp/x.db"

def test_ensure_db_dir_mode(tmp_path):
    db = tmp_path / "sub" / "secretary.db"
    ensure_db_dir(str(db))
    assert (tmp_path / "sub").is_dir()
    assert stat.S_IMODE(os.stat(tmp_path / "sub").st_mode) == 0o700
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_config.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.config`)

- [ ] **Step 3: Write pyproject + package init**

```toml
# core/pyproject.toml
[project]
name = "hisho-core"
version = "0.1.0"
requires-python = ">=3.13"
dependencies = ["fastapi", "uvicorn", "httpx"]

[project.optional-dependencies]
dev = ["pytest", "pytest-asyncio"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
```

```python
# core/hisho_core/__init__.py
"""Hisho の Python core: ローカル ollama の前に立つ秘書サーバ (記録+OpenAI互換SSE)。"""
```

- [ ] **Step 4: Write config.py**

```python
# core/hisho_core/config.py
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
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed)

- [ ] **Step 6: Commit**

```bash
git add core/pyproject.toml core/hisho_core/__init__.py core/hisho_core/config.py core/tests/test_config.py
git commit -m "feat(core): config loader + db dir bootstrap"
```

---

### Task 2: store.py — スキーマ + PRAGMA

**Files:**
- Create: `core/hisho_core/store.py`
- Test: `core/tests/test_store_schema.py`

**Interfaces:**
- Produces: `Store(db_path: str)` — 開くと PRAGMA 適用 + スキーマ作成(冪等)。属性 `conn: sqlite3.Connection`。`close() -> None`。`user_version() -> int`。schema は spec §10 の v1(sessions, turns, uq index, activity index, turns_fts + 同期トリガ)。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_store_schema.py
"""Store 初期化で WAL・STRICT スキーマ・FTS・user_version が整うことを検証。"""
from hisho_core.store import Store

def test_schema_and_pragmas(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    assert s.conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    assert {"sessions", "turns", "turns_fts"} <= tables
    assert s.user_version() == 1
    s.close()

def test_idempotent_open(tmp_path):
    p = str(tmp_path / "t.db")
    Store(p).close()
    s = Store(p)  # 再オープンで壊れない
    assert s.user_version() == 1
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_store_schema.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.store`)

- [ ] **Step 3: Write store.py (schema + pragmas only)**

```python
# core/hisho_core/store.py
"""SQLite の唯一のスキーマ所有者。会話ターンの記録・取得を担う(WAL, STRICT)。"""
from __future__ import annotations
import sqlite3

_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, title TEXT,
    created_at INTEGER NOT NULL, last_activity INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete',
    model TEXT, token_count INTEGER,
    created_at INTEGER NOT NULL, completed_at INTEGER,
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_turns_session_seq ON turns(session_id, seq);
CREATE INDEX IF NOT EXISTS idx_sessions_activity ON sessions(last_activity DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    content, content='turns', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS turns_ai AFTER INSERT ON turns BEGIN
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS turns_ad AFTER DELETE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
END;
CREATE TRIGGER IF NOT EXISTS turns_au AFTER UPDATE ON turns BEGIN
    INSERT INTO turns_fts(turns_fts, rowid, content) VALUES('delete', old.id, old.content);
    INSERT INTO turns_fts(rowid, content) VALUES (new.id, new.content);
END;
"""

class Store:
    def __init__(self, db_path: str):
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._bootstrap()

    def _bootstrap(self) -> None:
        c = self.conn
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("PRAGMA foreign_keys = ON")
        c.execute("PRAGMA busy_timeout = 5000")
        c.execute("PRAGMA synchronous = NORMAL")
        c.executescript(_SCHEMA_V1)
        if self.user_version() < 1:
            c.execute("PRAGMA user_version = 1")
        c.commit()

    def user_version(self) -> int:
        return self.conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.conn.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_store_schema.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/store.py core/tests/test_store_schema.py
git commit -m "feat(core): sqlite schema v1 (WAL, STRICT, fts5)"
```

---

### Task 3: store.py — ターンのライフサイクル

**Files:**
- Modify: `core/hisho_core/store.py`
- Test: `core/tests/test_store_turns.py`

**Interfaces:**
- Produces (methods on `Store`):
  - `get_or_create_session(session_id: str, now_ms: int) -> None`
  - `next_seq(session_id: str) -> int` (1-based; 既存最大 seq + 1)
  - `append_user_turn(session_id: str, content: str, now_ms: int, source: str) -> int` (returns turn id; status='complete')
  - `add_assistant_placeholder(session_id: str, model: str, now_ms: int) -> int` (status='streaming', content='')
  - `finalize_turn(turn_id: int, content: str, token_count: int | None, status: str, completed_at_ms: int) -> None`
  - `recent_turns(session_id: str, limit: int) -> list[dict]` (古→新、各 `{role, content}`; status='complete' のみ)
  - `touch_session(session_id: str, now_ms: int) -> None`

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_store_turns.py
"""ターン記録の一連(user同期→assistantプレースホルダ→finalize)と取得を検証。"""
from hisho_core.store import Store

def _store(tmp_path):
    return Store(str(tmp_path / "t.db"))

def test_turn_lifecycle(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess1", 1000)
    assert s.next_seq("sess1") == 1
    uid = s.append_user_turn("sess1", "こんにちは", 1000, source="popover")
    aid = s.add_assistant_placeholder("sess1", "qwen3.6:35b-a3b", 1001)
    # プレースホルダは streaming で recent に出ない
    assert [t["content"] for t in s.recent_turns("sess1", 10)] == ["こんにちは"]
    s.finalize_turn(aid, "やあ", token_count=3, status="complete", completed_at_ms=1002)
    rows = s.recent_turns("sess1", 10)
    assert [(r["role"], r["content"]) for r in rows] == [("user", "こんにちは"), ("assistant", "やあ")]
    assert s.next_seq("sess1") == 3
    s.close()

def test_partial_on_disconnect(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("s", 1)
    aid = s.add_assistant_placeholder("s", "m", 2)
    s.finalize_turn(aid, "途中まで", token_count=None, status="partial", completed_at_ms=3)
    row = s.conn.execute("SELECT status, content FROM turns WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "partial" and row["content"] == "途中まで"
    s.close()

def test_source_recorded_in_meta(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("s", 1)
    uid = s.append_user_turn("s", "hi", 1, source="external")
    meta = s.conn.execute("SELECT meta FROM turns WHERE id=?", (uid,)).fetchone()["meta"]
    assert '"source"' in meta and "external" in meta
    s.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_store_turns.py -v`
Expected: FAIL (`AttributeError: 'Store' object has no attribute 'get_or_create_session'`)

- [ ] **Step 3: Add methods to store.py**

```python
# core/hisho_core/store.py — Store クラスに追記
    import json as _json_unused  # (ファイル先頭に `import json` を追加すること)

    def get_or_create_session(self, session_id: str, now_ms: int) -> None:
        self.conn.execute(
            "INSERT INTO sessions(id, created_at, last_activity) VALUES(?,?,?) "
            "ON CONFLICT(id) DO NOTHING", (session_id, now_ms, now_ms))
        self.conn.commit()

    def next_seq(self, session_id: str) -> int:
        row = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM turns WHERE session_id=?",
            (session_id,)).fetchone()
        return row["n"]

    def append_user_turn(self, session_id: str, content: str, now_ms: int, source: str) -> int:
        import json
        seq = self.next_seq(session_id)
        cur = self.conn.execute(
            "INSERT INTO turns(session_id, seq, role, content, status, created_at, completed_at, meta) "
            "VALUES(?,?,?,?, 'complete', ?, ?, ?)",
            (session_id, seq, "user", content, now_ms, now_ms, json.dumps({"source": source})))
        self.conn.commit()
        return cur.lastrowid

    def add_assistant_placeholder(self, session_id: str, model: str, now_ms: int) -> int:
        seq = self.next_seq(session_id)
        cur = self.conn.execute(
            "INSERT INTO turns(session_id, seq, role, content, status, model, created_at) "
            "VALUES(?,?,?, '', 'streaming', ?, ?)",
            (session_id, seq, "assistant", model, now_ms))
        self.conn.commit()
        return cur.lastrowid

    def finalize_turn(self, turn_id: int, content: str, token_count, status: str,
                      completed_at_ms: int) -> None:
        self.conn.execute(
            "UPDATE turns SET content=?, token_count=?, status=?, completed_at=? WHERE id=?",
            (content, token_count, status, completed_at_ms, turn_id))
        self.conn.commit()

    def recent_turns(self, session_id: str, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT role, content FROM turns WHERE session_id=? AND status='complete' "
            "ORDER BY seq DESC LIMIT ?", (session_id, limit)).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def touch_session(self, session_id: str, now_ms: int) -> None:
        self.conn.execute("UPDATE sessions SET last_activity=? WHERE id=?", (now_ms, session_id))
        self.conn.commit()
```
> 注: ファイル先頭の import 群に `import json` を追加。上の `_json_unused` 行は書かない(説明用)。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_store_turns.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/store.py core/tests/test_store_turns.py
git commit -m "feat(core): turn record lifecycle (user sync, assistant placeholder+finalize)"
```

---

### Task 4: context.py — persona + 履歴切り詰め

**Files:**
- Create: `core/hisho_core/context.py`
- Test: `core/tests/test_context.py`

**Interfaces:**
- Produces: `PERSONA: str`(秘書 system prompt、実名なし)。`approx_tokens(text: str) -> int`。`build_messages(recent: list[dict], user_message: str, num_ctx: int, response_reserve: int, persona: str = PERSONA) -> list[dict]` — 先頭に `{"role":"system","content":persona}`、その後 budget(=num_ctx - response_reserve)内で**新しい方から**採用した recent、末尾に `{"role":"user","content":user_message}`。system と user は必ず残す。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_context.py
"""persona 常在・budget 切り詰め・順序(system→履歴古新→user)を検証。"""
from hisho_core.context import build_messages, PERSONA, approx_tokens

def test_persona_and_user_always_present():
    msgs = build_messages([], "質問です", num_ctx=8192, response_reserve=1024)
    assert msgs[0]["role"] == "system" and msgs[0]["content"] == PERSONA
    assert msgs[-1] == {"role": "user", "content": "質問です"}

def test_order_oldest_to_newest():
    recent = [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]
    msgs = build_messages(recent, "C", num_ctx=8192, response_reserve=1024)
    assert [m["content"] for m in msgs] == [PERSONA, "A", "B", "C"]

def test_truncates_oldest_when_over_budget():
    # 小さい budget で古い履歴が落ち、system と user は残る
    big = "x" * 400
    recent = [{"role": "user", "content": big + "_old"},
              {"role": "assistant", "content": big + "_new"}]
    msgs = build_messages(recent, "now", num_ctx=300, response_reserve=100)  # budget=200 tokens
    contents = [m["content"] for m in msgs]
    assert contents[0] == PERSONA and contents[-1] == "now"
    assert (big + "_old") not in contents  # 最古が落ちる
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_context.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.context`)

- [ ] **Step 3: Write context.py**

```python
# core/hisho_core/context.py
"""秘書 persona と、num_ctx 予算内に履歴を切り詰めるメッセージ合成(純粋関数)。"""
from __future__ import annotations

PERSONA = (
    "あなたはユーザー専属の秘書アシスタント「Hisho」です。"
    "簡潔で丁寧、要点先出し。分からないことは推測せず確認します。"
    "日本語で応答します。"
)

def approx_tokens(text: str) -> int:
    # 日本語混在の粗い近似。正確なトークナイザは持たないため保守的に3文字≒1token。
    return len(text) // 3 + 1

def build_messages(recent, user_message, num_ctx, response_reserve, persona=PERSONA):
    budget = num_ctx - response_reserve
    system_msg = {"role": "system", "content": persona}
    user_msg = {"role": "user", "content": user_message}
    used = approx_tokens(persona) + approx_tokens(user_message)
    kept: list[dict] = []
    for m in reversed(recent):  # 新しい方から詰める
        t = approx_tokens(m["content"])
        if used + t > budget:
            break
        kept.append({"role": m["role"], "content": m["content"]})
        used += t
    kept.reverse()  # 古→新に戻す
    return [system_msg, *kept, user_msg]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_context.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/context.py core/tests/test_context.py
git commit -m "feat(core): secretary persona + budget-bounded message builder"
```

---

### Task 5: sse.py — OpenAI SSE 整形

**Files:**
- Create: `core/hisho_core/sse.py`
- Test: `core/tests/test_sse.py`

**Interfaces:**
- Produces: `DONE: str` (= `"data: [DONE]\n\n"`)。`sse(data: dict) -> str` (= `f"data: {json.dumps(data, ensure_ascii=False)}\n\n"`)。`chunk(id: str, model: str, created: int, delta: dict | None, finish_reason: str | None) -> dict` — OpenAI `chat.completion.chunk` を組む。`error_frame(message: str) -> dict`。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_sse.py
"""OpenAI chunk 構造と SSE 整形・[DONE]・error frame を検証。"""
import json
from hisho_core.sse import sse, chunk, DONE, error_frame

def test_sse_frame_format():
    line = sse({"a": 1})
    assert line == 'data: {"a": 1}\n\n'

def test_done_sentinel():
    assert DONE == "data: [DONE]\n\n"

def test_chunk_shape_first_delta():
    c = chunk("id1", "m", 100, delta={"role": "assistant"}, finish_reason=None)
    assert c["object"] == "chat.completion.chunk"
    assert c["id"] == "id1" and c["model"] == "m" and c["created"] == 100
    assert c["choices"][0]["delta"] == {"role": "assistant"}
    assert c["choices"][0]["finish_reason"] is None

def test_chunk_final_has_finish_reason():
    c = chunk("id1", "m", 100, delta={}, finish_reason="stop")
    assert c["choices"][0]["finish_reason"] == "stop"

def test_error_frame():
    assert error_frame("boom")["error"]["message"] == "boom"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_sse.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.sse`)

- [ ] **Step 3: Write sse.py**

```python
# core/hisho_core/sse.py
"""OpenAI 互換 chat.completion.chunk の生成と SSE 行整形(手書きフレーミング)。"""
from __future__ import annotations
import json

DONE = "data: [DONE]\n\n"

def sse(data: dict) -> str:
    return f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

def chunk(id: str, model: str, created: int, delta: dict | None, finish_reason):
    return {
        "id": id, "object": "chat.completion.chunk", "created": created, "model": model,
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish_reason}],
    }

def error_frame(message: str) -> dict:
    return {"error": {"message": message, "type": "hisho_error"}}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_sse.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/sse.py core/tests/test_sse.py
git commit -m "feat(core): openai-compatible sse framing helpers"
```

---

### Task 6: llm.py — ollama NDJSON パーサ(純粋)

**Files:**
- Create: `core/hisho_core/llm.py`
- Test: `core/tests/test_llm_parser.py`

**Interfaces:**
- Produces: `async def iter_ollama_events(raw_lines: AsyncIterator[bytes]) -> AsyncIterator[dict]` — ollama `/api/chat` の NDJSON 行(bytes)を消費し、`{"type":"delta","content":str}` / `{"type":"done","finish_reason":str,"eval_count":int|None}` / `{"type":"error","message":str}` を yield。**`message.thinking` は無視**(delta にしない)。空行はスキップ。`done_reason` → `finish_reason`(既定 "stop")。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_llm_parser.py
"""ollama NDJSON → 中立イベント。thinking除去・delta・done・error を検証。"""
import json, pytest
from hisho_core.llm import iter_ollama_events

async def _lines(objs):
    for o in objs:
        yield (json.dumps(o) + "\n").encode()

async def _collect(objs):
    return [e async for e in iter_ollama_events(_lines(objs))]

async def test_deltas_and_done():
    evts = await _collect([
        {"message": {"role": "assistant", "content": "や"}, "done": False},
        {"message": {"role": "assistant", "content": "あ"}, "done": False},
        {"message": {"role": "assistant", "content": ""}, "done": True,
         "done_reason": "stop", "eval_count": 5},
    ])
    assert [e for e in evts if e["type"] == "delta"] == [
        {"type": "delta", "content": "や"}, {"type": "delta", "content": "あ"}]
    done = [e for e in evts if e["type"] == "done"][0]
    assert done["finish_reason"] == "stop" and done["eval_count"] == 5

async def test_thinking_is_dropped():
    evts = await _collect([
        {"message": {"role": "assistant", "thinking": "内心...", "content": ""}, "done": False},
        {"message": {"role": "assistant", "content": "答え"}, "done": False},
        {"done": True},
    ])
    deltas = [e["content"] for e in evts if e["type"] == "delta"]
    assert deltas == ["答え"]  # thinking は出ない

async def test_error_field():
    evts = await _collect([{"error": "model not found"}])
    assert evts == [{"type": "error", "message": "model not found"}]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_llm_parser.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.llm`)

- [ ] **Step 3: Write llm.py (parser only)**

```python
# core/hisho_core/llm.py
"""ollama /api/chat(NDJSON) を消費し中立イベントに変換。thinking は表示/記録しない。"""
from __future__ import annotations
import json
from typing import AsyncIterator

async def iter_ollama_events(raw_lines: AsyncIterator[bytes]) -> AsyncIterator[dict]:
    async for raw in raw_lines:
        line = raw.decode("utf-8", "replace").strip()
        if not line:
            continue
        obj = json.loads(line)
        if "error" in obj:
            yield {"type": "error", "message": str(obj["error"])}
            return
        msg = obj.get("message") or {}
        content = msg.get("content") or ""   # thinking は意図的に無視
        if content:
            yield {"type": "delta", "content": content}
        if obj.get("done"):
            yield {"type": "done",
                   "finish_reason": obj.get("done_reason") or "stop",
                   "eval_count": obj.get("eval_count")}
            return
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_llm_parser.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/llm.py core/tests/test_llm_parser.py
git commit -m "feat(core): ollama ndjson event parser (drops thinking)"
```

---

### Task 7: llm.py — chat_stream(httpx 配線)

**Files:**
- Modify: `core/hisho_core/llm.py`
- Test: `core/tests/test_llm_stream.py`

**Interfaces:**
- Produces: `async def chat_stream(messages: list[dict], *, model: str, ollama_host: str, num_ctx: int, keep_alive: str, think: bool = False, client_factory=None) -> AsyncIterator[dict]` — ollama `POST /api/chat` に `stream=True, think=think, options={"num_ctx":num_ctx}, keep_alive=keep_alive` を送り、`iter_ollama_events` に流す。`async with client.stream(...)` を**このジェネレータ内**で開き、キャンセル時に上流を閉じる。`client_factory` はテスト用注入(既定は `httpx.AsyncClient`)。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_llm_stream.py
"""chat_stream が /api/chat に正しいbodyを送り、行を parser に流すことを検証(httpxモック)。"""
import json, httpx, pytest
from hisho_core.llm import chat_stream

def _mock_factory(captured):
    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        captured["url"] = str(request.url)
        ndjson = (json.dumps({"message": {"content": "hi"}, "done": False}) + "\n"
                  + json.dumps({"done": True, "done_reason": "stop", "eval_count": 1}) + "\n")
        return httpx.Response(200, content=ndjson.encode())
    def factory():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return factory

async def test_stream_sends_body_and_parses():
    cap = {}
    evts = [e async for e in chat_stream(
        [{"role": "user", "content": "hi"}],
        model="qwen3.6:35b-a3b", ollama_host="http://127.0.0.1:11434",
        num_ctx=8192, keep_alive="30m", think=False,
        client_factory=_mock_factory(cap))]
    assert cap["url"].endswith("/api/chat")
    assert cap["body"]["model"] == "qwen3.6:35b-a3b"
    assert cap["body"]["stream"] is True
    assert cap["body"]["think"] is False
    assert cap["body"]["options"]["num_ctx"] == 8192
    assert cap["body"]["keep_alive"] == "30m"
    assert {"type": "delta", "content": "hi"} in evts
    assert any(e["type"] == "done" for e in evts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_llm_stream.py -v`
Expected: FAIL (`ImportError: cannot import name 'chat_stream'`)

- [ ] **Step 3: Add chat_stream to llm.py**

```python
# core/hisho_core/llm.py — 追記(冒頭 import に httpx を追加)
import httpx

async def chat_stream(messages, *, model, ollama_host, num_ctx, keep_alive,
                      think: bool = False, client_factory=None):
    body = {
        "model": model, "messages": messages, "stream": True, "think": think,
        "keep_alive": keep_alive, "options": {"num_ctx": num_ctx},
    }
    factory = client_factory or (lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)))
    client = factory()
    try:
        async with client.stream("POST", f"{ollama_host}/api/chat", json=body) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                yield {"type": "error", "message": f"ollama {resp.status_code}: {text[:200]}"}
                return
            async for evt in iter_ollama_events(resp.aiter_lines_bytes()):
                yield evt
    finally:
        await client.aclose()
```
> 注: httpx は `aiter_lines()`(str)を提供する。上の `resp.aiter_lines_bytes()` は存在しないため、
> `iter_ollama_events` に渡す前に str→bytes 変換するアダプタを噛ませる:
> ```python
> async def _as_bytes(aiter_str):
>     async for s in aiter_str:
>         yield (s + "\n").encode()
> ```
> そして `iter_ollama_events(_as_bytes(resp.aiter_lines()))` とする。`_as_bytes` を llm.py に追加すること。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_llm_stream.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/llm.py core/tests/test_llm_stream.py
git commit -m "feat(core): chat_stream httpx wiring to ollama /api/chat"
```

---

### Task 8: server.py — /healthz + /v1/models

**Files:**
- Create: `core/hisho_core/server.py`
- Test: `core/tests/test_server_health.py`

**Interfaces:**
- Consumes: `Config`, `Store`.
- Produces: `create_app(store: Store, config: Config, *, chat_fn=None, probe_fn=None) -> FastAPI`。`chat_fn` 既定 = `llm.chat_stream`、`probe_fn` 既定 = ollama を叩く実プローブ(テストで差し替え)。
  - `GET /healthz` → `{"core": true, "ollama": {"reachable": bool, "version": str|None}, "model": {"present": bool, "loaded": bool}}`(probe_fn の結果; 数秒キャッシュ)。
  - `GET /v1/models` → `{"object":"list","data":[{"id": model, "object":"model"} ...]}`(probe_fn の tags)。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_server_health.py
"""/healthz 層状ボディと /v1/models を、probe を注入して検証(ollama不要)。"""
import httpx, pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app

def _app(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    async def probe():
        return {"reachable": True, "version": "0.31.1",
                "model_present": True, "model_loaded": False,
                "models": ["qwen3.6:35b-a3b"]}
    return create_app(store, cfg, chat_fn=None, probe_fn=probe)

async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

async def test_healthz_layered(tmp_path):
    app = _app(tmp_path)
    async with await _client(app) as c:
        r = await c.get("/healthz")
        j = r.json()
        assert j["core"] is True
        assert j["ollama"]["reachable"] is True and j["ollama"]["version"] == "0.31.1"
        assert j["model"]["present"] is True and j["model"]["loaded"] is False

async def test_models_list(tmp_path):
    app = _app(tmp_path)
    async with await _client(app) as c:
        r = await c.get("/v1/models")
        ids = [m["id"] for m in r.json()["data"]]
        assert "qwen3.6:35b-a3b" in ids
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_server_health.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.server`)

- [ ] **Step 3: Write server.py (health + models)**

```python
# core/hisho_core/server.py
"""FastAPI アプリ組立。/healthz, /v1/models, /v1/chat/completions, /history を提供。"""
from __future__ import annotations
import time
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from . import llm
from .config import Config
from .store import Store

def _now_ms() -> int:
    return int(time.time() * 1000)

async def _default_probe(config: Config) -> dict:
    import httpx
    out = {"reachable": False, "version": None, "model_present": False,
           "model_loaded": False, "models": []}
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            v = await c.get(f"{config.ollama_host}/api/version")
            out["reachable"] = v.status_code == 200
            out["version"] = v.json().get("version") if v.status_code == 200 else None
            tags = await c.get(f"{config.ollama_host}/api/tags")
            names = [m["name"] for m in tags.json().get("models", [])] if tags.status_code == 200 else []
            out["models"] = names
            out["model_present"] = any(n.split(":")[0] == config.chat_model.split(":")[0] or n == config.chat_model for n in names)
            ps = await c.get(f"{config.ollama_host}/api/ps")
            loaded = [m["name"] for m in ps.json().get("models", [])] if ps.status_code == 200 else []
            out["model_loaded"] = config.chat_model in loaded
    except Exception:
        pass
    return out

def create_app(store: Store, config: Config, *, chat_fn=None, probe_fn=None) -> FastAPI:
    app = FastAPI()
    app.state.store = store
    app.state.config = config
    app.state.chat_fn = chat_fn or llm.chat_stream
    app.state.probe_fn = probe_fn or (lambda: _default_probe(config))
    app.state._probe_cache = {"t": 0.0, "v": None}

    async def _probe() -> dict:
        cache = app.state._probe_cache
        if app.state._probe_cache["v"] is not None and (time.time() - cache["t"]) < 3.0:
            return cache["v"]
        v = await app.state.probe_fn()
        cache["t"], cache["v"] = time.time(), v
        return v

    @app.get("/healthz")
    async def healthz():
        p = await _probe()
        return {"core": True,
                "ollama": {"reachable": p["reachable"], "version": p["version"]},
                "model": {"present": p["model_present"], "loaded": p["model_loaded"]}}

    @app.get("/v1/models")
    async def models():
        p = await _probe()
        names = p["models"] or [config.chat_model]
        return {"object": "list", "data": [{"id": n, "object": "model"} for n in names]}

    return app
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_server_health.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/server.py core/tests/test_server_health.py
git commit -m "feat(core): /healthz layered readiness + /v1/models"
```

---

### Task 9: server.py — /v1/chat/completions (SSE + 記録 + finally)

**Files:**
- Modify: `core/hisho_core/server.py`
- Test: `core/tests/test_server_chat.py`

**Interfaces:**
- Consumes: `context.build_messages`, `sse.*`, `store` メソッド、`app.state.chat_fn`.
- Produces: `POST /v1/chat/completions` — body `{model?, messages:[...], stream?, session_id?}`。ヘッダ `X-Hisho-Source`(既定 "external")。popover の時のみ persona+履歴を合成。user ターン同期記録→assistant プレースホルダ→`chat_fn` を SSE 再発行しつつ content 蓄積→`finally` で finalize(complete/partial/error)+touch。`StreamingResponse(media_type="text/event-stream", headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})`。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_server_chat.py
"""チャット: SSE 出力・全ターン記録・popover合成・切断時partial を、fake chat_fn で検証。"""
import httpx, pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app

def _make(tmp_path, chat_fn):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    async def probe():
        return {"reachable": True, "version": "x", "model_present": True,
                "model_loaded": True, "models": [cfg.chat_model]}
    return store, create_app(store, cfg, chat_fn=chat_fn, probe_fn=probe)

async def _fake_ok(messages, **kw):
    for ch in ["こん", "にちは"]:
        yield {"type": "delta", "content": ch}
    yield {"type": "done", "finish_reason": "stop", "eval_count": 2}

async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

async def test_stream_and_logging(tmp_path):
    store, app = _make(tmp_path, _fake_ok)
    async with await _client(app) as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"messages": [{"role": "user", "content": "やあ"}],
                               "session_id": "s1", "stream": True})
        body = r.text
        assert "こん" in body and "にちは" in body and "data: [DONE]" in body
    rows = store.conn.execute(
        "SELECT role, content, status, json_extract(meta,'$.source') AS src "
        "FROM turns WHERE session_id='s1' ORDER BY seq").fetchall()
    assert [(x["role"], x["content"], x["status"]) for x in rows] == [
        ("user", "やあ", "complete"), ("assistant", "こんにちは", "complete")]
    assert rows[0]["src"] == "popover"

async def test_external_source_default(tmp_path):
    store, app = _make(tmp_path, _fake_ok)
    async with await _client(app) as c:
        await c.post("/v1/chat/completions",
                     json={"messages": [{"role": "user", "content": "hi"}], "session_id": "e1"})
    src = store.conn.execute(
        "SELECT json_extract(meta,'$.source') AS s FROM turns "
        "WHERE session_id='e1' AND role='user'").fetchone()["s"]
    assert src == "external"

async def test_partial_on_stream_error(tmp_path):
    async def _fake_err(messages, **kw):
        yield {"type": "delta", "content": "途中"}
        yield {"type": "error", "message": "boom"}
    store, app = _make(tmp_path, _fake_err)
    async with await _client(app) as c:
        r = await c.post("/v1/chat/completions",
                         json={"messages": [{"role": "user", "content": "x"}], "session_id": "p1"})
        assert '"error"' in r.text
    a = store.conn.execute(
        "SELECT content, status FROM turns WHERE session_id='p1' AND role='assistant'").fetchone()
    assert a["content"] == "途中" and a["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_server_chat.py -v`
Expected: FAIL (404 / no route)

- [ ] **Step 3: Add the chat route to server.py**

```python
# core/hisho_core/server.py — import 追記
import uuid
from fastapi.responses import StreamingResponse
from . import context
from .sse import sse, chunk, DONE, error_frame

# create_app 内、/v1/models の下に追加:

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        cfg = app.state.config
        store = app.state.store
        body = await request.json()
        source = request.headers.get("X-Hisho-Source", "external")
        model = body.get("model") or cfg.chat_model
        session_id = body.get("session_id") or f"sess-{uuid.uuid4().hex[:12]}"
        msgs_in = body.get("messages", [])
        user_message = msgs_in[-1]["content"] if msgs_in else ""

        now = _now_ms()
        store.get_or_create_session(session_id, now)
        store.append_user_turn(session_id, user_message, now, source=source)

        if source == "popover":
            recent = store.recent_turns(session_id, cfg.history_replay_turns)
            # 直近には今記録した user も含まれるので末尾を除いて合成
            recent_wo_last = recent[:-1] if recent and recent[-1]["role"] == "user" else recent
            messages = context.build_messages(recent_wo_last, user_message,
                                              cfg.num_ctx, cfg.response_reserve)
        else:
            messages = msgs_in  # 外部ツールは素通し

        assistant_id = store.add_assistant_placeholder(session_id, model, _now_ms())
        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        async def gen():
            acc: list[str] = []
            status = "complete"
            finish = "stop"
            try:
                yield sse(chunk(cid, model, _now_ms() // 1000,
                                delta={"role": "assistant"}, finish_reason=None))
                async for evt in app.state.chat_fn(
                        messages, model=model, ollama_host=cfg.ollama_host,
                        num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False):
                    if evt["type"] == "delta":
                        acc.append(evt["content"])
                        yield sse(chunk(cid, model, _now_ms() // 1000,
                                        delta={"content": evt["content"]}, finish_reason=None))
                    elif evt["type"] == "error":
                        status = "error"
                        yield sse(error_frame(evt["message"]))
                        return  # [DONE] を出さずに閉じる
                    elif evt["type"] == "done":
                        finish = evt.get("finish_reason", "stop")
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason=finish))
                yield DONE
            except Exception as ex:  # クライアント切断含む
                status = "partial" if status == "complete" else status
                try:
                    yield sse(error_frame(str(ex)))
                except Exception:
                    pass
            finally:
                store.finalize_turn(assistant_id, "".join(acc), None, status, _now_ms())
                store.touch_session(session_id, _now_ms())

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
```
> 注: `error` イベントで `return` すると gen の `finally` が走り status='error' で finalize される。
> クライアント切断は `asyncio.CancelledError`(Exception 派生)で捕捉され partial になる。

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_server_chat.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/server.py core/tests/test_server_chat.py
git commit -m "feat(core): /v1/chat/completions streaming + turn logging (finally-safe)"
```

---

### Task 10: server.py — /history(読み取り専用)

**Files:**
- Modify: `core/hisho_core/server.py`
- Modify: `core/hisho_core/store.py` (list_sessions 追加)
- Test: `core/tests/test_server_history.py`

**Interfaces:**
- Produces (store): `list_sessions(limit: int) -> list[dict]`(`{id,title,last_activity}`、last_activity DESC)。
- Produces (route): `GET /history` → `{"sessions":[...]}`。`GET /history?session_id=S` → `{"session_id":S,"turns":[{role,content}...]}`(古→新, complete のみ)。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_server_history.py
"""/history: セッション一覧と1セッションのターン列を検証。"""
import httpx
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app

def _make(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path)
    store.get_or_create_session("s1", 100)
    store.append_user_turn("s1", "Q1", 100, source="popover")
    aid = store.add_assistant_placeholder("s1", "m", 101)
    store.finalize_turn(aid, "A1", 2, "complete", 102)
    store.touch_session("s1", 102)
    async def probe():
        return {"reachable": True, "version": "x", "model_present": True,
                "model_loaded": True, "models": [cfg.chat_model]}
    return create_app(store, cfg, chat_fn=None, probe_fn=probe)

async def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

async def test_history_sessions_and_turns(tmp_path):
    app = _make(tmp_path)
    async with await _client(app) as c:
        s = (await c.get("/history")).json()["sessions"]
        assert any(x["id"] == "s1" for x in s)
        t = (await c.get("/history", params={"session_id": "s1"})).json()["turns"]
        assert [(x["role"], x["content"]) for x in t] == [("user", "Q1"), ("assistant", "A1")]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_server_history.py -v`
Expected: FAIL (404 or AttributeError list_sessions)

- [ ] **Step 3: Add list_sessions + /history**

```python
# core/hisho_core/store.py — Store に追記
    def list_sessions(self, limit: int) -> list[dict]:
        rows = self.conn.execute(
            "SELECT id, title, last_activity FROM sessions WHERE status != 'deleted' "
            "ORDER BY last_activity DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]
```

```python
# core/hisho_core/server.py — create_app 内に追加
    @app.get("/history")
    async def history(session_id: str | None = None):
        store = app.state.store
        if session_id is None:
            return {"sessions": store.list_sessions(100)}
        return {"session_id": session_id, "turns": store.recent_turns(session_id, 500)}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_server_history.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/store.py core/hisho_core/server.py core/tests/test_server_history.py
git commit -m "feat(core): read-only /history (sessions + turns)"
```

---

### Task 11: lifecycle.py — core.json + stdin死監視 + ポート選択

**Files:**
- Create: `core/hisho_core/lifecycle.py`
- Test: `core/tests/test_lifecycle.py`

**Interfaces:**
- Produces:
  - `core_json_path(config_db_path: str) -> str`(db と同じ Hisho ディレクトリの `core.json`)。
  - `write_core_json(path: str, pid: int, port: int) -> None` / `read_core_json(path: str) -> dict | None`。
  - `is_our_stale_core(info: dict) -> bool`(pid 生存かつ `/healthz` が `core:true` を返すか。`urllib` で 127.0.0.1 に短時間アクセス。ネットワーク不能なら False)。
  - `bind_port(preferred: int) -> tuple[socket.socket, int]`(127.0.0.1:preferred を試し、`OSError` なら `:0` に fallback。listen 済ソケットと実ポートを返す)。
  - `start_stdin_death_watcher(on_death=os._exit)` — スレッド起動し stdin EOF で `on_death(0)`。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_lifecycle.py
"""core.json 読み書き・ポート fallback・stdin死監視の発火を検証。"""
import io, json, socket, threading, time
from hisho_core import lifecycle

def test_core_json_roundtrip(tmp_path):
    p = str(tmp_path / "core.json")
    lifecycle.write_core_json(p, pid=1234, port=51100)
    info = lifecycle.read_core_json(p)
    assert info["pid"] == 1234 and info["port"] == 51100

def test_read_missing_returns_none(tmp_path):
    assert lifecycle.read_core_json(str(tmp_path / "nope.json")) is None

def test_bind_port_fallbacks_when_taken():
    # preferred を占有しておくと fallback で別ポートが返る
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen()
    taken = held.getsockname()[1]
    sock, port = lifecycle.bind_port(taken)
    assert port != 0
    sock.close(); held.close()

def test_stdin_death_watcher_fires_on_eof():
    fired = {"v": None}
    r = io.BytesIO(b"")  # 即 EOF
    lifecycle.start_stdin_death_watcher(on_death=lambda code: fired.__setitem__("v", code),
                                        stream=r)
    time.sleep(0.1)
    assert fired["v"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_lifecycle.py -v`
Expected: FAIL (`ModuleNotFoundError: hisho_core.lifecycle`)

- [ ] **Step 3: Write lifecycle.py**

```python
# core/hisho_core/lifecycle.py
"""子プロセスの生存管理: core.json 照合・ポート選択・親死(stdin EOF)での自死。"""
from __future__ import annotations
import json, os, socket, sys, threading
from pathlib import Path

def core_json_path(config_db_path: str) -> str:
    return str(Path(config_db_path).expanduser().parent / "core.json")

def write_core_json(path: str, pid: int, port: int) -> None:
    Path(path).write_text(json.dumps({"pid": pid, "port": port}))

def read_core_json(path: str):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return isinstance(pid, int)  # PermissionError = 別ユーザだが生存
    except OSError:
        return False

def is_our_stale_core(info: dict) -> bool:
    import urllib.request
    if not info or not _pid_alive(info.get("pid", -1)):
        return False
    try:
        with urllib.request.urlopen(
                f"http://127.0.0.1:{info['port']}/healthz", timeout=1.0) as r:
            return json.loads(r.read()).get("core") is True
    except Exception:
        return False

def bind_port(preferred: int):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", preferred))
    except OSError:
        s.bind(("127.0.0.1", 0))  # OS 割当に fallback
    s.listen()
    return s, s.getsockname()[1]

def start_stdin_death_watcher(on_death=os._exit, stream=None):
    st = stream if stream is not None else sys.stdin.buffer
    def _watch():
        try:
            while st.read(1):  # 親が生きてる限りブロック
                pass
        except Exception:
            pass
        on_death(0)  # EOF = 親死
    threading.Thread(target=_watch, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_lifecycle.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add core/hisho_core/lifecycle.py core/tests/test_lifecycle.py
git commit -m "feat(core): lifecycle (core.json, port fallback, stdin-EOF death watcher)"
```

---

### Task 12: __main__.py — 起動配線 + 実機スモーク

**Files:**
- Create: `core/hisho_core/__main__.py`
- Create: `core/tests/test_entry.py`
- Create: `core/SMOKE.md`(実 ollama での手動確認手順)

**Interfaces:**
- Consumes: `config`, `store`, `server`, `lifecycle`.
- Produces: `build_server_and_port(config) -> (app, socket, port)`(reclaim/fallback 込みで socket と port を確定、app を作る)。`main()` — config→ensure_db_dir→Store→core.json 照合(stale は無視して再bind)→stdin死監視起動→core.json 書込→uvicorn を確定 socket で単一 worker 起動。

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_entry.py
"""起動配線: build_server_and_port が app と有効ポートを返し、DB dir を作ることを検証。"""
from hisho_core.config import load_config
from hisho_core import __main__ as entry

def test_build_server_and_port(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "d" / "t.db"), "HISHO_PORT": "0"})
    app, sock, port = entry.build_server_and_port(cfg)
    try:
        assert port > 0
        assert (tmp_path / "d").is_dir()
        assert any(r.path == "/healthz" for r in app.routes)
    finally:
        sock.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && python -m pytest tests/test_entry.py -v`
Expected: FAIL (`ModuleNotFoundError` or `AttributeError: build_server_and_port`)

- [ ] **Step 3: Write __main__.py**

```python
# core/hisho_core/__main__.py
"""`python -m hisho_core` エントリ: 設定→DB→ポート確定→stdin死監視→uvicorn 起動。"""
from __future__ import annotations
import os
import uvicorn
from .config import load_config, ensure_db_dir
from .store import Store
from .server import create_app
from . import lifecycle

def build_server_and_port(config):
    ensure_db_dir(config.db_path)
    store = Store(config.db_path)
    cj = lifecycle.core_json_path(config.db_path)
    existing = lifecycle.read_core_json(cj)
    if existing and lifecycle.is_our_stale_core(existing):
        # 既存の我々の core が生きている: 本 MVP では新規 bind を優先(親が監督)
        pass
    sock, port = lifecycle.bind_port(config.port)
    app = create_app(store, config)
    return app, sock, port

def main():
    config = load_config()
    app, sock, port = build_server_and_port(config)
    lifecycle.start_stdin_death_watcher()  # 親(Swift)死で自死
    lifecycle.write_core_json(lifecycle.core_json_path(config.db_path), os.getpid(), port)
    uvicorn.run(app, fd=sock.fileno(), workers=1, log_level="info")

if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && python -m pytest tests/test_entry.py -v`
Expected: PASS (1 passed)

- [ ] **Step 5: Full suite + real-ollama smoke doc**

Run: `cd core && python -m pytest -v`
Expected: PASS (全タスクのテスト、緑)

```markdown
# core/SMOKE.md — 実 ollama での手動確認(自動テストとは別)
前提: ローカル ollama 稼働、`qwen3.6:35b-a3b` pull 済。
1. 起動: `cd core && HISHO_PORT=51100 python -m hisho_core`
2. 健康: `curl -s 127.0.0.1:51100/healthz | python -m json.tool`
   → ollama.reachable=true, model.present=true を確認。
3. チャット(ストリーミング):
   `curl -N -H "X-Hisho-Source: popover" -H "Content-Type: application/json" \
     -d '{"messages":[{"role":"user","content":"自己紹介して"}],"session_id":"smoke1"}' \
     127.0.0.1:51100/v1/chat/completions`
   → `data: {...}` が逐次流れ、末尾 `data: [DONE]`。
4. 記録: `curl -s "127.0.0.1:51100/history?session_id=smoke1" | python -m json.tool`
   → user と assistant の2ターンが complete で残る。
5. 外部ツール互換(任意): OpenAI 互換クライアント(Chatbox 等)の base_url を
   `http://127.0.0.1:51100/v1`、model=`qwen3.6:35b-a3b` にして疎通。
6. 親死自死(任意): 起動プロセスの stdin を閉じると core が自動終了することを確認。
```

- [ ] **Step 6: Commit**

```bash
git add core/hisho_core/__main__.py core/tests/test_entry.py core/SMOKE.md
git commit -m "feat(core): entrypoint wiring + real-ollama smoke doc"
```

---

## Self-Review (spec カバレッジ)

- §7 API 契約 → Task 8(/healthz,/v1/models), 9(/v1/chat/completions,X-Hisho-Source), 10(/history)。SSE 手書き=Task 5,9。✓
- §8 LLM パス → Task 6(native /api/chat 消費, think除去), 7(num_ctx/keep_alive/httpx/cancel)。✓
- §9 秘書合成 → Task 4(persona+切り詰め), 9(popover のみ合成)。✓
- §10 永続化 → Task 2(schema/WAL/STRICT/fts), 3(user同期→placeholder→finally finalize, meta.source), 並行=3短txn。✓
- §6 ライフサイクル(pipe-EOF, core.json, port fallback) → Task 11, 12。✓
- §11 プライバシー(127.0.0.1 のみ) → Task 11 bind_port, 12 起動。認証なし=loopback境界。✓
- §12 env → Task 1。✓
- **Plan 2 送り(この plan の対象外)**: §3 Swift 殻/popover UX, §4-5 パッケージング/署名(Xcode, python-build-standalone 同梱), §14 の focus スパイク・bundle relocation smoke。sensors/RAG(§13)は seam のみで未実装。
- 既知の簡略化(MVP): `is_our_stale_core` は検知するが本 MVP は常に新規 bind(親が監督)。トークン数は eval_count/近似のみ。要 Plan 2 で Swift 側から stdin パイプ保持。

## Execution Handoff

Plan 1 完了・保存済。実装は plan=Opus/impl=Sonnet・Haiku 方針 → **Subaget-Driven(推奨)**: タスク毎に新規 Sonnet/Haiku サブエージェント + 二段レビュー、サブエージェントは commit しない(親がレビュー後にコミット)。

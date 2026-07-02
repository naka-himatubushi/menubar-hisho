# Hisho RAG 長期記憶 実装プラン (Plan 3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **実装ポリシー:** plan=Opus / 実装=Sonnet サブエージェント / **サブエージェントは commit しない**。

**Goal:** 蓄積済みの popover 会話をベクトル索引し、新しい発話のたびに関連する過去の記憶を検索して秘書コンテキストに注入する — 「使うたびに賢くなる」の実体。

**Architecture:** spec §10 の v2 additive スキーマ(`chunks` + `embeddings` + `vec0`)を `store.py` に追加。新モジュール `rag.py` が embedding(ollama `/api/embed`, bge-m3)と kNN 検索を持つ。索引はチャット完了後の fire-and-forget タスク + 起動時 backfill。検索は popover 経路の `build_messages` 直前に top-k を取り、persona に「過去の関連メモ」ブロックとして合成。**外部ツール (`source='external'`) は索引しない**(記憶汚染防止)。

**Tech Stack:** sqlite-vec v0.1.9(スパイク済: 同梱 CPython 3.13 で拡張ロード + kNN 動作確認 2026-07-02)/ bge-m3(ollama, 1024 次元, ローカル導入済)/ 追加 dep = `sqlite-vec` のみ。

## Global Constraints

- **既存テーブル (`sessions`/`turns`/`turns_fts`) は不変**。v2 は additive のみ、`PRAGMA user_version` で管理 (spec §10)。
- **float32 blob (`embeddings.vec`) が真実の源**。`vec0` は再構築可能な索引扱い (モデル差替・破損は rebuild で済ます)。
- **索引対象 = `turns.meta.source == 'popover'` の complete な user/assistant ターンのみ**。external は絶対に索引しない。
- embedding は **ollama `/api/embed`**(`http://127.0.0.1:11434`、`config.ollama_host` 共用)。クラウド経路なし。
- **sqlite-vec ロード失敗時は RAG を無効化して通常チャット続行**(クラッシュさせない)。numpy fallback は実装しない(文書化のみ、spec §10)。
- 環境変数: `HISHO_EMBED_MODEL`(既定 `bge-m3`)/ `HISHO_RAG`(既定 `1`、`0` で無効)/ `HISHO_RAG_TOP_K`(既定 `3`)。
- write は既存パターン厳守: `write_lock` + `anyio.to_thread.run_sync`、モデル呼び出し中に write txn を開かない。
- 全ファイル役割 docstring / logger 使用(print 禁止)/ 実名を書かない。
- **core 変更後は `scripts/build_core.sh` 再実行 → .app リビルド**(Plan 2 の教訓)。

## 実測済み事実 (2026-07-02 スパイク)

- 同梱 CPython 3.13.13: sqlite 3.50.4、`enable_load_extension` 可、`sqlite_vec.load(db)` → `vec_version()` = v0.1.9、`vec0` kNN 動作。
- `curl ollama /api/embed -d '{"model":"bge-m3","input":"…"}'` → `{"embeddings": [[1024 floats]]}`。
- 現 DB: `turns` 12 行(popover 実会話含む)。

---

### Task 1: store v2 — chunks / embeddings / vec0 (+ 拡張ロード)

**Files:**
- Modify: `core/hisho_core/store.py`
- Modify: `core/pyproject.toml`(dependencies に `sqlite-vec` 追加)
- Test: `core/tests/test_store_rag.py`

**Interfaces:**
- Produces: `Store.rag_enabled: bool` / `Store.add_chunk(source_type, source_id, session_id, content, vec: bytes, model, dim) -> int` / `Store.search_chunks(query_vec: bytes, k, exclude_session_id=None) -> list[dict]` / `Store.unindexed_popover_turns(limit) -> list[dict]`
- vec は **float32 little-endian bytes**(`struct.pack(f"<{dim}f", *floats)`)。sqlite-vec は blob を直接受ける。

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_store_rag.py`:

```python
"""store v2 (RAG テーブル): 移行・chunk 追加・kNN 検索・未索引抽出を tmp DB で検証。"""
import struct
from hisho_core.store import Store


def _vec(*floats):
    return struct.pack(f"<{len(floats)}f", *floats)


def _store(tmp_path):
    return Store(str(tmp_path / "t.db"), vec_dim=4)  # テストは 4 次元で軽く


def test_migration_v2_and_rag_enabled(tmp_path):
    s = _store(tmp_path)
    assert s.rag_enabled is True
    ver = s.conn.execute("PRAGMA user_version").fetchone()[0]
    assert ver == 2
    tables = {r[0] for r in s.conn.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','virtual table') OR type='table'")}
    assert {"chunks", "embeddings"} <= tables


def test_existing_tables_untouched(tmp_path):
    s = _store(tmp_path)
    # v1 のテーブルと索引がそのまま生きている
    s.get_or_create_session("sess-a", 1000)
    s.append_user_turn("sess-a", "こんにちは", 1000, "popover")
    assert s.recent_turns("sess-a", 10)[0]["content"] == "こんにちは"


def test_add_and_search_chunks(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "私の好物はカレーです", 1000, "popover")
    t2 = s.append_user_turn("sess-a", "明日は雨らしい", 2000, "popover")
    s.add_chunk("turn", t1, "sess-a", "私の好物はカレーです", _vec(1, 0, 0, 0), "bge-m3", 4)
    s.add_chunk("turn", t2, "sess-a", "明日は雨らしい", _vec(0, 1, 0, 0), "bge-m3", 4)

    hits = s.search_chunks(_vec(0.9, 0.1, 0, 0), k=1)
    assert len(hits) == 1
    assert hits[0]["content"] == "私の好物はカレーです"
    assert hits[0]["session_id"] == "sess-a"


def test_search_excludes_session(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "カレー", 1000, "popover")
    s.add_chunk("turn", t1, "sess-a", "カレー", _vec(1, 0, 0, 0), "bge-m3", 4)

    assert s.search_chunks(_vec(1, 0, 0, 0), k=3, exclude_session_id="sess-a") == []
    assert len(s.search_chunks(_vec(1, 0, 0, 0), k=3, exclude_session_id="sess-b")) == 1


def test_unindexed_popover_turns(tmp_path):
    s = _store(tmp_path)
    s.get_or_create_session("sess-a", 1000)
    t1 = s.append_user_turn("sess-a", "popover の発話", 1000, "popover")
    s.append_user_turn("sess-a", "外部ツールの発話", 2000, "external")  # 対象外
    s.append_user_turn("sess-a", "短い", 3000, "popover")               # 10 文字未満は対象外

    rows = s.unindexed_popover_turns(limit=10)
    assert [r["id"] for r in rows] == [t1]

    s.add_chunk("turn", t1, "sess-a", "popover の発話", _vec(0, 0, 0, 1), "bge-m3", 4)
    assert s.unindexed_popover_turns(limit=10) == []
```

- [ ] **Step 2: 失敗確認**

Run: `core/.venv/bin/pip install sqlite-vec -q && core/.venv/bin/python -m pytest core/tests/test_store_rag.py -q`
Expected: FAIL(`vec_dim` 引数なし / メソッド未定義)

- [ ] **Step 3: 実装**

`core/pyproject.toml` の dependencies を `["fastapi", "uvicorn", "httpx", "sqlite-vec"]` に。

`core/hisho_core/store.py` — `Store.__init__` に `vec_dim: int = 1024` を追加し、接続 bootstrap 後に呼ぶ:

```python
    def _init_rag(self) -> None:
        """v2 additive 移行: sqlite-vec をロードし chunks/embeddings/vec0 を作る。
        拡張ロード失敗時は rag_enabled=False で通常動作を続ける(クラッシュ禁止)。"""
        self.rag_enabled = False
        try:
            import sqlite_vec
            self.conn.enable_load_extension(True)
            sqlite_vec.load(self.conn)
            self.conn.enable_load_extension(False)
        except Exception:
            logger.warning("sqlite-vec load failed — RAG disabled", exc_info=True)
            return
        if self.conn.execute("PRAGMA user_version").fetchone()[0] < 2:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS chunks (
                    id INTEGER PRIMARY KEY,
                    source_type TEXT NOT NULL,
                    source_id INTEGER NOT NULL,
                    session_id TEXT,
                    content TEXT NOT NULL,
                    token_count INTEGER,
                    meta TEXT NOT NULL DEFAULT '{{}}'
                ) STRICT;
                CREATE UNIQUE INDEX IF NOT EXISTS uq_chunks_source
                    ON chunks(source_type, source_id);
                CREATE TABLE IF NOT EXISTS embeddings (
                    chunk_id INTEGER NOT NULL REFERENCES chunks(id) ON DELETE CASCADE,
                    model TEXT NOT NULL,
                    dim INTEGER NOT NULL,
                    vec BLOB NOT NULL,
                    PRIMARY KEY (chunk_id, model)
                ) STRICT;
                CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks_bge_m3
                    USING vec0(embedding float[{self.vec_dim}]);
                PRAGMA user_version = 2;
            """)
            self.conn.commit()
        self.rag_enabled = True
```

メソッド 3 本(全て同期 — 呼び手が to_thread で包む):

```python
    def add_chunk(self, source_type, source_id, session_id, content, vec, model, dim) -> int:
        """chunk + embedding + vec0 索引を 1 txn で追加。戻り値 chunk id。重複 source は既存 id を返す。
        (DO NOTHING 後の lastrowid/rowcount は挙動が曖昧なため、SELECT 先行で明示分岐する。)"""
        row = self.conn.execute(
            "SELECT id FROM chunks WHERE source_type=? AND source_id=?",
            (source_type, source_id)).fetchone()
        if row:
            return row[0]
        cur = self.conn.execute(
            "INSERT INTO chunks(source_type, source_id, session_id, content) VALUES(?,?,?,?)",
            (source_type, source_id, session_id, content))
        cid = cur.lastrowid
        self.conn.execute(
            "INSERT INTO embeddings(chunk_id, model, dim, vec) VALUES(?,?,?,?)",
            (cid, model, dim, vec))
        self.conn.execute(
            "INSERT INTO vec_chunks_bge_m3(rowid, embedding) VALUES(?,?)",
            (cid, vec))
        self.conn.commit()
        return cid

    def search_chunks(self, query_vec, k, exclude_session_id=None) -> list[dict]:
        """vec0 kNN → chunks join。exclude_session_id は現在の会話(直近 replay 済)を除くため。"""
        rows = self.conn.execute(
            "SELECT c.content, c.session_id, v.distance "
            "FROM vec_chunks_bge_m3 v JOIN chunks c ON c.id = v.rowid "
            "WHERE v.embedding MATCH ? AND v.k = ? "
            "ORDER BY v.distance",
            (query_vec, k + 8)).fetchall()  # 除外分を見込み多めに取る
        out = []
        for content, session_id, distance in rows:
            if exclude_session_id is not None and session_id == exclude_session_id:
                continue
            out.append({"content": content, "session_id": session_id, "distance": distance})
            if len(out) >= k:
                break
        return out

    def unindexed_popover_turns(self, limit=50) -> list[dict]:
        """未索引の popover complete ターン(10文字以上)を古い順に返す(backfill 用)。"""
        rows = self.conn.execute(
            "SELECT t.id, t.session_id, t.content FROM turns t "
            "LEFT JOIN chunks c ON c.source_type='turn' AND c.source_id = t.id "
            "WHERE c.id IS NULL AND t.status='complete' "
            "  AND length(t.content) >= 10 "
            "  AND json_extract(t.meta, '$.source') = 'popover' "
            "ORDER BY t.id LIMIT ?", (limit,)).fetchall()
        return [{"id": r[0], "session_id": r[1], "content": r[2]} for r in rows]
```

注意: assistant ターンの meta.source — 現実装では user ターンのみ source を持つ可能性がある。**実装時に `add_assistant_placeholder` を確認し、meta.source が無ければ popover 経路で `{"source": source}` を入れるよう 1 行追加**(これも Task 1 の範囲。既存テストが meta を固定 assert していないか確認して調整)。

- [ ] **Step 4: green 確認** — `core/.venv/bin/python -m pytest core/tests/ -q` → 既存 41 + 新 5 = **46 passed** 期待(assistant meta 変更でズレたら理由を報告)

- [ ] **Step 5: Commit**(controller)

---

### Task 2: rag.py — embedding クライアント + 検索/索引オーケストレーション

**Files:**
- Create: `core/hisho_core/rag.py`
- Test: `core/tests/test_rag.py`

**Interfaces:**
- Consumes: `Store.add_chunk / search_chunks / unindexed_popover_turns`(Task 1)、`config.Config`
- Produces:
  - `rag.embed(texts: list[str], *, model, ollama_host, client_factory=None) -> list[bytes] | None`(float32-LE blob。失敗は None、例外を上げない)
  - `rag.index_turn(store, write_lock, turn_id, session_id, content, *, config) -> bool`
  - `rag.retrieve(store, user_message, *, config, exclude_session_id) -> list[str]`(top-k の content。RAG 無効/失敗は `[]`)
  - `rag.backfill(store, write_lock, *, config, batch=20) -> int`(索引した件数)

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_rag.py`:

```python
"""rag.py: fake ollama クライアントで embed 形状・索引・検索・backfill・失敗時の無害性を検証。"""
import asyncio
import struct
import pytest
from hisho_core import rag
from hisho_core.config import load_config
from hisho_core.store import Store


def _cfg(tmp_path, **extra):
    env = {"HISHO_DB": str(tmp_path / "t.db"), **extra}
    return load_config(env=env)


class _FakeEmbedClient:
    """/api/embed を真似る。単語→固定4次元ベクトルの決定的マップ。"""
    def __init__(self, dim=4):
        self.dim = dim
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        vecs = []
        for text in json["input"]:
            v = [0.0] * self.dim
            v[hash(text) % self.dim] = 1.0
            vecs.append(v)
        class R:
            status_code = 200
            def json(self_inner):
                return {"embeddings": vecs}
        return R()

    async def aclose(self):
        pass


async def test_embed_returns_float32_blobs():
    fake = _FakeEmbedClient()
    out = await rag.embed(["こんにちは"], model="bge-m3",
                          ollama_host="http://127.0.0.1:11434",
                          client_factory=lambda: fake)
    assert out is not None and len(out) == 1
    assert len(out[0]) == 4 * 4  # float32 × 4
    assert fake.calls[0]["url"].endswith("/api/embed")
    assert fake.calls[0]["json"]["model"] == "bge-m3"


async def test_embed_failure_returns_none():
    class _Boom:
        async def post(self, url, json=None):
            import httpx
            raise httpx.ConnectError("down")
        async def aclose(self):
            pass
    out = await rag.embed(["x"], model="bge-m3", ollama_host="http://127.0.0.1:1",
                          client_factory=lambda: _Boom())
    assert out is None


async def test_index_and_retrieve_roundtrip(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    store.get_or_create_session("sess-a", 1000)
    t1 = store.append_user_turn("sess-a", "私の好物はカレーライスです", 1000, "popover")
    lock = asyncio.Lock()
    fake = _FakeEmbedClient()

    ok = await rag.index_turn(store, lock, t1, "sess-a", "私の好物はカレーライスです",
                              config=cfg, client_factory=lambda: fake)
    assert ok is True

    hits = await rag.retrieve(store, "私の好物はカレーライスです", config=cfg,
                              exclude_session_id="sess-b", client_factory=lambda: fake)
    assert hits == ["私の好物はカレーライスです"]

    # 同一セッションは除外される(直近 replay と二重にならない)
    hits_same = await rag.retrieve(store, "私の好物はカレーライスです", config=cfg,
                                   exclude_session_id="sess-a", client_factory=lambda: fake)
    assert hits_same == []


async def test_retrieve_disabled_returns_empty(tmp_path):
    cfg = _cfg(tmp_path, HISHO_RAG="0")
    store = Store(cfg.db_path, vec_dim=4)
    hits = await rag.retrieve(store, "何か", config=cfg, exclude_session_id=None,
                              client_factory=lambda: _FakeEmbedClient())
    assert hits == []


async def test_backfill_indexes_pending(tmp_path):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    store.get_or_create_session("sess-a", 1000)
    store.append_user_turn("sess-a", "バックフィル対象のターンです", 1000, "popover")
    store.append_user_turn("sess-a", "こちらも索引対象のターン", 2000, "popover")
    lock = asyncio.Lock()
    n = await rag.backfill(store, lock, config=cfg,
                           client_factory=lambda: _FakeEmbedClient())
    assert n == 2
    assert store.unindexed_popover_turns(10) == []
```

- [ ] **Step 2: 失敗確認** — `core/.venv/bin/python -m pytest core/tests/test_rag.py -q` → ImportError

- [ ] **Step 3: 実装**

`core/hisho_core/rag.py`:

```python
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
```

`config.py` に 3 項目追加(`Config` dataclass + `load_config`):

```python
    embed_model: str        # e.get("HISHO_EMBED_MODEL", "bge-m3")
    rag_enabled: bool       # e.get("HISHO_RAG", "1") == "1"
    rag_top_k: int          # int(e.get("HISHO_RAG_TOP_K", "3"))
```

- [ ] **Step 4: green 確認** — 全体 pytest → **51 passed** 期待
- [ ] **Step 5: Commit**(controller)

---

### Task 3: チャット経路への配線 — 検索注入 + 完了時索引 + 起動時 backfill

**Files:**
- Modify: `core/hisho_core/context.py`(`build_messages` に `memories` 引数)
- Modify: `core/hisho_core/server.py`(popover 分岐で retrieve、finally 後に index、lifespan に backfill)
- Test: `core/tests/test_context_memories.py` + `core/tests/test_server_rag.py`

**Interfaces:**
- Consumes: `rag.retrieve / index_turn / backfill`(Task 2)
- Produces: `context.build_messages(recent, user_message, num_ctx, response_reserve, persona=PERSONA, memories=())`

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_context_memories.py`:

```python
"""build_messages の memories 注入: system prompt への合成と budget 切り詰めを検証。"""
from hisho_core.context import build_messages, PERSONA


def test_memories_go_into_system_prompt():
    msgs = build_messages([], "今日の夕飯は?", 8192, 1024,
                          memories=["私の好物はカレーライスです"])
    assert msgs[0]["role"] == "system"
    assert PERSONA in msgs[0]["content"]
    assert "過去の関連メモ" in msgs[0]["content"]
    assert "カレーライス" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "今日の夕飯は?"}


def test_no_memories_keeps_persona_unchanged():
    msgs = build_messages([], "こんにちは", 8192, 1024, memories=[])
    assert msgs[0]["content"] == PERSONA


def test_memories_count_against_budget():
    # 巨大メモでも履歴が budget からはみ出ないこと(落ちない・全メッセージが収まる)
    big = "居" * 3000
    recent = [{"role": "user", "content": "古い発話" * 50}] * 10
    msgs = build_messages(recent, "質問", 8192, 1024, memories=[big])
    total = sum(len(m["content"]) // 3 + 1 for m in msgs)
    assert total <= 8192 - 1024
```

`core/tests/test_server_rag.py`:

```python
"""チャット経路の RAG 配線: retrieve が呼ばれ system に載る / 完了後に index タスクが走る。"""
import anyio
import httpx
import pytest
from hisho_core.config import load_config
from hisho_core.store import Store
from hisho_core.server import create_app


def _cfg(tmp_path):
    return load_config(env={"HISHO_DB": str(tmp_path / "t.db")})


async def _fake_chat(messages, **kw):
    _fake_chat.seen = messages
    yield {"type": "delta", "content": "はい、カレーですね"}
    yield {"type": "done", "finish_reason": "stop", "eval_count": 1}


async def test_popover_chat_injects_memories_and_indexes(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)

    retrieved = ["私の好物はカレーライスです"]
    indexed = []

    async def fake_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        return retrieved

    async def fake_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    from hisho_core import server as server_mod
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)
    monkeypatch.setattr(server_mod.rag, "index_turn", fake_index)

    app = create_app(store, cfg, chat_fn=_fake_chat)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         headers={"X-Hisho-Source": "popover"},
                         json={"session_id": "sess-x", "stream": True,
                               "messages": [{"role": "user", "content": "夕飯どうしよう、おすすめは?"}]})
        assert r.status_code == 200

    system = _fake_chat.seen[0]["content"]
    assert "カレーライス" in system                      # 記憶が注入された
    await anyio.sleep(0.05)                              # fire-and-forget の索引を待つ
    assert any("夕飯どうしよう" in c for c in indexed)    # user ターンが索引対象になった


async def test_external_chat_has_no_rag(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    store = Store(cfg.db_path, vec_dim=4)
    called = []

    async def fake_retrieve(*a, **kw):
        called.append(1)
        return []

    from hisho_core import server as server_mod
    monkeypatch.setattr(server_mod.rag, "retrieve", fake_retrieve)

    app = create_app(store, cfg, chat_fn=_fake_chat)
    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/v1/chat/completions",
                         json={"stream": True,
                               "messages": [{"role": "user", "content": "外部ツールからの質問です"}]})
        assert r.status_code == 200
    assert called == []  # external では検索も注入もしない
```

- [ ] **Step 2: 失敗確認** — pytest 該当 2 ファイル → FAIL

- [ ] **Step 3: context.py 実装**

`build_messages` を拡張(署名互換 — 既存呼び出しは無変更で動く):

```python
def build_messages(recent, user_message, num_ctx, response_reserve,
                   persona=PERSONA, memories=()):
    system_text = persona
    if memories:
        notes = "\n".join(f"- {m}" for m in memories)
        system_text = f"{persona}\n\n過去の関連メモ(参考。矛盾したら新しい発言を優先):\n{notes}"
    budget = num_ctx - response_reserve
    system_msg = {"role": "system", "content": system_text}
    user_msg = {"role": "user", "content": user_message}
    used = approx_tokens(system_text) + approx_tokens(user_message)
    ...(以降の切り詰めループは既存のまま)...
```

- [ ] **Step 4: server.py 配線**

popover 分岐(`if source == "popover":`)を:

```python
        if source == "popover":
            memories = await rag.retrieve(store, user_message, config=cfg,
                                          exclude_session_id=session_id)
            recent = await anyio.to_thread.run_sync(store.recent_turns, session_id, cfg.history_replay_turns)
            recent_wo_last = recent[:-1] if recent and recent[-1]["role"] == "user" else recent
            messages = context.build_messages(recent_wo_last, user_message,
                                              cfg.num_ctx, cfg.response_reserve,
                                              memories=memories)
```

`gen()` の `finally` の**後**(StreamingResponse を返す前ではなく finally 節の中の shield ブロック直後)に fire-and-forget 索引を足す — 具体的には finally 内 shield ブロックの直後に:

```python
            finally:
                with anyio.CancelScope(shield=True):
                    ...(既存の finalize)...
                if source == "popover":
                    asyncio.create_task(_index_pair())
```

`_index_pair` は gen 内クロージャ:

```python
        async def _index_pair():
            """user + assistant ターンを索引(失敗しても無害)。"""
            try:
                await rag.index_turn(store, app.state.write_lock, user_turn_id,
                                     session_id, user_message, config=cfg)
                text = "".join(acc)
                if text:
                    await rag.index_turn(store, app.state.write_lock, assistant_id,
                                         session_id, text, config=cfg)
            except Exception:
                logger.warning("index after chat failed", exc_info=True)
```

前提: user ターンの id が要る — 既存の `append_user_turn` は lastrowid を返すので `user_turn_id = await anyio.to_thread.run_sync(...)` の戻り値を受ける(現在捨てているだけ)。
lifespan startup に backfill を追加(warm-up タスクと並走):

```python
        backfill_task = asyncio.create_task(
            rag.backfill(app.state.store, app.state.write_lock, config=config))
```

(shutdown で `backfill_task.cancel()` + suppress。)
`import` に `from . import rag` を追加。

- [ ] **Step 5: green 確認** — 全体 pytest → **56 passed** 期待(Store() の既存呼び出しが vec_dim 既定 1024 で走ることに注意 — 既存テストの Store 生成が壊れないこと)
- [ ] **Step 6: Commit**(controller)

---

### Task 4: 同梱再ビルド + E2E 記憶スモーク

**Files:**
- Modify: `core/SMOKE.md`(RAG 節追記)

- [ ] **Step 1**: `scripts/build_core.sh` 再実行(sqlite-vec が同梱に入る)→ `.app` リビルド → 再起動
- [ ] **Step 2**: 自動 E2E — 別セッションの記憶が引けるか:

```bash
# セッション 1: 事実を教える
curl -sN http://127.0.0.1:51100/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'X-Hisho-Source: popover' \
  -d '{"session_id":"sess-ragtest-a","stream":true,"messages":[{"role":"user","content":"覚えておいて: 私の好物はカレーライスです"}]}' >/dev/null
sleep 3  # fire-and-forget 索引待ち
# 索引されたか
sqlite3 "$HOME/Library/Application Support/Hisho/secretary.db" "SELECT COUNT(*) FROM chunks;"
# セッション 2(別session_id): 記憶が引けて答えに反映されるか
curl -sN http://127.0.0.1:51100/v1/chat/completions -H 'Content-Type: application/json' \
  -H 'X-Hisho-Source: popover' \
  -d '{"session_id":"sess-ragtest-b","stream":true,"messages":[{"role":"user","content":"私の好物は何だったか覚えてる?"}]}' | grep -o '"content": "[^"]*"' | head -20
```

Expected: chunks > 0、セッション 2 の応答に「カレー」が含まれる(LLM 出力なので確率的 — 応答全文を確認し、含まれなければ retrieve ログを見る)。

- [ ] **Step 3**: SMOKE.md に上記を「RAG 記憶 E2E」節として追記
- [ ] **Step 4**: Commit + main マージ(controller)

## 非目標

- `POST /ask` 単独エンドポイント(seam のまま。popover 注入が MVP の記憶体験)
- summary/document/sensor ソースの索引(`source_type` の口だけ確保)
- numpy fallback 実装(sqlite-vec 動作実証済のため文書化のみ)
- 索引の削除同期(スレ削除は FK CASCADE で chunks も消える。vec0 の孤児 rowid は rebuild で掃除 — 運用課題として記録)
- UI の記憶インジケータ

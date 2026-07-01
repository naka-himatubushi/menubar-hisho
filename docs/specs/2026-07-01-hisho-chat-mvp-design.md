# Hisho — 設計仕様 (Chat MVP)

> macOS メニューバー常駐のローカル LLM 秘書。第1フェーズ = チャットのみ。
> 作成日 2026-07-01 / 対象 `~/sandbox/menubar-hisho/` / 筆者の個人開発
> 本仕様は多角レビュー (5レンズ + 統合 + ストレージ専用レンズ、実機検証込み) を経て硬化済み。

---

## 1. 概要と MVP スコープ

Hisho は「使うたびに賢くなる自分専用の秘書」を目指す macOS メニューバーアプリ。最終形は
チャット + PC 健康状態/バックアップ状況 + 蓄積型 RAG 記憶。**本 MVP はチャットのみ**を作り、
残り (センサー・RAG・Studio 加速・WebUI) は**差し込み口 (seam) だけ設計し実装しない**。

参照コンセプト: `ggml-org/Llama-macOS`（「LLM のための居心地いい家」= メニューバー常駐・完全ローカル・
ゼロ設定・OpenAI 互換ローカルサーバ・自動 load/unload）。**概念と体験だけ借り、エンジンは既存の ollama 据え置き**
（llama.cpp に乗り換えない）。「~4MB ネイティブ」の主張は**捨てる**（CPython 同梱で ~90MB になるため、正直に）。

### ロック済み決定 (前提)

| 軸 | 決定 |
|---|---|
| スコープ | チャットのみ。sensors/RAG は seam のみ |
| スタック | Swift 薄殻 (SwiftUI) + Python core (FastAPI) |
| エンジン | ローカル ollama (`127.0.0.1:11434`) 据え置き |
| チャットモデル | `qwen3.6:35b-a3b`（実 pull 済 ~23GB / Q4_K_M 相当）。llm.py の単一設定値。q8 移行はベンチ後判断 |
| 埋め込み(将来) | bge-m3 / ruri / embeddinggemma（ローカル） |
| 呼び出し/表示 | メニューバーアイコン → popover、返答はトークン逐次ストリーミング |
| 記憶 | **初日から**全ターンを SQLite に記録（RAG は後で索引を貼るだけ） |
| ビルド土台 | **Xcode あり** → .app 組立/署名/Info.plist/entitlements/SMAppService を Xcode に任せる |
| 配布姿勢 | **Sandbox OFF・Hardened Runtime OFF・notarize なし**（自ビルド・自機ローカル運用） |
| Python 同梱 | uv 管理 python-build-standalone を Resources に丸ごと同梱（system/venv-symlink 不可） |

### 明示的に作らないもの (scope cuts)

センサー実体と `GET /status` / RAG 実体と `POST /ask` / 設定画面・モデルピッカー・カタログ/インストーラ /
内蔵 WebUI / App Sandbox・notarize・Developer ID 署名・強制 Hardened Runtime / Studio-tailnet ルーティング /
LaunchAgent 常駐 / 自動再起動バックオフ状態機械 / `/v1` の認証・CORS・多worker・`--reload` /
OpenAI フル互換 (tools/function-calling・logprobs・n>1・画像入力) / ORM・マイグレーションフレームワーク /
~4MB フットプリント追求。

---

## 2. 参照フレーミング (Llama-macOS から採用 / 却下)

- **採用**: メニューバー常駐の居心地・`127.0.0.1` 限定の完全プライベート/ゼロテレメトリ・ollama 任せの
  自動 load/unload・ゼロ設定初回起動（モデル1個ロック、ピッカー/設定なし）・OpenAI 互換エンドポイント
  （将来 WebUI/外部ツールが無料で繋がる seam）。
- **却下/正直に諦める**: 「~4MB tiny native」サイズ主張（同梱 CPython で ~90-110MB、strip して ~45-60MB）。
  モデルカタログ/インストーラ・ハードウェア自動最適・設定 UI。

---

## 3. アーキテクチャとプロセスモデル

```
┌─ メニューバー (LSUIElement, Dockなし) ─────┐
│  🤖 icon → popover                          │  SwiftUI (Xcode管理の.app)
│  ┌───────────────────────────────────────┐  │
│  │ 会話ログ (逐次描画)                     │  │  ← app層storeがstream保持
│  │ [入力...] ↵                             │  │     (popover破棄でも切れない)
│  └───────────────────────────────────────┘  │
│  状態: starting core… / warming model… / ready / ollama-down / core-stopped
└───────────┬─────────────────────────────────┘
            │  HTTP 127.0.0.1:51100 (loopback限定)
            │  POST /v1/chat/completions (SSE) + X-Hisho-Source: popover
            ▼
┌─ Python core  (Contents/Resources/core, 同梱CPython3.13) ─┐
│  server.py  ルーティング/オーケストレーション              │
│    POST /v1/chat/completions  純粋・ステートレス(外部ツール可)│
│    GET  /v1/models            /api/tags プロキシ            │
│    GET  /healthz              2段階readiness(層状ボディ)     │
│    GET  /history              読み取り専用                  │
│  llm.py     ollama native /api/chat を消費→OpenAI SSE 再発行 │
│             (host/model の単一チョークポイント=将来Studio)   │
│  store.py   SQLite 唯一のスキーマ所有者                     │
│      │                          │                          │
│      ▼                          ▼                          │
│  ollama :11434              ~/Library/Application Support/  │
│  qwen3.6:35b-a3b                Hisho/secretary.db (WAL)    │
└────────────────────────────────────────────────────────────┘
```

**責務分離**: Swift 殻 = UI + core 子プロセスの起動/監視/終了のみ（ビジネスロジックゼロ、ollama に直接触れない）。
`llm.py` = 純トランスポート。`server.py` = オーケストレーション。`store.py` = スキーマ唯一所有。

**1ターンの流れ**: 入力↵ → `POST /v1/chat/completions` (popover マーカー付) → server が user ターンを
同期記録 + assistant プレースホルダ行を作成 → llm.py が ollama `/api/chat` を stream 消費し OpenAI SSE 再発行
→ Swift が逐次描画 → stream 終了時 `finally` で assistant 行を UPDATE（正常=complete / 切断=partial / 例外=error）。

---

## 4. リポジトリ構成・ビルド・パッケージング

```
menubar-hisho/
  HishoApp/                 # Xcode プロジェクト (SwiftUI, .app を所有)
    HishoApp.xcodeproj
    Sources/…               # MenuBarExtra or AppKit host, streamストア, 状態UI
    Info.plist              # LSUIElement=YES, CFBundleIdentifier, ATS, NSAllowsLocalNetworking
  core/                     # Python core (hisho_core パッケージ)
    hisho_core/
      __init__.py           # 役割docstring
      __main__.py           # `python -m hisho_core` エントリ
      server.py  llm.py  store.py  config.py
    pyproject.toml
    tests/
  scripts/
    build_core.sh           # uv で python-build-standalone を取得→Resources/core へコピー→deps install
  docs/specs/…
```

- **同梱 Python**: uv 管理の **python-build-standalone CPython 3.13** ツリーを `Contents/Resources/core/` に
  **丸ごとコピー**（symlink venv は外部インタプリタの絶対パスを焼き込み .app 移動で壊れる）。
  deps を `uv pip install --python <tree>/bin/python3` でそのツリーに直接入れる。
  deps = `fastapi` + `uvicorn`(uvloop+httptools のみ、`[standard]` フル不要) + `httpx`。SQLite は stdlib `sqlite3`。
  SSE フレーミングは手書き（`sse-starlette` 不要）。
- **検証**: ビルド済 .app を `/tmp` に移動して起動 → core が動く（relocatable 確認）を smoke test 化。
- **Xcode が所有**: Info.plist（`LSUIElement`, `CFBundleIdentifier`, ATS, `NSAllowsLocalNetworking=YES`）・
  署名・entitlements・`SMAppService`（ログイン起動）・MenuBarExtra。
- **将来の RAG 注意**: 同梱インタプリタが sqlite 拡張 (`enable_load_extension`) をロード可か要検証。
  不可なら §10 の numpy コサイン fallback に切替（データ層は同一スキーマのまま動く）。

---

## 5. 署名・Sandbox・配布姿勢

- **MVP**: **App Sandbox OFF**（ソケット bind・`~/Library/Application Support` 実パス解決・子プロセス spawn・
  外部ツール接続が全部 sandbox で壊れるため）。**Hardened Runtime OFF**。**notarize しない**。
  Apple-Development（または ad-hoc）署名で自機ローカル起動 = Gatekeeper 隔離対象外。
- **notarize-ready は保つ（払わないだけ）**: 将来他 Mac へ配布する時に必要なのは
  Developer ID Application 証明書 + 全 Mach-O に Hardened Runtime + inside-out per-file 署名
  （`codesign --deep` は使わない）+ notarytool + staple。同梱 venv の C 拡張 (.so) は各個署名対象。
  → この道は**ドキュメント化のみ**、MVP では実施しない。
- **理由**: 現状 Apple-Development 証明書のみ（notarize 不可）。自ビルドは隔離されない。

---

## 6. 子プロセスのライフサイクルと監視

**最大の落とし穴 = Swift 強制終了 (Cmd-Opt-Esc/クラッシュ/デバッガ停止) で core が孤児化しポート占有。**
macOS に `PR_SET_PDEATHSIG` 無し、MenuBarExtra は終了フックが飛ばないので graceful 終了だけでは必ず漏れる。

- **構造的な孤児殺し (pipe-EOF)**: 子の stdin を Swift が握る `Pipe` にする。Python 側 daemon watcher が
  `sys.stdin.buffer.read()` で待ち、親死→カーネルが pipe を閉じ→EOF→`os._exit()`。これが `PR_SET_PDEATHSIG`
  の代替。
- **ベルト&サスペンダー**: 起動時 `~/Library/Application Support/Hisho/core.json {pid, port}` を書く。
  次回起動時に存在すれば PID 生存 + それが我々の python か + `/healthz` が我々の署名を返すか照合 → 再利用 or
  SIGKILL してから spawn。子は独自プロセスグループで起動し、終了時はグループごと kill + reap。
- **監視は最小** (レンズ対立の解決): spawn は起動時1回、health-poll で green-or-timeout、終了時 SIGTERM→SIGKILL、
  子の stdout/stderr を Application Support 配下のローテートログへ、想定外の子終了時は「core stopped — restart」
  を可視化し手動再起動アクション。**自動バックオフ再起動はしない**（crash-loop 回避）。
- **ポート**: 既定 **51100**（ollama 11434 から意図的に離す）、`127.0.0.1` のみ bind、認証なし。
  bind 衝突時は `/healthz` 署名で我々の stale core か判定 → 再利用/kill、他人なら OS 割当 (`:0`) に fallback し
  実ポートを core.json に書く（Swift は core.json を読む、外部ツールは既定ポート）。`0.0.0.0` は絶対使わない。

---

## 7. Python core API 契約

- **`POST /v1/chat/completions`** — 純粋・ステートレス。外部ツール (VS Code/Antigravity/Chatbox) が素で繋がる
  OpenAI 互換。ヘッダ `X-Hisho-Source: popover` がある時だけ秘書挙動（§9）をサーバ側で合成。無い時は
  passthrough で `source='external'` として記録。実装は `stream` true/false と Hisho が使うフィールドのみ。
  「best-effort OpenAI 互換、自前クライアント + Chatbox で検証」と明記。
- **`GET /v1/models`** — `/api/tags` プロキシ。
- **`GET /healthz`** — 2段階 readiness。ボディ層状: `{ollama:{reachable,version}, model:{present,loaded}}`
  （`/api/version` + `/api/tags` + `/api/ps` を数秒キャッシュ）。core-up ≠ model-ready を区別。
- **`GET /history`** — 読み取り専用（セッション/直近ターン取得）。
- **SSE フレーミング**: `data:{json}` / `[DONE]` 手書き。安定 uuid を全 chunk で使い回し、初 delta に
  `{role:assistant}`、`done_reason→finish_reason`。中間失敗時は最後に `data:{error:...}` を1本出して
  `[DONE]` 無しで閉じ、partial を `finish_reason='error'` で永続化。first-byte 前の失敗は素直に HTTP エラー。
- **SSE 地雷回避**: bundled run は単一 uvicorn worker・`--reload` なし・SSE ルートに GZip 掛けない・
  `media_type=text/event-stream`・`Cache-Control:no-cache`・`X-Accel-Buffering:no`。

---

## 8. LLM パス (llm.py)

- ollama の**ネイティブ `/api/chat` (NDJSON) を消費**し、core が独自 OpenAI SSE を再発行。
  （どうせ記録のため stream をパースするので pure passthrough は省力にならない。native は `keep_alive` /
  `options.num_ctx` / think トグル / リッチな done フレームを露出する利点。）
- **推論モデルの罠**: `qwen3.6:35b-a3b` は reasoning モデル。既定 **`think:false`**（キビキビ）。
  think ON 時は思考を折り畳み「thinking…」UI に流し、**表示にもログにも `<think>` を連結しない**。
  記録は `message.content` のみ。将来用に reasoning カラムを予約。
- **num_ctx の罠**: ollama 既定 ~4096 は履歴が伸びると**最古トークン (=persona/system) から黙って捨てる** →
  秘書がセッション途中で指示を忘れる。`options.num_ctx` を毎回明示 (8192–16384) + アプリ側で
  system/persona + 直近を必ず budget 内に保つ切り詰めを1関数に隔離。popover は直近 ~10-20 メッセージのみ
  model へ replay、view には ~50 ターン。
- **キャンセル伝播**: `async with client.stream(...)` を **yield するジェネレータの内側**で開き、client 切断
  → `CancelledError` → loopback ソケット閉 → ollama 側も生成中断。バッファリングしない。
- **単一チョークポイント**: host/model はここだけ。将来の Studio-tailnet ルーティングはここを差し替えるだけ。
- **warm-up / unload**: `/healthz` green 後に 1 トークンの warm-up を `keep_alive~30m` で撃ち cold load を隠す。
  終了時 `keep_alive:0`。load/unload は ollama に任せ再実装しない。

---

## 9. 秘書コンテキスト合成 (RAG-seam の前身)

- 固定 persona system prompt + 直近履歴の bounded replay + `num_ctx` budget 切り詰めを **core のサーバ側で合成**。
  popover はリクエストマーカー (`X-Hisho-Source: popover`) で opt-in。→ `/v1` は外部ツール向けに教科書的に純粋なまま。
- この「サーバ側コンテキスト合成」を汎化した先が将来の `POST /ask`（RAG 検索結果を差し込む）。**同じ関数の一般化**
  で済むよう今から形を整える。

---

## 10. 永続化とスキーマ (store.py が唯一所有)

> 実機検証済 (2026-07-01): Homebrew python 3.14.5 は SQLite 3.53.2 / `STRICT` / WAL / `enable_load_extension` 可、
> sqlite-vec v0.1.9 が kNN 動作。**Apple `/usr/bin/python3` は拡張ロード不可（sqlite-vec 死ぬ）= 硬い制約**。

**無痛拡張の原則**: 既存テーブルは append-only、`ALTER`/rewrite しない。RAG は `turns` を**指す新テーブル**として
後付け。行内の可変属性は `meta TEXT` JSON で吸収。スキーマ版は `PRAGMA user_version` で additive 管理。

### 接続 bootstrap (毎接続)
```sql
PRAGMA journal_mode = WAL;        PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;       PRAGMA synchronous = NORMAL;
PRAGMA wal_autocheckpoint = 1000;
```

### 初日テーブル (v1)
```sql
CREATE TABLE sessions (
    id TEXT PRIMARY KEY, title TEXT,
    created_at INTEGER NOT NULL, last_activity INTEGER NOT NULL,  -- unix epoch millis, UTC
    status TEXT NOT NULL DEFAULT 'active',                        -- active|archived|deleted
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,                                       -- rowid; 将来chunkが参照する安定id
    session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, role TEXT NOT NULL,                     -- user|assistant|system
    content TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'complete',                      -- streaming|complete|partial|error
    model TEXT, token_count INTEGER,
    created_at INTEGER NOT NULL, completed_at INTEGER,
    meta TEXT NOT NULL DEFAULT '{}'
) STRICT;
CREATE UNIQUE INDEX uq_turns_session_seq ON turns(session_id, seq);
CREATE INDEX idx_sessions_activity ON sessions(last_activity DESC);

-- 日本語キーワード検索 (初日, 安価): 既定unicode61はCJK非分割 → trigram
CREATE VIRTUAL TABLE turns_fts USING fts5(
    content, content='turns', content_rowid='id', tokenize='trigram');
-- + insert/delete/update トリガで同期
```
- タイムスタンプは INTEGER unix-epoch-millis UTC（UI でローカル表示）。`turns.id` を INTEGER rowid にするのは
  sqlite-vec/FTS5 が rowid(int64) キーだから → RAG join が整数等価で済む。
- `source` 相当は `turns.meta` でなく将来の `chunks.source_type` で持つ。ただし popover/external の区別は
  記録時に必要なので **`turns.meta.source` に `'popover'|'external'`** を初日から入れる（外部ツールの `/v1`
  トラフィックが将来の RAG 記憶を汚さないため）。

### 将来 RAG (v2, additive・既存不変)
`chunks(id, source_type['turn'|'summary'|'document'|'sensor'], source_id, session_id, content, token_count, meta)` +
`embeddings(chunk_id, model, dim, vec BLOB float32-LE, PRIMARY KEY(chunk_id,model))` +
`CREATE VIRTUAL TABLE vec_chunks_bge_m3 USING vec0(embedding float[1024])` + `documents(...)`。

**seam の要**: ①参照するだけ（`chunks.source_id → turns.id`、turns 不変、backfill はループ）②多相ソース
（`(source_type, source_id)` で chat/file/sensor/summary を同一パイプラインが処理、新ソース = 新 `source_type` 値、
検索側 DDL ゼロ）③**float32 blob を真実の源に**、`vec0`/FTS は再構築可能な索引扱い（モデル差替も「sqlite-vec 壊れた」
も rebuild で済み migration にならない）。

### ベクトル検索方針 (RAG フェーズ)
**sqlite-vec 単一ファイル同 DB 推奨**（chroma/faiss/サーバ追加しない）。単一ユーザ・完全ローカルでは 1 store/1 file/
1 transaction が正義。chroma は既定 PostHog テレメトリ (§11 違反) で却下。vec0 は brute-force kNN（ANN 無し）だが
数万 chunk まで M5 で sub-100ms。**失敗条件**: ~10^5–10^6 chunk で線形が重くなる → (a) メタ/recency 事前フィルタ
(b) int8 量子化 (c) それでも超えたら ANN store へ（`embeddings` を移行元に）。**fallback**: sqlite-vec 不可時は
`embeddings` blob + numpy コサイン（C 拡張ゼロ、同一スキーマ、遅いが動く）。

### 並行性
WAL は「複数 reader + 単一 writer」。肝は**モデル呼び出しの間 write txn を開いたままにしない**。
専用 writer 接続を `asyncio.Lock` で直列化し、DB 呼びは `anyio.to_thread.run_sync` でスレッドへ（event loop を塞がない）。
**1交換 = 短い write txn 3本**（①user ターン確定 ②assistant プレースホルダ `streaming` ③stream 終了後 `finally`
で UPDATE + `sessions.last_activity`）。トークン毎書き込みはしない（WAL churn）。idle/終了時 `wal_checkpoint(TRUNCATE)`。

---

## 11. プライバシー姿勢 (データは Mac の外に出ない)

1. **`127.0.0.1` のみ bind**（`0.0.0.0` 禁止）= 最重要1行。
2. **クラウド LLM 経路なし**。chat も embedding も loopback の ollama。`HF_HUB_OFFLINE=1`/`TRANSFORMERS_OFFLINE=1`
   をプロセス env に。モデルは明示 `ollama pull` のみ。
3. **第三者テレメトリ/クラッシュレポータ入れない**（Sentry/Crashlytics/analytics 不使用、依存を phone-home 監査）。
   sqlite-vec を chroma より選ぶ理由の一つ。
4. **ollama 自体の挙動を正直に**: 推論はローカルだがバージョン確認/`pull` は外向き。更新無効で運用、pull は可視な
   setup ステップに。**検証法**: 通常使用中に `lsof -i -nP | grep -v 127.0.0.1` で Hisho/ollama から非 loopback 接続が
   無いこと（ユーザ実行可能なチェックとして同梱、断定でなく検証）。
5. **at-rest**: FileVault (全ディスク) + perms（`Hisho/` dir `700`, `secretary.db` `600`）。SQLCipher は将来オプション。
6. **削除は実削除**: 「スレ削除」は hard-delete（FK CASCADE）+ `PRAGMA secure_delete=ON`（or 定期 `VACUUM`）。

---

## 12. 設定と環境変数
`HISHO_PORT`(既定 51100) / `OLLAMA_HOST`(既定 127.0.0.1:11434) / `HISHO_DB`
(既定 `~/Library/Application Support/Hisho/secretary.db`) / エントリ `python -m hisho_core`。
モデル名は `config.py`/`llm.py` の単一値 `qwen3.6:35b-a3b`。

---

## 13. 将来の seam と非目標
- **sensors** → `core/sensors.py` + `GET /status`、popover にタブ。seam のみ。
- **RAG** → `core/rag.py` + `POST /ask`（§9 のサーバ側合成を検索で一般化）。既に貯めた `turns` を索引。
- **Studio-tailnet** → `llm.py` の host 切替のみ。必須経路にしない。
- **WebUI** → 純 `/v1` にそのまま attach（popover が MVP の UI）。

---

## 14. テスト戦略
- `core` は pytest: `llm.py` は mock ollama、`store.py` は tmp DB、httpx で SSE 結合1本、
  **cancel/finally-logging テスト**（popover 破棄で partial が記録されるか）。
- Swift: launch/terminate テスト + **macOS 26 focus スパイク**（バークリック→即入力→送信→初トークン後も focus 維持。
  失敗なら AppKit `NSStatusItem`+`NSPopover` host へ退避。chat view は host-agnostic に保つ）。
- **bundle relocation smoke**（.app を移動して core が動く）。

---

## 15. 未解決事項・決定ログ
- **決定済**: Xcode 使用 / モデル `qwen3.6:35b-a3b`(~23GB, 現 pull) / Sandbox OFF / python-build-standalone 同梱 /
  ポート 51100 / sqlite-vec は RAG フェーズ (numpy fallback 用意)。
- **保留 (実装前スパイクで確定)**: MenuBarExtra `.window` の focus（macOS 26 未検証、30分スパイク）/
  同梱 CPython の `enable_load_extension` 可否（RAG フェーズ直前に検証、不可なら numpy fallback）/
  q8_0 への移行（load_duration + RSS ベンチ後）。

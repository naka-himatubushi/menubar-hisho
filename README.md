# Hisho (JARVIS) — 完全ローカルの macOS メニューバー秘書

macOS のメニューバーに常駐し、ローカル LLM (ollama) と会話する個人秘書アプリ。
全会話を SQLite に記録し、ベクトル検索 (RAG) で**使うたびに賢くなる長期記憶**を持つ。
データは 1 バイトも Mac の外に出ない (127.0.0.1 のみ・テレメトリなし)。

A fully-local macOS menu bar assistant: SwiftUI shell + Python core + ollama,
with SQLite-backed long-term memory (sqlite-vec RAG). Nothing leaves your Mac.

## 構成

```
メニューバー (SwiftUI MenuBarExtra)
  └─ popover: チャット UI (逐次ストリーミング描画)
       │ HTTP 127.0.0.1:51100
       ▼
hisho_core (同梱 CPython 3.13 / FastAPI)
  ├─ OpenAI 互換 /v1/chat/completions (SSE) — 外部ツールもそのまま接続可
  ├─ 秘書コンテキスト合成: persona + 直近履歴 + RAG 検索した過去の記憶
  ├─ 全ターンを SQLite (WAL) に記録、sqlite-vec で kNN 検索
  └─ ollama (チャット: qwen 系 / 埋め込み: bge-m3)
```

- **Swift 殻** (`HishoApp/` + `HishoKit/`): UI と core 子プロセスの起動・監視・終了のみ。
  親が死ぬと stdin EOF で core が自死する (孤児プロセスを作らない)
- **Python core** (`core/`): 単体でも `python -m hisho_core` で動く
- **記憶** (`~/Library/Application Support/Hisho/secretary.db`): 会話 + プロフィール事実 +
  定期収集した現況ドキュメント。質問の自己エコー除外・現況優先などの検索補正付き

## 必要なもの

- macOS 26+ (Apple Silicon) / Xcode / [uv](https://docs.astral.sh/uv/) / [XcodeGen](https://github.com/yonaskolb/XcodeGen)
- [ollama](https://ollama.com) + チャットモデルと埋め込みモデル (既定: `qwen3.6:35b-a3b` / `bge-m3`)

## ビルド

```bash
# 1. 同梱用 Python ツリーを組み立てる (python-build-standalone + deps)
scripts/build_core.sh

# 2. Xcode プロジェクト生成 → ビルド
cd HishoApp && xcodegen generate && cd ..
xcodebuild -project HishoApp/HishoApp.xcodeproj -scheme HishoApp \
  -configuration Debug -derivedDataPath build/derived build

# 3. 起動
open build/derived/Build/Products/Debug/Hisho.app
```

テスト: `core` は `pytest core/tests/`、Swift は `cd HishoKit && swift test`。

## 設定 (環境変数)

`HISHO_PORT` (51100) / `HISHO_MODEL` / `HISHO_EMBED_MODEL` (bge-m3) /
`HISHO_RAG` (1) / `HISHO_NUM_CTX` (8192) / `OLLAMA_HOST` / `HISHO_DB`

## 付属ツール

- `scripts/seed_memory.py` — プロフィール事実を記憶に種まき
- `scripts/collect_backup_status.py` — 機器のバックアップ状況を定期収集して記憶に反映
  (対象は `~/Library/Application Support/Hisho/backup_targets.json` に置く — リポジトリ外)
- `scripts/smoke_relocation.sh` — .app 移動耐性 + 孤児プロセス検査
- `scripts/check_no_egress.sh` — 非 loopback 接続がないことの確認

## プライバシー原則

1. `127.0.0.1` のみ bind (`0.0.0.0` 禁止)
2. クラウド LLM 経路なし。チャットも埋め込みもローカル ollama
3. 第三者テレメトリ・クラッシュレポータなし
4. 検証可能: `scripts/check_no_egress.sh`

## License

MIT

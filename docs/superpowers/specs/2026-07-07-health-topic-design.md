# sensors「health」topic 設計書 — mini 監視レポートの読み取り

日付: 2026-07-07
状態: 設計承認済み

## 目的

JARVIS が Mac mini の定例監視 (health-check cron 群) の成果物を読めるようにする。
「mini の調子どう?」「朝レポート見せて」に、聞かれた瞬間の実測で答える。

たとえ: これまで JARVIS は「バックアップ倉庫の見回り」はできたが、
「ビル管理室の計器盤 (mini が毎時集めている健康データ)」は見えなかった。
本機能は管理室の計器盤と朝刊 (Discord 朝レポート) を読む権限を追加する。

## 方式

既存 sensors (読み取り専用・台帳駆動・決定的事前注入) に topic を1つ足すだけ。
新しい仕組みは作らない。

### 1. `sensors.py`

- `TOPICS = ("backup", "machines", "storage", "health", "all")`
- `ledger_items()`: sensor_targets.json を読む分岐に `health` を含める。
  `all` の時は machines / storage / health を全部測る。

### 2. `server.py`

- `_TOPIC_PATTERNS` に追加: `("health", re.compile(r"警報|異常|アラート|レポート|健康"))`
  (「調子」は入れない — 既存テストが「調子どう?」→ all を固定挙動として保証しており、
  漠然とした聞き方には全部測って返す方が自然なため)
- 既存ルール踏襲: ちょうど1群一致のみ採用、0 or 複数一致は all (読み取り専用ゆえ過剰測定が安全側)。
  例:「バックアップの異常ある?」は backup + health の2群一致 → all。

### 3. 台帳 `sensor_targets.json` (リポジトリ外・人間管理)

`topics.health` セクションを新設、2エントリ:

- **mini 健康サマリ**: ssh (BatchMode, ConnectTimeout 6) →
  `/tmp/health/{system-health,docker-resources,ollama-status,immich-status}.json` を
  python3 one-liner で数行に圧縮 (メモリ%/CPU/Docker/コンテナ数/ollama 状態/計測時刻)。
  ファイル欠損・JSON 破損は「未計測」の行にして落ちない。
- **朝レポート**: ssh → mini 内で `source ~/.secrets/.env` →
  Discord API GET messages (limit=20) → bot 投稿のうち「朝のヘルスレポート」を含む
  最新1件の本文を出力 (先頭 1200 字で打切り)。
  **Discord token は mini の外に出ない** (MacBook 側には置かない)。

### 4. テスト (`core/tests/`)

- `_guess_topic`: 「朝レポート見せて」→ health / 「バックアップの異常」→ all (複数一致)
- `ledger_items("health")` が台帳 `topics.health` を読む / `all` に health が含まれる
- TOPICS enum 外は従来どおり ValueError

## 継承する安全契約 (sensors.py 冒頭 docstring)

- 台帳は人間だけが編集する固定コマンド。LLM/HTTP 由来文字列を混ぜない (だから shell=True 可)
- 全て読み取り専用。書き込み・起動系は登録しない
- コマンド1本 8 秒 / topic 全体 12 秒。超過は「実測失敗」の行
- 形式不正エントリは実行せず報告 (第二層防御)

## やらないこと

- 警報履歴の遡り検索 (要望に含まれず)
- Discord への書き込み
- mini 側 health スクリプトの変更
- Swift 殻 (HishoApp) の変更 — Python core の再起動のみで反映

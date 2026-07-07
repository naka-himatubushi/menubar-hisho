<!--
役割: JARVIS actions (手の解禁・人間確認必須) の設計書。
固定レジストリ2アクション (start_backup / fleet_submit) を、確認フロー付きで安全に実行する。
ユーザー承認済み (2026-07-07): 両アクション + 安全不変条件5つ。
-->

# JARVIS actions 設計 (2026-07-07)

## 目的

「バックアップ回しておいて」「Studio でテスト流しておいて」に対し、JARVIS が実行内容を
そのまま提示 → ユーザーの「はい」でのみ実行する。初めての「手」だが、握らせるのは
固定された2本のレバーだけ。

## アクション (固定レジストリ・これ以外は永遠に作らない前提で設計)

| action | 引数 | 実行 (全て argv リスト直渡し・shell 非経由) |
|---|---|---|
| `start_backup` | machine ∈ {macbook, studio, mini} | macbook: `tmutil startbackup` / studio・mini: ssh 経由で同コマンド。冪等 |
| `fleet_submit` | machine ∈ {studio, mini} + task 文字列 | `work <machine> <task>` (mac-fleet CLI)。task は argv の1要素として渡す |

## 安全不変条件 (受け入れ条件)

1. **初回ターンで実行しない**。JARVIS は必ず実行内容 (実際に走る argv を人間可読にしたもの)
   を提示して停止する
2. 確認は次ターンの「はい/yes/ok/やって/実行して」(先頭一致・短文のみ) でのみ成立。
   **pending は 5分TTL・一回限り・同一 session 限定**。確認以外の発話が来たら破棄
3. 実行は subprocess の **argv リスト直渡し** (shell=False)。task 文字列がコマンドとして
   解釈される経路をつくらない
4. アクション tool spec はアクション意図ゲートが開いたターンのみモデルに公開
   (sensors と同じ思想。日常会話にレバーは存在しない)
5. モデルが tool を呼ばなくても、ゲートが開いていればサーバが決定的に提案を組み立てる
   (実行はどのみち確認後なので、提案の取り違えはユーザーが「はい」を出さないことで無害化)

## 構成

### 設定 (リポジトリ外, `~/Library/Application Support/Hisho/`)

- `action_targets.json` (新規) — ssh 宛先と work CLI パス。**IP/ホスト名/ユーザー名を
  コードに書かないため** (public repo):
  `{"backup_ssh": {"macbook": null, "studio": "<ssh先>", "mini": "<ssh先>"}, "work_cli": "~/.local/bin/work"}`

### 新モジュール `core/hisho_core/actions.py`

- ActionSpec: name / 引数スキーマ / argv ビルダ (設定を読んで list[str] を返す) / 人間可読の説明文ビルダ
- `execute(action, args, config) -> str`: argv 実行 (timeout 30秒、fleet_submit は投入だけなので数秒)。
  stdout/stderr を整形して返す
- pending 管理: `PendingAction` (action, args, argv 表示文, created, session_id)。
  server 側で session_id → PendingAction の辞書 (TTL 300秒, pop で一回限り)

### `server.py` — 提案→確認→実行の状態機械

- アクション意図ゲート (正規表現、forget/sensor と並置):
  `(バックアップ|TM).*(回|取|開始|走|実行|して)|((スタジオ|studio|ミニ|mini).*(投げ|回し|任せ|やらせ|やって))`
- 優先順位: **forget > アクション確認 (pending あり) > アクション提案 > sensor**
- 提案ターン: ゲート開 → アクション tool spec のみモデルに公開。モデルの tool call は
  実行せず pending に変換。tool call が無ければサーバが決定的に構築
  (start_backup: 機体語から推定 / fleet_submit: 機体語 + task=ユーザー発話全文)。
  応答は「実行内容: <argv 可読形>。実行していい? (はい で実行)」の定型 (モデル生成に任せない)
- 確認ターン: pending あり + 確認語 → execute → 結果を文脈注入してモデルが平文報告。
  確認語以外 → pending 破棄を一言添えて通常応答へ
- アクション関連ターンは RAG 注入・索引ともスキップ (sensors と同じ)

### 応答規約

- 提案文とアクション実行結果はサーバ定型 + 実測時刻。モデルは実行結果の要約のみ
- 失敗時は stderr をそのまま (盛らない)

## テスト

1. pytest: argv ビルダ (injection 断面: task に `"; rm -rf` 等を入れて argv 1要素のまま)、
   TTL/一回限り/セッション束縛、確認語マッチャ (「はいはい話戻すけど」で発火しない = 短文限定)、
   優先順位 (forget との共存、pending 中に sensor 質問)、ゲート非発火
2. 実LLMスモーク: 提案→「はい」→実行 (start_backup macbook は実 TM 起動で無害・冪等 /
   fleet_submit は mini の local 脳に軽ジョブ)。「はい」以外で破棄されることも実測
3. 完了条件: 確認なし実行の経路がテストで存在しないことを証明 / 既存 133 tests green

# Hisho (JARVIS) — セッション引き継ぎ (2026-07-03 終業時点)

> 次セッションで**そのまま再開**するための地図。詳細は spec / plans / メモリを参照。

## 現在地

- ブランチ **main** / working tree clean / **Python 84 tests + Swift 30 tests green** / GitHub public に push 済
- **Plan 1〜3 (core / Swift 殻 / RAG) + Plan 4 スライス1 (forget) + 新しい会話ボタン + 電源トグル = 全部完成・main マージ済**
- **既定チャットモデル = `gemma4:12b`** (常駐 ~8.4GB。`HISHO_MODEL` env で上位モデルに切替可)。埋め込み = bge-m3
- アプリは JARVIS ブランド (髭アイコン) でメニューバー常駐稼働中
- 公開物に実名・内部 IP・ホスト名を書かない (author 中立、docs は「オーナー」表記)

## できていること (完成機能)

1. チャット (MenuBarExtra popover、SSE 逐次描画、popover 破棄耐性、平文出力)
2. 長期記憶 RAG (sqlite-vec + bge-m3): 別セッション想起・external 非汚染・status 現況優先・質問の自己エコー除外
3. 秘書知識の種まき (`scripts/seed_memory.py`、source_type='document')。追記で事実訂正も可 (例: 「JARVIS は MacBook 上の gemma4 で動く」を追記して誤認を修正)
4. バックアップ監視: launchd 収集 → 記憶上書き → 機器別実測日時で回答
5. **忘却 (Plan 4 スライス1)**: 「○○忘れて」で長期記憶を soft-delete。実 LLM で安全削除を実測検証済 (対象のみ削除・巻き添えなし・想起からも消滅・可逆)
6. **新しい会話ボタン (📝)**: popover ヘッダ。画面クリア + 新スレッド (session_id 再生成)。SQLite の会話は残る
7. **電源トグル (⏻)**: popover ヘッダ。モデルを手動アンロード (VRAM ~8GB 即解放) / ロード。30分アイドル自動アンロードに加えた手動制御

## 🔑 forget の実 LLM 教訓 (重要・コードレビューでは捕捉できず実機スモークのみが暴いた)

- **RAG memories に削除対象の事実が注入されると、ローカルモデルは forget ツールを呼ばず「消しました」と幻覚する** → 対策: forget 意図ターンは memories 注入をスキップ (`server.py` の is_forget)
- **ローカルモデルの tool-calling は非決定的** (qwen3.6 は memories 無しでも ~25% 幻覚、gemma4 は素直に発火) → 対策: **モデルが呼ばなければ server が決定的に forget を実行するフォールバック** (`server._forget_query` で対象語抽出)。破壊操作を「必ず安全」にする要
- keyword ゲートは imperative 形 (`忘れて|消して|消去|削除|覚えなくて`) で否定「忘れないで」を除外
- **閾値**: bge-m3 未正規化 L2 で直接キーワード一致 ≈0.82、無関係 ≈1.0+ → `tools.FORGET_THRESHOLD=0.85` (自然文前提。「テスト事実:」等のプレフィックス付きは距離が上がり外れる)
- **LLM ツール機能はコードレビューでなく実モデルスモークでしか検証できない**。別DB (`HISHO_DB`) + 別ポート (`HISHO_PORT`) で core を起動し curl で実測せよ

## 次の候補 (未着手)

- **Plan 4 スライス2: sensors** — 「今すぐバックアップ確認して/起動して」を JARVIS が実行 (tool-calling 基盤は完成済、あとは sensor ツールを `tools.REGISTRY` に足すだけ)
- 電源トグルの磨き: OFF 直後に一瞬緑に光る (healthz 3秒キャッシュ + ollama アンロード遅延)。probe cache TTL 短縮 or optimistic 表示で解消可
- keep_alive 短縮 (`HISHO_KEEP_ALIVE`) でアイドル時もメモリ解放 / gemma4:12b の品質が不足なら 26b-a4b に戻す
- `/history` 画面 / SMAppService (ログイン起動)

## 再開手順

```bash
cd ~/sandbox/menubar-hisho
core/.venv/bin/python -m pytest core/tests/ -q                      # 84 passed
cd HishoKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test  # 30 tests
# アプリ再ビルド (core を変えた時は build_core.sh 必須):
scripts/build_core.sh && cd HishoApp && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodegen generate && cd .. \
  && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project HishoApp/HishoApp.xcodeproj -scheme HishoApp -configuration Debug -derivedDataPath build/derived build \
  && open build/derived/Build/Products/Debug/Hisho.app
# 実 LLM スモーク (別DB/別ポートで実データを汚さず):
HISHO_DB=/tmp/smoke.db core/.venv/bin/python scripts/seed_memory.py "テスト事実..."
tail -f /dev/null | HISHO_DB=/tmp/smoke.db HISHO_PORT=51199 core/.venv/bin/python -m hisho_core &
curl -N -s -X POST http://127.0.0.1:51199/v1/chat/completions -H "X-Hisho-Source: popover" -d '{"session_id":"s","messages":[{"role":"user","content":"○○忘れて"}],"stream":true}'
```

## 落とし穴 (実測済・忘れると再度ハマる)

- `swift test` / `xcodebuild` / `xcodegen` は **DEVELOPER_DIR 前置必須**
- **core を変更したら `scripts/build_core.sh` 再実行**してから .app リビルド
- core 単体を background 起動する時は `tail -f /dev/null |` 前置 (stdin 即 EOF → 自死)
- **healthz の probe は3秒キャッシュ** + ollama のアンロードは ~1秒遅延 → 電源トグル直後は healthz が実態とズレる (unload/load エンドポイントで cache 無効化して緩和済)
- **sqlite-vec の vec0 (`vec_chunks_bge_m3`) は sqlite3 CLI で触れない** (module 無し) → 削除は Store 経由 (拡張ロード) で。chunks(id) + vec(rowid) + turns(id) の3系統を消す
- RAG 教訓: 質問文は索引しない / 現況は status チャンクに一元化 / 忘却往復を再索引しない (H1)
- JARVIS の自己認識 (使用モデル等) は DB の document チャンクに依存 → モデル変更時は自己紹介事実も更新 (追記 or forget)
- 収集系の個人設定は repo 外 (`~/Library/Application Support/Hisho/backup_targets.json`)

## 開発ポリシー (この repo で固定)

- plan=Opus/Fable / 実装=Sonnet サブエージェント / 設計段階で codex-review (usage limit 時は Fable セルフレビューで代替)
- SDD (subagent-driven): タスクごとに実装→レビュー→ controller が commit。公開前に実名/IP/ホスト名をスクラブ + squash マージで履歴も clean
- 公開物に実名を書かない。UI の色/font/挙動は実機確認してから確定 (電源トグルの ON バグは実機でしか出なかった)

## ファイル地図

```
docs/specs/2026-07-01-hisho-chat-mvp-design.md           # 設計仕様 (Llama-macOS 参照コンセプト)
docs/specs/2026-07-02-hisho-forget-tool-design.md        # forget 設計
docs/superpowers/plans/2026-07-01..03-*.md               # Plan 1-3 + forget
core/hisho_core/{config,store,context,sse,llm,server,lifecycle,rag,tools,__main__}.py
  server.py: /v1/chat/completions (tool ループ+forget), /v1/admin/model/{load,unload}, /healthz, /history
  tools.py:  forget_memories + REGISTRY (sensors はここに足す)
HishoKit/  HishoApp/  scripts/{seed_memory,build_core}.py  core/SMOKE.md
```

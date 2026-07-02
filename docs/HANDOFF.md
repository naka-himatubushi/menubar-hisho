# Hisho (JARVIS) — セッション引き継ぎ (2026-07-02 終業時点)

> 次セッションで**そのまま再開**するための地図。詳細は spec / plans / メモリを参照。

## 現在地

- ブランチ **main** / working tree clean / **Python 62 tests + Swift 26 tests green**
- **Plan 1 (Python core) + Plan 2 (Swift 殻 + .app) + Plan 3 (RAG 長期記憶) = 全部完成・マージ済**
- GitHub 公開済 (public, MIT)。全履歴 author は中立名義にクリーニング済 — **今後も実名・内部 IP・ホスト名をコミットしない**
- アプリは JARVIS ブランド (髭アイコン) でメニューバー常駐稼働中。persona 個人化 + プロフィール/インフラ知識の種まき + バックアップ状況の定期収集 (launchd 毎日 9:00) まで運用中

## できていること (完成機能)

1. チャット (MenuBarExtra popover、SSE 逐次描画、popover 破棄耐性、平文出力)
2. 長期記憶 RAG (sqlite-vec + bge-m3): 別セッション想起・external 非汚染・status 現況優先・質問の自己エコー除外
3. 秘書知識: プロフィール/プロジェクト/接続手順を種まき済 (`scripts/seed_memory.py` で追加可)
4. バックアップ監視 (方式①): launchd 収集 → 記憶上書き → 「バックアップ大丈夫?」に機器別実測日時で回答。異常は先頭で警告・断定しない
5. 自動アンロード対応 (30 分アイドル → 次の発話で自動再ロード、warming 中も送信可)

## 次の候補 (未着手)

- **Plan 4 (本命): sensors + tool calling** — 「今すぐ確認して」でその場で実行 + 未実行ならバックアップ起動を JARVIS が実行。設計メモはメモリ側 (project_menubar_hisho / user_backup_infra)
- UI 磨き / `/history` 画面 / 新規会話ボタン / SMAppService (ログイン起動) / ruri との検索精度比較

## 再開手順

```bash
cd ~/sandbox/menubar-hisho
core/.venv/bin/python -m pytest core/tests/ -q                      # 62 passed
cd HishoKit && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test  # 26 tests
# アプリ再ビルド (core を変えた時):
scripts/build_core.sh && cd HishoApp && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodegen generate && cd .. \
  && DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -project HishoApp/HishoApp.xcodeproj -scheme HishoApp -configuration Debug -derivedDataPath build/derived build \
  && open build/derived/Build/Products/Debug/Hisho.app
```

## 落とし穴 (実測済・忘れると再度ハマる)

- `swift test` / `xcodebuild` / `xcodegen` は **DEVELOPER_DIR 前置必須** (xcode-select が CLT のため)
- **core を変更したら `scripts/build_core.sh` 再実行**してから .app リビルド (古いツリーを embed する事故を 1 回やった)
- core 単体を background 起動する時は `tail -f /dev/null |` 前置 (stdin 即 EOF → 自死)
- RAG 教訓: 質問文は索引しない / 現況は status チャンクに一元化 (静的メモに状態を書かない) / 知識に検索枠保証
- 収集系の個人設定は repo 外 (`~/Library/Application Support/Hisho/backup_targets.json`)

## 開発ポリシー (この repo で固定)

- plan=Opus / 実装=Sonnet サブエージェント (commit 禁止、controller がレビュー後コミット) / 設計段階で codex-review
- **セッション運用: フェーズごとに本ファイルを更新して /clear。日常モデルは Opus 4.8、Fable は設計/難障害/最終レビューの日のみ**
- 公開物に実名を書かない。UI の色/font は実機確認してから確定

## ファイル地図

```
docs/specs/2026-07-01-hisho-chat-mvp-design.md      # 設計仕様 (15節)
docs/superpowers/plans/2026-07-01-hisho-python-core.md   # Plan 1
docs/superpowers/plans/2026-07-02-hisho-swift-shell.md   # Plan 2
docs/superpowers/plans/2026-07-02-hisho-rag-memory.md    # Plan 3
core/hisho_core/{config,store,context,sse,llm,server,lifecycle,rag,__main__}.py
HishoKit/  HishoApp/  scripts/  core/SMOKE.md
```

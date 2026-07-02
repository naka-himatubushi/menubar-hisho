# Hisho — セッション引き継ぎ (2026-07-01)

> 次セッションで**そのまま再開**するための地図。詳細は spec/plan/ledger を参照。

## 現在地

- ブランチ **main** / HEAD `0e0e4cd` / **35 tests green** / working tree clean。
- **Plan 1(Python core `hisho_core`)= 完成・main マージ済**。
- 次にやるのは **Plan 2(Swift 殻 + パッケージング)= 未着手**。

## 何ができたか (Plan 1)

`hisho_core` = ローカル ollama の前に立つ「秘書レイヤ」サーバ。**単体で完動**（Swift 殻なしでも使える）。

- API: `POST /v1/chat/completions`(OpenAI 互換 SSE。`X-Hisho-Source: popover` で persona+履歴をサーバ側合成、無ければ外部ツール素通し)、`GET /healthz`(2段階 readiness)、`GET /v1/models`、`GET /history`。
- 記録: 全ターンを SQLite(WAL, `~/Library/Application Support/Hisho/secretary.db`)。RAG 拡張の seam 済(chunks/embeddings は追加テーブルで無痛)。
- モジュール: `config / store / context / sse / llm / server / lifecycle / __main__`(各 role docstring 付き)。
- モデル: `qwen3.6:35b-a3b`(llm.py/config.py の単一設定値)。

## 次にやること (Plan 2 — Swift 殻 + packaging)

spec `docs/specs/2026-07-01-hisho-chat-mvp-design.md` の §3-5, §11, §14 が設計元。

1. **SwiftUI MenuBarExtra** の薄殻(アイコン→popover: 会話ログ逐次描画 + 入力)。stream はアプリ層 store で保持(popover 破棄で切れない)。状態: starting core / warming model / ready / ollama-down / core-stopped。
2. **子プロセス供給**: 殻が同梱 `hisho_core` を child で起動/監視/終了。**stdin を Pipe で保持**(親死→EOF→core 自死。lifecycle.py 実装済の watcher が受ける)。健康は `/healthz` ポーリング。
3. **同梱 Python**: uv 管理 **python-build-standalone CPython 3.13** を `Contents/Resources/core/` に丸ごとコピー(symlink venv 不可)。deps を直接 install。
4. **ビルド/署名**: **Xcode あり** → .app/Info.plist(`LSUIElement`, `NSAllowsLocalNetworking`)/署名/`SMAppService`/entitlements を Xcode に任せる。**Sandbox OFF・Hardened Runtime OFF・notarize なし**(自機ローカル)。notarize-ready の道は spec §5 に文書化のみ。
5. **macOS 26 focus スパイク(着手前 30分)**: MenuBarExtra `.window` の TextField focus を実機確認。ダメなら AppKit `NSStatusItem`+`NSPopover` へ退避(chat view は host-agnostic に)。
6. bundle relocation smoke(.app を移動して core が動く)。

## 再開手順(コマンド)

```bash
cd ~/sandbox/menubar-hisho
# テスト
core/.venv/bin/python -m pytest core/tests/ -q        # 35 passed 期待
# core を単体起動(ollama 稼働前提)
core/.venv/bin/python -m hisho_core                    # 127.0.0.1:51100
# 実機スモーク手順は core/SMOKE.md(chat/history/外部ツール互換/親死自死/切断partial)
```
venv が壊れてたら: `cd core && /opt/homebrew/bin/python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'`

## 引き継ぐ重要決定・落とし穴

- **エンジンは ollama 据え置き**(llama.cpp に乗り換えない)。「4MB tiny」は諦め(同梱 CPython で ~90MB)。
- **127.0.0.1 のみ bind**(0.0.0.0 禁止)。データは Mac の外に出さない(テレメトリ無し)。
- **推論モデル**: 既定 `think:false`。`<think>` を表示にもログにも**絶対に連結しない**。
- **num_ctx を毎回明示**(既定 8192)。既定 4096 だと古い=persona から捨てられ秘書が指示を忘れる。
- **記録の finally は `anyio.CancelScope(shield=True)` で保護**(client 切断時に lock 待ちで partial 書き漏れる Opus 指摘 F1 の対策)。cancel→partial は決定的テスト(`body_iterator.aclose()`)で担保。
- ポート **51100**(ollama 11434 と離す)。衝突時 reclaim→`:0` fallback→core.json 実ポート。
- SQLite 書き込みは `asyncio.Lock` + `anyio.to_thread` 直列化、stream 中は lock 非保持。seq は INSERT 内アトミック。

## 開発ポリシー(この repo で固定)

- **plan=Opus / 実装=Sonnet・Haiku サブエージェント / サブエージェントは commit しない**(controller がレビュー後コミット)。
- 設計段階で **codex-review**(gpt-5.5)。SDD: タスク毎 実装→個別レビュー→修正、最後に全ブランチレビュー(最上位モデル)。
- README/公開物に**実名を出さない**(筆者/開発者表記)。
- UI 色/font/motion は**実機/ブラウザで確認**してから確定。

## 受容済みの負債 / follow-up

- **2プロセス reconciliation 未配線**(Opus #5): `python -m hisho_core` を手で2重起動すると別ポートで同じ DB を共有し seq 衝突しうる。MVP では SO_REUSEADDR で安全に `:0` fallback。**Plan 2 の Swift supervisor が唯一起動を保証**する前提。`lifecycle.is_our_stale_core` は名前が実態(=生存判定)とズレ(リネーム候補)。
- `wal_autocheckpoint` は既定と同値だが明示済。

## 別スレ(停止中)

- **skillup 伴走開発**: 選定済プロジェクト=③「ベクトル検索エンジン自作(FAISS/Chroma の中身)」。伴走スタイル=**Sonnet 段階実装→開発者が全行理解(予測/説明/破壊)**。menubar とは別物として分離。ショートリスト全8案は当セッション workflow 出力にあり(必要なら再生成)。

## ファイル地図

```
docs/specs/2026-07-01-hisho-chat-mvp-design.md     # 設計仕様(15節・硬化済)
docs/superpowers/plans/2026-07-01-hisho-python-core.md  # Plan 1(C1-C5 addendum 含む)
docs/HANDOFF.md                                    # これ
core/pyproject.toml  core/SMOKE.md
core/hisho_core/{config,store,context,sse,llm,server,lifecycle,__main__}.py
core/tests/test_*.py                               # 35 tests
.superpowers/sdd/progress.md                       # SDD ledger(gitignore・ローカルのみ)
```

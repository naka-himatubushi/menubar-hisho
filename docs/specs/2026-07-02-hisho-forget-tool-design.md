# Hisho (JARVIS) — 自己記憶削除ツール (forget) 設計仕様

> 役割: Plan 4 (tool calling) の第1スライスの設計書。JARVIS 自身が会話中に「忘れて」を実行できるようにする tool-calling 基盤 + 最初のツール `forget_memories` を定義する。実装計画は writing-plans で別途作る。

最終更新: 2026-07-02

---

## 1. 背景と目的

「猫を飼ってると誤記憶している。消して」とチャットで頼むと、JARVIS は「削除しました」と答えるが **実際には何も消えない**。原因は chat の LLM に tool-calling が無く、「削除しました」は動作でなく作文 (hallucination) だから。しかも削除依頼の往復自体が新チャンクとして索引され、対象語がむしろ増える (2026-07-02 実測)。

本スライスの目的: **JARVIS 自身が記憶を実際に忘れられる**ようにする。そのための tool-calling 基盤と、最初のツール `forget_memories` を実装する。

## 2. スコープ

対象:
- tool-calling 基盤 (Ollama native tools を使ったエージェントループ)
- ツール `forget_memories(query)` = 記憶の soft-delete

非対象 (次スライス以降):
- sensors 系ツール (バックアップ状況確認 / 起動)
- undo (「戻して」) ツール — soft-delete なので手動復元は可能。音声 undo は後回し
- 「覚えて」ツール (追加は既に turn 索引で実質できている)
- 削除前の確認 UX

## 3. 挙動 (決定事項)

中沢さん決定:
- **即 soft-delete・確認なし** — 「忘れて」で即実行。友好的で速い
- **事後に何を忘れたか報告** — 確認を省く代わりの安全網。件数と内容を必ず提示
- **undo ツールなし** — soft-delete で物理的には残るため、必要なら後述の手動手順で復元
- 削除は **soft** (物理削除しない) = 誤爆しても手で戻せる

## 4. データモデル (additive migration v2 → v3)

`chunks` に列を2つ足す (STRICT テーブルは DEFAULT 付き ADD COLUMN 可):

```sql
ALTER TABLE chunks ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
ALTER TABLE chunks ADD COLUMN forgotten_at INTEGER;   -- soft-delete 時刻(ms)、active は NULL
PRAGMA user_version = 3;
```

- `search_chunks` に `AND c.status='active'` を追加 → forgotten は検索・想起に出ない
- soft-delete = `UPDATE chunks SET status='forgotten', forgotten_at=? WHERE id IN (...)`
- **手動復元手順** (undo ツール未実装のため運用者用): `UPDATE chunks SET status='active', forgotten_at=NULL WHERE forgotten_at=<該当バッチの時刻>` (同一 forget 呼び出しは同じ forgotten_at を共有するのでバッチ単位で戻せる)
- vec0 (`vec_chunks_bge_m3`) と `embeddings` は触らない (soft なので索引ごと残す。物理削除に切替える時だけ消す)

## 5. `forget_memories` ツール

ツール定義 (Ollama tools 形式):
```
name: forget_memories
description: ユーザーが特定の記憶を明示的に「忘れて/消して/覚えなくていい」と要求した時だけ呼ぶ。query には忘れる対象を表す語句を入れる (例: 「猫」「私の好物」)。
parameters: { query: string (必須) }
```

実行 (`tools.py` の async 関数):
1. `rag.embed([query])` で埋め込み。失敗時はツールを実行せず「今 記憶を整理できません」を返す (握りつぶさない)
2. `store.search_forgettable(vec, k)` = `status='active'` かつ `source_type IN ('turn','document')` の kNN。`status`(バックアップ現況) は対象外
3. 距離 < `FORGET_THRESHOLD` (既定 0.55、要スモーク調整。距離は小さいほど近い。Hisho の bge-m3 は L2 正規化していないため実データ「猫」等で校正する) のものだけ採用
4. 採用が `MAX_FORGET` (既定 15) を超えたら上位のみ soft-delete し `truncated=true` を返す (**silent cap 禁止** — 報告に「N件中M件」と出す)
5. 該当を soft-delete (同一 `forgotten_at`)
6. 戻り値 (JSON): `{ "count": M, "matched": N, "truncated": bool, "items": ["忘れた内容の先頭..."] }`

JARVIS はこの戻り値を根拠に報告する (例:「以下3件を忘れました: ・… 戻したい場合は言ってください」)。**巻き添え可視化**: 混在チャンク (例「カレーと猫モチ」) が対象に入ったら items にそのまま出るので、ユーザーが気づいて対処できる。

新規 store メソッド: `search_forgettable(query_vec, k)` (id+content+distance を返す)、`soft_delete_chunks(ids, now_ms)`。

## 6. tool-calling 機構 (エージェントループ)

採用案: **tools を通常の streaming `/api/chat` に渡し、tool_calls が出た時だけ2回目を回す**。通常会話は1呼び出しで追加負荷ゼロ、forget 発火時だけ2回目 (実行→結果注入→最終回答 streaming)。

- `llm.chat_stream`: body に `tools` を追加 (呼び出し側が渡す。無ければ従来通り)
- `llm.iter_ollama_events`: `message.tool_calls` を検出したら `{"type":"tool_call","id","name","arguments"}` を emit。`done_reason=="tool_calls"` を done で扱う
- `server.gen()`: ループを挟む
  - stream から `delta` → 従来通り SSE
  - `tool_call` → messages に `{role:assistant, tool_calls:[...]}` と `{role:tool, content:<戻り値JSON>}` を足し、`chat_fn` を再呼び出しして最終回答を stream
  - 上限 `MAX_TOOL_ITERS = 3` (無限ループ防止)
- tools は **`source=='popover'` の時だけ**渡す (external passthrough は据え置き)
- tool 実行は速い (embed + UPDATE) ため、実行中は無音で最終回答だけ stream。中間の「記憶を整理中…」表示は任意 (v1 は無し)
- ツールレジストリ = `tools.py` に name→async fn の dict。将来 sensors を足しやすくする seam

## 7. persona 修正

現 PERSONA の「コマンド実行やリアルタイム確認の手段を持ちません」を精密化:
- 「記憶の忘却は forget 機能で実際に実行できる。実行したら結果 (件数・内容) を必ず事実として報告する」
- 「バックアップ起動やリアルタイム確認など、それ以外の実行手段はまだ持たない。持たないことは実行したように装わない」

tool 戻り値という ground truth があるので、忘却に関する「盛り」は構造的に消える (猫事件の根治)。

## 8. 失敗条件・安全 (先出し・断定しない)

- **モデルが forget を誤爆**: soft-delete なので手動復元可 + 事後報告で気づける + `MAX_FORGET` で大量削除防止。確実に誤爆しないとは言わない
- **tool_calls 非対応/不安定**: qwen3 系は Ollama tools 対応だが、この量子化 (qwen3.6:35b-a3b) での信頼性は**実装の最初にスモークテストで確認する**。不安定なら Approach C (キーワード検知で forget 直実行) にフォールバックする分岐を残す。「確実に動く」とは言わない
- **embed 失敗**: forget せず正直に「整理できません」。`except: pass` 禁止、`logger` に残す
- **曖昧クエリ (「全部忘れて」等)**: 閾値 + `MAX_FORGET` で歯止め。全消しは v1 で対応しない (報告で件数が出るので暴走は可視)

## 9. テスト

- store (unit): `soft_delete_chunks` が status を反転 / `search_chunks` が forgotten を除外 / `search_forgettable` が id を返す / migration v3 が既存 v2 DB に additive 適用
- llm (unit): `iter_ollama_events` が `message.tool_calls` で `tool_call` イベントを吐く / tools 無し時は従来と同一
- tools (unit): `forget_memories` が閾値・cap・source_type 除外を守り、正しい報告 JSON を返す / embed 失敗時に安全に帰る
- server (integration): fake chat_fn が tool_call → server がツール実行 → 2回目呼び出し → 最終回答が stream される / `MAX_TOOL_ITERS` で止まる / external では tools を渡さない
- e2e (手動): 「猫のこと忘れて」→ retrieve に猫が出ない → 手動復元で戻る

## 10. 実装順 (writing-plans で詳細化)

1. migration v3 (status/forgotten_at) + `search_chunks` フィルタ
2. store メソッド (`search_forgettable`, `soft_delete_chunks`)
3. `tools.py` (レジストリ + `forget_memories`)
4. `llm` の tool イベント対応
5. `server.gen()` のツールループ
6. persona 修正
7. 各層テスト → 手動 e2e + スモーク (tool_calls 信頼性)

実装は [feedback_sonnet_subagent_implementation] 通り plan=Opus/実装=Sonnet サブエージェント (commit 禁止)、設計段階で codex-review。

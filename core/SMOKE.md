# core/SMOKE.md — 実 ollama での手動確認

実際の環境での動作確認(自動テストの範囲外)。

## 前提

- ローカル ollama が稼働中
- `qwen3.6:35b-a3b` が pull 済み
- core が `.venv` に入った状態

## 基本動作確認

### 1. 起動

```bash
cd core
HISHO_PORT=51100 python -m hisho_core
```

サーバーが起動し、stdin で親の死を監視開始。

### 2. 健康状態確認

```bash
curl -s 127.0.0.1:51100/healthz | python -m json.tool
```

期待値:

```json
{
  "core": true,
  "ollama": {
    "reachable": true,
    "version": "..."
  },
  "model": {
    "present": true,
    "loaded": false
  }
}
```

### 3. チャット(ストリーミング)

```bash
curl -N -H "X-Hisho-Source: popover" -H "Content-Type: application/json" \
  -d '{"messages":[{"role":"user","content":"自己紹介して"}],"session_id":"smoke1"}' \
  127.0.0.1:51100/v1/chat/completions
```

期待値: `data: {...}` が逐次流れ、末尾に `data: [DONE]`。

### 4. 記録確認

```bash
curl -s "127.0.0.1:51100/history?session_id=smoke1" | python -m json.tool
```

期待値: user と assistant の2ターンが `status: complete` で残る。

### 5. 外部ツール互換(任意)

OpenAI 互換クライアント(Chatbox, OpenAI CLI など)で以下を設定:

- `base_url`: `http://127.0.0.1:51100/v1`
- `model`: `qwen3.6:35b-a3b`

疎通可能なことを確認。

### 6. 親死自死(任意)

起動プロセスの stdin を閉じると core が自動終了することを確認:

```bash
# ターミナル1: サーバー起動
cd core && HISHO_PORT=51100 python -m hisho_core

# ターミナル2: プロセス PID を確認
ps aux | grep hisho_core

# ターミナル1 で Ctrl-D を押す (stdin EOF)
# → core が自動終了
```

---

## **⚠️ C1: クライアント切断時の部分ログ記録確認**

**目的**: 流送中に Ctrl-C でクライアント接続が切れた場合、assistant ターンが `status: partial` で正しく記録される(失われない、`complete` にもならない)ことを検証。これが status のデフォルト値が `partial` である理由。

### 手順

1. **サーバー起動**

   ```bash
   cd core && HISHO_PORT=51100 python -m hisho_core
   ```

2. **流送リクエスト開始(切断なし)**

   ```bash
   SESSION_ID="partial-test-$(date +%s)"
   curl -N -H "X-Hisho-Source: popover" -H "Content-Type: application/json" \
     -d "{\"messages\":[{\"role\":\"user\",\"content\":\"100 から 200 まで数えて\"}],\"session_id\":\"$SESSION_ID\"}" \
     127.0.0.1:51100/v1/chat/completions
   ```

3. **mid-stream で Ctrl-C**

   数秒(3～5秒)待ってから Ctrl-C を押し、curl を中断。

4. **記録確認**

   ```bash
   curl -s "127.0.0.1:51100/history?session_id=$SESSION_ID" | python -m json.tool
   ```

   期待値:

   ```json
   {
     "session_id": "partial-test-...",
     "turns": [
       {
         "role": "user",
         "content": "100 から 200 まで数えて",
         "status": "complete"
       },
       {
         "role": "assistant",
         "content": "100, 101, 102, ...",
         "status": "partial"
       }
     ]
   }
   ```

   **確認ポイント**:

   - ✓ assistant ターンが `status: partial` で記録されている(失われていない)
   - ✓ content に部分的なテキストが残っている
   - ✓ `status: complete` ではない(完了フラグが立っていない)

### 背景

- SSE ハンドラは `finally` で `finalize_turn(status='complete')` を呼ぶが、
- クライアント切断時は `finally` に到達する前に例外が発生し、
- DB は status を `partial` (デフォルト)のまま保持する。
- これにより、部分応答がシステムに記録され、後で再度質問できる。


## Swift 殻 E2E (Plan 2)

前提: ollama 稼働 (`ollama serve`)、`scripts/build_core.sh` + Task 10 ビルド済。

1. **チャット往復**: .app 起動 → メニューバー → popover → 挨拶 → 逐次描画で応答。
2. **記録確認**: `sqlite3 "$HOME/Library/Application Support/Hisho/secretary.db" \
   "SELECT role, status, substr(content,1,20), json_extract(meta,'$.source') FROM turns ORDER BY id DESC LIMIT 4;"`
   → user/assistant が complete、source=popover。
3. **popover 破棄耐性**: 長い応答を要求 → streaming 中に popover を閉じ 3 秒後に再度開く → 続きが表示されている。
4. **graceful 終了**: アプリ終了 → `pgrep -f hisho_core` が空(開発中の別 core が居ない前提。居るなら core.json の pid で確認)。
5. **強制終了(孤児化なし)**: 再起動 → `kill -9 <Hisho pid>` → 2 秒以内に `pgrep -f hisho_core` が空。
6. **ollama down 表示**: `OLLAMA_HOST=http://127.0.0.1:9 build/derived/Build/Products/Debug/Hisho.app/Contents/MacOS/Hisho`
   → バナー「ollama に接続できません」→ 終了。
7. **core stopped 表示**: 通常起動 → `kill -9 $(pgrep -f hisho_core)` → バナー「core 停止」→ [再起動] → 復帰。
8. **外部ツール互換**: 稼働中に `curl -N http://127.0.0.1:51100/v1/chat/completions -H 'Content-Type: application/json' \
   -d '{"model":"qwen3.6:35b-a3b","stream":true,"messages":[{"role":"user","content":"1+1は?"}]}'`
   → SSE が流れ、DB に source=external で記録。
9. **relocation + 孤児化**: `scripts/smoke_relocation.sh` → 2 つの OK。
10. **egress なし**: チャット後 `scripts/check_no_egress.sh` → 「OK: 非 loopback 接続なし」。

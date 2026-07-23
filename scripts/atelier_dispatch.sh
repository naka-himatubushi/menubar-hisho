#!/bin/bash
# 役割: JARVIS の工房発注 action から呼ばれる、laguna 工房 (Studio のローカル LLM +
#       Claude Code ヘッドレス) への実装ジョブ投入スクリプト。
# 使い方: atelier_dispatch.sh <repo-key> <task-text>
#   repo-key : ~/Library/Application Support/Hisho/atelier_targets.json の
#              {"repos": {"<key>": "/abs/path"}} に登録済みのキー (許可リスト制)。
#   task-text: 実装タスクの発注文 (受け入れ基準込み推奨)。argv 1要素で受ける。
# 動作: 対象 repo に atelier/<timestamp> ブランチを作成 → laguna (direct 接続) で
#       claude -p をバックグラウンド起動 → 完了時に diff stat / テスト残骸を
#       ~/AtelierOut/<timestamp>-<repo-key>.md へ書き、macOS 通知を出す。
# 安全: commit はしない (発注文でも禁止し、検収者が行う)。許可リスト外 repo は拒否。
#       ツールは Read/Glob/Grep/Write/Edit/Bash(uv run) の白名単。merge しない。
set -euo pipefail

REPO_KEY="${1:?usage: atelier_dispatch.sh <repo-key> <task-text>}"
TASK_TEXT="${2:?task-text required}"

TARGETS="$HOME/Library/Application Support/Hisho/atelier_targets.json"
OUT_DIR="$HOME/AtelierOut"
TS="$(date +%Y%m%d-%H%M%S)"
MODEL="laguna-bench:s21-q4"
STUDIO="http://naka-studio:11434"

[[ -f "$TARGETS" ]] || { echo "atelier_targets.json が無い: $TARGETS" >&2; exit 1; }

REPO_PATH=$(python3 -c '
import json, sys
repos = json.load(open(sys.argv[1])).get("repos", {})
print(repos.get(sys.argv[2], ""))' "$TARGETS" "$REPO_KEY")
[[ -n "$REPO_PATH" && -d "$REPO_PATH" ]] || { echo "許可リスト外または実在しない repo-key: $REPO_KEY" >&2; exit 1; }

mkdir -p "$OUT_DIR"
OUT_MD="$OUT_DIR/$TS-$REPO_KEY.md"
BRANCH="atelier/$TS"
TASK_FILE="$OUT_DIR/.task-$TS.txt"
printf '%s\n\nIMPORTANT: Do NOT run git commit. After implementing, run the full test suite and verify acceptance criteria against real behavior before finishing.\n' "$TASK_TEXT" > "$TASK_FILE"

# Studio ollama 生存確認 (落ちていたら早期失敗して通知)
if ! curl -s --max-time 5 "$STUDIO/api/version" >/dev/null; then
  osascript -e 'display notification "Studio Ollama に届きません" with title "JARVIS 工房"' || true
  echo "studio unreachable" >&2; exit 1
fi

(
  cd "$REPO_PATH"
  git checkout -b "$BRANCH" >/dev/null 2>&1 || git checkout "$BRANCH" >/dev/null 2>&1
  export ANTHROPIC_BASE_URL="$STUDIO" ANTHROPIC_AUTH_TOKEN=ollama \
         ANTHROPIC_SMALL_FAST_MODEL="$MODEL" NO_PROXY=127.0.0.1 \
         API_TIMEOUT_MS=1200000 API_FORCE_IDLE_TIMEOUT=0
  set +e
  RESULT_JSON=$(claude --model "$MODEL" -p "$(cat "$TASK_FILE")" \
    --allowedTools "Read" "Glob" "Grep" "Write" "Edit" "Bash(uv run:*)" "Bash(ls:*)" \
    --output-format json 2>&1 | tail -c 2000)
  RC=$?
  set -e
  {
    echo "# 工房納品: $REPO_KEY ($TS)"
    echo
    echo "- repo: $REPO_PATH"
    echo "- branch: $BRANCH"
    echo "- exit: $RC"
    echo
    echo "## 発注文"
    echo '```'
    cat "$TASK_FILE"
    echo '```'
    echo
    echo "## 変更ファイル"
    echo '```'
    git status --short
    git diff --stat | tail -8
    echo '```'
    echo
    echo "## claude 終端出力 (tail)"
    echo '```json'
    echo "$RESULT_JSON"
    echo '```'
    echo
    echo "検収前。merge 禁止。レビュー: git -C $REPO_PATH diff"
  } > "$OUT_MD"
  osascript -e "display notification \"$REPO_KEY: 納品 (branch $BRANCH)。レビュー待ち\" with title \"JARVIS 工房\"" || true
) &

echo "dispatched: $REPO_KEY branch=$BRANCH out=$OUT_MD"

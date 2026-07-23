#!/bin/bash
# 役割: launchd から毎朝呼ばれ、稼働中の JARVIS サーバに「朝の報告」を聞いて
#       期限行 (⏰/⚠️) を macOS 通知に出すプッシュ配信スクリプト。
# 前提: JARVIS.app が起動済み (:51100)。停止中なら通知を出さず静かに終了する
#       (毎朝の起動は強制しない — 利用者の「ログイン自動起動は不要」方針)。
# インストール: launchd plist (com.jarvis.morning-briefing.plist) から呼ぶ。
set -euo pipefail

PORT="${HISHO_PORT:-51100}"
BASE="http://127.0.0.1:$PORT"

curl -s --max-time 3 "$BASE/healthz" >/dev/null || exit 0  # JARVIS 停止中は静かに去る

RESP=$(curl -s --max-time 120 "$BASE/v1/chat/completions" \
  -H 'content-type: application/json' -H 'X-Hisho-Source: popover' \
  -d '{"messages":[{"role":"user","content":"朝の報告"}]}' || true)

# SSE chunk から content を復元し、期限行 (⏰/⚠️) を最大3行抽出
LINES=$(printf '%s' "$RESP" \
  | grep -oE '"content": "[^"]*"' | sed 's/"content": //' | tr -d '"' \
  | tr -d '\n' | sed 's/\\n/\n/g' | grep -E '⏰|⚠️' | head -3 || true)

if [[ -n "$LINES" ]]; then
  BODY=$(printf '%s' "$LINES" | tr '\n' ' / ')
else
  BODY="ブリーフィング準備完了 (JARVIS を開いて確認)"
fi
osascript -e "display notification \"${BODY//\"/}\" with title \"JARVIS 朝ブリーフィング\"" || true

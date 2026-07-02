#!/usr/bin/env bash
# 役割: ビルド済 .app を /tmp に移動して起動し、core が動く(relocatable)ことと
#       親死→core 自死(stdin EOF)を自動確認する(spec §4/§6)。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC_APP="build/derived/Build/Products/Debug/Hisho.app"
DST_APP="/tmp/Hisho-reloc.app"
CORE_JSON="$HOME/Library/Application Support/Hisho/core.json"

[ -d "$SRC_APP" ] || { echo "error: 先に Task 10 のビルドを実行" >&2; exit 1; }

rm -rf "$DST_APP"
ditto "$SRC_APP" "$DST_APP"

"$DST_APP/Contents/MacOS/Hisho" &
APP_PID=$!
trap 'kill -9 $APP_PID 2>/dev/null || true' EXIT

# core.json 出現 → healthz を待つ (最大 15 秒)
PORT=""
for i in $(seq 1 30); do
  sleep 0.5
  PORT=$(python3 -c "import json;print(json.load(open('$CORE_JSON'))['port'])" 2>/dev/null) || continue
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
done
curl -sf "http://127.0.0.1:$PORT/healthz" | grep -q '"core":true' \
  && echo "OK: relocated .app から core 起動" \
  || { echo "FAIL: healthz 不達" >&2; exit 1; }

CORE_PID=$(python3 -c "import json;print(json.load(open('$CORE_JSON'))['pid'])")

# 親を強制殺害 → stdin EOF → core 自死を確認 (最大 5 秒)
kill -9 "$APP_PID"
trap - EXIT
for i in $(seq 1 10); do
  sleep 0.5
  if ! kill -0 "$CORE_PID" 2>/dev/null; then
    echo "OK: 親死→core 自死 (孤児化なし)"
    rm -rf "$DST_APP"
    exit 0
  fi
done
echo "FAIL: core が孤児化 (pid $CORE_PID)" >&2
exit 1

#!/bin/bash
# 役割: ビルド済み JARVIS (Hisho.app) を /Applications/JARVIS.app へ配備するスクリプト。
# 再ビルド後に必ず実行する。忘れると /Applications 側が古いビルドのまま動き続ける。
# やること: 起動中の JARVIS を停止 → ditto でコピー → 新しい方を起動。
# 使い方: scripts/deploy_app.sh
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)/build/derived/Build/Products/Debug/Hisho.app"
DST="/Applications/JARVIS.app"

[[ -d "$SRC" ]] || { echo "ビルド成果物が見つからない: $SRC (先に xcodebuild を実行)" >&2; exit 1; }

# 起動中なら止める (二重起動と使用中ファイルの上書きを避ける)
pkill -x Hisho 2>/dev/null && sleep 1 || true
pkill -f 'python.*hisho_core' 2>/dev/null || true

ditto "$SRC" "$DST"
open "$DST"
echo "配備完了: $DST"

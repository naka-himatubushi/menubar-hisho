#!/usr/bin/env bash
# 役割: Xcode ビルド中に build/core-dist/python を .app の Contents/Resources/core/python へ rsync。
#       ビルド成果物の署名前フェーズで走る(Run Script)。
set -euo pipefail

SRC="${PROJECT_DIR}/../build/core-dist/python"
DST="${TARGET_BUILD_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/core/python"

if [ ! -d "$SRC" ]; then
  echo "error: $SRC がない。先に scripts/build_core.sh を実行" >&2
  exit 1
fi

mkdir -p "$DST"
rsync -a --delete "$SRC/" "$DST/"
echo "embedded python core -> $DST"

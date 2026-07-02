#!/usr/bin/env bash
# 役割: uv で python-build-standalone CPython 3.13 を取得し、hisho_core と deps を
#       直接インストールした同梱用ツリーを build/core-dist/python に組み立てる。
#       symlink venv は .app 移動で壊れるため「ツリー丸ごと + 直接 install」(spec §4)。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_SPEC="cpython-3.13"
DIST="build/core-dist"
PY_DIR="$DIST/python"

rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> python-build-standalone 取得 ($PYTHON_SPEC)"
uv python install "$PYTHON_SPEC" --install-dir "$DIST/uv-python"

SRC_TREE=$(find "$DIST/uv-python" -maxdepth 1 -type d -name 'cpython-3.13*' | head -1)
[ -n "$SRC_TREE" ] || { echo "error: python ツリーが見つからない" >&2; exit 1; }
mv "$SRC_TREE" "$PY_DIR"
rm -rf "$DIST/uv-python"

PY="$PY_DIR/bin/python3"
echo "==> hisho_core + deps を直接インストール"
# uv 管理ツリーは EXTERNALLY-MANAGED マーカー付き → 同梱専用の私有ツリーなので明示的に上書き
uv pip install --python "$PY" --break-system-packages ./core

echo "==> import 検証"
"$PY" -c "import hisho_core, fastapi, uvicorn, httpx; print('bundle imports OK')"

echo "==> relocatable 検証(ツリーを移動しても import できるか)"
RELOC="/tmp/hisho-reloc-check-$$"
cp -R "$PY_DIR" "$RELOC"
"$RELOC/bin/python3" -c "import hisho_core; print('relocation OK')"
rm -rf "$RELOC"

du -sh "$PY_DIR"
echo "==> core-dist ready: $PY_DIR"

#!/usr/bin/env bash
# 役割: Hisho / core / ollama が loopback 以外に接続していないことを目視確認する(spec §11)。
# 使い方: チャットを 1 往復してから実行。出力ゼロ = OK。
FOUND=$(lsof -i -nP 2>/dev/null | grep -iE 'hisho|python3.*hisho_core|ollama' \
        | grep -vE '127\.0\.0\.1|\[::1\]|localhost' || true)
if [ -z "$FOUND" ]; then
  echo "OK: 非 loopback 接続なし"
else
  echo "確認が必要な接続:"
  echo "$FOUND"
fi

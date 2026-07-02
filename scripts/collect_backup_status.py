"""バックアップ状況コレクタ: 設定ファイルの機器ごとにコマンドを実行し、
結果を 1 つの「最新バックアップ状況」ドキュメントとして JARVIS の長期記憶に上書き保存する。

- 設定: ~/Library/Application Support/Hisho/backup_targets.json
    {"devices": [{"name": "表示名", "cmd": "シェルコマンド"}, ...]}
  (ホスト名や IP 等の個人情報は設定ファイル側に置き、このスクリプトは汎用に保つ)
- 保存: chunks に source_type='status' で upsert (旧 status は vec0 含め削除してから追加)
- 実行: launchd から定期起動 (com.hisho.backup-status.plist)。手動実行も可。
"""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "core"))

from hisho_core import rag
from hisho_core.config import load_config
from hisho_core.store import Store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
logger = logging.getLogger("hisho.backup")

CONFIG_PATH = Path.home() / "Library" / "Application Support" / "Hisho" / "backup_targets.json"
STATUS_SOURCE_ID = 1  # status ドキュメントは常に 1 件だけ保つ


def _run(cmd: str, timeout: int = 40) -> str:
    """シェルコマンドを実行し、出力 (なければエラー) を返す。落ちても例外は投げない。"""
    try:
        p = subprocess.run(["/bin/bash", "-lc", cmd], capture_output=True,
                           text=True, timeout=timeout)
        out = (p.stdout or "").strip() or (p.stderr or "").strip()
        return out or "(出力なし)"
    except subprocess.TimeoutExpired:
        return "(タイムアウト — 接続先が応答しない)"
    except Exception as e:  # noqa: BLE001 — コレクタは何があっても続行する
        return f"(実行失敗: {e})"


def _collect() -> str:
    devices = json.loads(CONFIG_PATH.read_text())["devices"]
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"現在のバックアップ状況・最新の実測データ ({now} 自動収集。各機器の今の状態はこれが一次情報):", ""]
    for d in devices:
        logger.info("collecting: %s", d["name"])
        result = _run(d["cmd"])
        indented = "\n".join("  " + ln for ln in result.splitlines()[:12])
        lines.append(f"■ {d['name']}\n{indented}")
    lines.append("")
    lines.append("データの限界 (回答時はこの通りに伝えること):")
    lines.append("・「アイドル」は宛先設定と待機状態の確認のみ。バックアップが正常完了した証明ではない。")
    lines.append("・最終バックアップ完了日時の真実は mini 側の sparsebundle にあり、mini 接続不可の間は未確認。")
    lines.append("・完了を断定せず「宛先は設定済み・最終完了日時は未確認」と答えるのが正確。")
    lines.append("(この情報は定期収集の時点値。今すぐの状態が必要なら再収集が要る)")
    return "\n".join(lines)


def _upsert_status(store: Store, doc: str, blob: bytes, model: str) -> None:
    """旧 status を vec0 含めて消してから新しい 1 件を入れる。"""
    old_ids = [r[0] for r in store.conn.execute(
        "SELECT id FROM chunks WHERE source_type='status'").fetchall()]
    for cid in old_ids:
        store.conn.execute("DELETE FROM vec_chunks_bge_m3 WHERE rowid=?", (cid,))
    store.conn.execute("DELETE FROM chunks WHERE source_type='status'")
    store.conn.commit()
    store.add_chunk("status", STATUS_SOURCE_ID, None, doc, blob, model, store.vec_dim)


async def main() -> int:
    if not CONFIG_PATH.exists():
        logger.error("設定がない: %s", CONFIG_PATH)
        return 1
    doc = _collect()
    logger.info("collected:\n%s", doc)

    config = load_config()
    store = Store(config.db_path)
    if not store.rag_enabled:
        logger.error("RAG 無効 — 保存できない")
        return 1
    blobs = await rag.embed([doc], model=config.embed_model, ollama_host=config.ollama_host)
    if blobs is None:
        logger.error("embedding 失敗 — ollama 稼働を確認")
        return 1
    _upsert_status(store, doc, blobs[0], config.embed_model)
    logger.info("記憶に上書き保存した")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

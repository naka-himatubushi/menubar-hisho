"""役割: JARVIS の「実測の目」。読み取り専用のセンサー機能。

台帳ファイル (backup_targets.json / sensor_targets.json、共に
~/Library/Application Support/Hisho/ 配下) に人間が書いた固定コマンドを
並列実行し、「HH:MM 実測」ヘッダつきの平文レポートに整形する。

安全前提 (このモジュールを変更する時に必ず守ること):
- 台帳の cmd は人間が手で編集する固定リストであり、LLM/HTTP 入力由来の文字列を
  一切混ぜない。だからこそ subprocess を shell=True で実行して良い。
- topic は "backup" / "machines" / "storage" / "health" / "all" の enum だけを受け付ける。
  この文字列自体をコマンド組み立てに使うことは絶対にしない (辞書のキー参照のみ)。
- 全て読み取り専用。書き込み・起動系のコマンドはここに登録しない。
- 時間の上限は二層: コマンド 1 本 8 秒 (COMMAND_TIMEOUT) と topic 全体 12 秒
  (TOPIC_DEADLINE)。どちらを超えても例外ではなく「実測失敗」の行になる。
- 台帳は人間管理だが編集ミスはあり得るので、形式不正のエントリは実行せず
  「形式が不正」の行として報告する (第二層の防御)。
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import signal
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("hisho")

COMMAND_TIMEOUT = 8    # 秒。台帳コマンド 1 本あたりの上限
TOPIC_DEADLINE = 12    # 秒。topic 全体 (並列実行の待ち合わせ) の上限
TOPICS = ("backup", "machines", "storage", "health", "all")

BACKUP_LEDGER = "backup_targets.json"   # {"devices": [{"name":..., "cmd":...}, ...]}
SENSOR_LEDGER = "sensor_targets.json"   # {"topics": {"machines": [...], "storage": [...]}}


def _kill_group(proc) -> None:
    """timeout したプロセスをグループごと止める。shell=True の複合コマンド
    (ssh やパイプ) は普通の kill だと孫プロセスに届かないため、
    start_new_session で分離したプロセスグループへ killpg する。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass  # 既に終了している等 — 止める相手がいなければそれで良い


def _run_one(item: dict) -> dict:
    """台帳エントリ {"name","cmd"} を 1 本実行する。timeout/例外は例外を投げず
    output を「実測失敗: 理由」に丸めて返す (呼び出し側は常に安全)。"""
    name = item.get("name", "?") if isinstance(item, dict) else "?"
    try:
        cmd = item["cmd"]
        # start_new_session=True でプロセスグループを分離 → timeout 時に孫ごと殺せる
        proc = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        try:
            stdout, stderr = proc.communicate(timeout=COMMAND_TIMEOUT)
        except subprocess.TimeoutExpired:
            _kill_group(proc)
            proc.communicate()  # kill 後の回収 (ゾンビ化防止)
            return {"name": name, "output": f"実測失敗: タイムアウト ({COMMAND_TIMEOUT}秒)"}
        out = (stdout or "").strip() or (stderr or "").strip()
        return {"name": name, "output": out or "(出力なし)"}
    except Exception as e:  # noqa: BLE001 — センサーは何があっても他の項目を止めない
        logger.warning("sensor command failed: %s", name, exc_info=True)
        return {"name": name, "output": f"実測失敗: {e}"}


def run_all(items: list[dict]) -> list[dict]:
    """[{"name","cmd"}, ...] を並列実行する汎用実行係。投入順を保った
    [{"name","output"}, ...] を返す。1 項目が落ちても他は継続する。
    全体でも TOPIC_DEADLINE 秒を超えて待たない (超過分は「全体タイムアウト」の行になる)。"""
    if not items:
        return []
    deadline = time.monotonic() + TOPIC_DEADLINE
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=len(items))
    futures = [ex.submit(_run_one, it) for it in items]
    results: list[dict] = []
    try:
        for it, fut in zip(items, futures):
            name = it.get("name", "?") if isinstance(it, dict) else "?"
            remaining = deadline - time.monotonic()
            try:
                results.append(fut.result(timeout=max(0.0, remaining)))
            except concurrent.futures.TimeoutError:
                fut.cancel()
                results.append({
                    "name": name,
                    "output": f"実測失敗: 全体タイムアウト ({TOPIC_DEADLINE}秒)"})
    finally:
        # 走り残りのスレッドを待たずに返す (各コマンドは COMMAND_TIMEOUT で自滅する)
        ex.shutdown(wait=False, cancel_futures=True)
    return results


def _load_json(path: Path) -> object | None:
    """台帳 JSON を読む。無い/壊れている場合は None (例外を投げない)。"""
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except Exception:
        logger.warning("台帳の読み込みに失敗: %s", path, exc_info=True)
        return None


def _valid_items(raw: object, missing: list[str]) -> list[dict]:
    """台帳から読んだエントリ列を検証し、cmd が文字列の dict だけ返す。
    形式不正のエントリは実行せず missing に説明を積む。台帳は人間管理だが、
    編集ミスでコマンド実行系が壊れたり型エラーで落ちたりしないための第二層。"""
    if not isinstance(raw, list):
        missing.append(f"台帳エントリの形式が不正: {repr(raw)[:50]}")
        return []
    good: list[dict] = []
    for it in raw:
        if isinstance(it, dict) and isinstance(it.get("cmd"), str):
            good.append(it)
        else:
            missing.append(f"台帳エントリの形式が不正: {repr(it)[:50]}")
    return good


def ledger_items(topic: str, app_support_dir: Path | str) -> tuple[list[dict], list[str]]:
    """topic に対応する台帳からコマンド一覧を集める。
    戻り値 = (実行対象の items, 欠落・形式不正の説明メッセージ一覧)。
    topic が enum 外なら ValueError (LLM 由来の文字列を弾く境界)。"""
    if topic not in TOPICS:
        raise ValueError(f"unknown topic: {topic!r}")
    app_support_dir = Path(app_support_dir)
    items: list[dict] = []
    missing: list[str] = []

    if topic in ("backup", "all"):
        data = _load_json(app_support_dir / BACKUP_LEDGER)
        if data is None:
            missing.append(f"{BACKUP_LEDGER} が見つかりません (台帳未設置)")
        elif not isinstance(data, dict):
            missing.append(f"台帳エントリの形式が不正: {repr(data)[:50]}")
        else:
            items.extend(_valid_items(data.get("devices", []), missing))

    if topic in ("machines", "storage", "health", "all"):
        data = _load_json(app_support_dir / SENSOR_LEDGER)
        if data is None:
            missing.append(f"{SENSOR_LEDGER} が見つかりません (台帳未設置)")
        elif not isinstance(data, dict) or not isinstance(data.get("topics", {}), dict):
            missing.append(f"台帳エントリの形式が不正: {repr(data)[:50]}")
        else:
            names = ("machines", "storage", "health") if topic == "all" else (topic,)
            topics_data = data.get("topics", {})
            for name in names:
                items.extend(_valid_items(topics_data.get(name, []), missing))

    return items, missing


def now_header(now: Callable[[], datetime] = datetime.now) -> str:
    """「HH:MM 実測」ヘッダを作る。now はテスト用の時刻注入口。"""
    return f"{now().strftime('%H:%M')} 実測"


def format_report(results: list[dict], missing: list[str]) -> str:
    """run_all() の結果 + 欠落メッセージを平文レポート本文に整形する
    (ヘッダは含まない。純粋関数)。項目は【name】+結果、記号装飾は使わない。"""
    parts = [f"【{r['name']}】\n{r['output']}" for r in results]
    if missing:
        parts.append("\n".join(missing))
    return "\n\n".join(parts)


def all_failed(results: list[dict]) -> bool:
    """results が空でなく、全項目が実測失敗だったら True。
    (check_status がこれを見て最終既知値フォールバックを足すか判断する)"""
    return bool(results) and all(r["output"].startswith("実測失敗:") for r in results)


def measure(topic: str, app_support_dir: Path | str,
            *, now: Callable[[], datetime] = datetime.now) -> str:
    """topic を検証し、台帳から読んだコマンドを並列実測して平文レポートを返す。

    topic は "backup"|"machines"|"storage"|"all" のみ (それ以外は ValueError)。
    台帳ファイルが無い場合も例外は投げず、分かるメッセージ入りのレポートを返す。
    """
    items, missing = ledger_items(topic, app_support_dir)
    header = now_header(now)
    if not items:
        body = "\n".join(missing) if missing else "台帳にコマンドが登録されていません"
        return f"{header}\n\n{body}"
    results = run_all(items)
    return f"{header}\n\n{format_report(results, missing)}"

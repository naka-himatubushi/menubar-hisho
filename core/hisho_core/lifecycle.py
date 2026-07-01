"""子プロセスの生存管理: core.json 照合・ポート選択・親死(stdin EOF)での自死。"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
from pathlib import Path

logger = logging.getLogger("hisho")


def core_json_path(config_db_path: str) -> str:
    """db と同じ Hisho ディレクトリの core.json パスを返す。"""
    return str(Path(config_db_path).expanduser().parent / "core.json")


def write_core_json(path: str, pid: int, port: int) -> None:
    """core.json に PID とポート番号を書き込む。"""
    Path(path).write_text(json.dumps({"pid": pid, "port": port}))


def read_core_json(path: str) -> dict | None:
    """core.json を読み込む。ファイルなし・JSON不正・非dictなら None を返す。"""
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else None
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def _pid_alive(pid: int) -> bool:
    """プロセスが生存しているか確認。"""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 別ユーザだが生存
    except OSError:
        return False


def is_our_stale_core(info: dict) -> bool:
    """PID 生存かつ /healthz が core:true を返すか確認。ネットワーク不能なら False。"""
    import urllib.request

    if not info or not _pid_alive(info.get("pid", -1)):
        return False
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{info['port']}/healthz", timeout=1.0
        ) as r:
            return json.loads(r.read()).get("core") is True
    except Exception:
        logger.debug("stale-core probe failed", exc_info=True)
        return False


def bind_port(preferred: int) -> tuple[socket.socket, int]:
    """127.0.0.1:preferred を試し、OSError なら :0 に fallback。listen 済ソケットと実ポートを返す。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", preferred))
    except OSError:
        s.bind(("127.0.0.1", 0))  # OS 割当に fallback
    s.listen()
    return s, s.getsockname()[1]


def start_stdin_death_watcher(on_death=os._exit, stream=None) -> None:
    """スレッド起動し stdin EOF で on_death(0) を呼ぶ。テスト用に stream を注入可能。"""
    st = stream if stream is not None else sys.stdin.buffer

    def _watch():
        try:
            while st.read(1):  # 親が生きてる限りブロック
                pass
        except Exception:
            pass
        on_death(0)  # EOF = 親死

    threading.Thread(target=_watch, daemon=True).start()

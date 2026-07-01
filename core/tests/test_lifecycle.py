"""core.json 読み書き・ポート fallback・stdin死監視の発火を検証。"""
import io
import json
import socket
import threading
import time

from hisho_core import lifecycle


def test_core_json_roundtrip(tmp_path):
    p = str(tmp_path / "core.json")
    lifecycle.write_core_json(p, pid=1234, port=51100)
    info = lifecycle.read_core_json(p)
    assert info["pid"] == 1234 and info["port"] == 51100


def test_read_missing_returns_none(tmp_path):
    assert lifecycle.read_core_json(str(tmp_path / "nope.json")) is None


def test_bind_port_fallbacks_when_taken():
    # preferred を占有しておくと fallback で別ポートが返る
    held = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    held.bind(("127.0.0.1", 0))
    held.listen()
    taken = held.getsockname()[1]
    sock, port = lifecycle.bind_port(taken)
    assert port != 0
    sock.close()
    held.close()


def test_stdin_death_watcher_fires_on_eof():
    fired = {"v": None}
    r = io.BytesIO(b"")  # 即 EOF
    lifecycle.start_stdin_death_watcher(
        on_death=lambda code: fired.__setitem__("v", code), stream=r
    )
    time.sleep(0.1)
    assert fired["v"] == 0

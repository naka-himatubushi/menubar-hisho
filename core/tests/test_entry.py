"""起動配線: build_server_and_port が app と有効ポートを返し、DB dir を作ることを検証。"""
from hisho_core.config import load_config
from hisho_core import __main__ as entry


def test_build_server_and_port(tmp_path):
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "d" / "t.db"), "HISHO_PORT": "0"})
    app, sock, port = entry.build_server_and_port(cfg)
    try:
        assert port > 0
        assert (tmp_path / "d").is_dir()
        assert any(r.path == "/healthz" for r in app.routes)
    finally:
        sock.close()

"""`python -m hisho_core` エントリ: 設定→DB→ポート確定→stdin死監視→uvicorn 起動。"""
from __future__ import annotations
import os
import uvicorn
from .config import load_config, ensure_db_dir, ensure_briefing_targets
from .store import Store
from .server import create_app
from . import lifecycle


def build_server_and_port(config):
    """設定からサーバーソケット・ポート・アプリを構築。

    - DB ディレクトリを作成
    - Store を開く
    - core.json を読み込み(stale なら無視して新規 bind)
    - ポートをバインド(fallback 対応)
    - FastAPI アプリを作成

    Returns:
        (app, sock, port): FastAPI アプリ・リスニングソケット・確定ポート番号
    """
    ensure_db_dir(config.db_path)
    store = Store(config.db_path)
    cj = lifecycle.core_json_path(config.db_path)
    existing = lifecycle.read_core_json(cj)
    if existing and lifecycle.is_our_stale_core(existing):
        # 既存の我々の core が生きている: 本 MVP では新規 bind を優先(親が監督)
        pass
    sock, port = lifecycle.bind_port(config.port)
    app = create_app(store, config)
    return app, sock, port


def main():
    """エントリポイント: config 読込 → サーバー構築 → stdin 死監視開始 → core.json 書込 → uvicorn 起動。"""
    config = load_config()
    app, sock, port = build_server_and_port(config)
    # briefing_targets.json の初回自動生成。build_server_and_port はテストからも直接
    # 呼ばれる(test_build_server_and_port)ため、実ファイルシステムに触るこの呼び出しは
    # main() 側に置く(テストが実ユーザーの Application Support を汚さないようにする)。
    ensure_briefing_targets(config.briefing_targets_path)
    lifecycle.start_stdin_death_watcher()  # 親(Swift)死で自死
    lifecycle.write_core_json(lifecycle.core_json_path(config.db_path), os.getpid(), port)
    uvicorn.run(app, fd=sock.fileno(), workers=1, log_level="info")


if __name__ == "__main__":
    main()

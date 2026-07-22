"""役割: library topic (書庫検索 = Library-DB の jarvis find) の全層テスト。
- extract_library_query: 検索語の regex 決定的抽出 (LLM を経ない)
- topic ルーティング: _guess_topic の library 判定と複数群一致時の all 落ち
- sensors.library_search: subprocess 呼び出し形状 (shell=False・検索語 1 要素・cwd・timeout 10s)
- tools.check_status: library 分岐 (config の書庫 dir 使用・空検索語・all 非包含)
- server 統合: 事前実測→SENSOR_NOTE 注入・空検索語のサーバ定型聞き返し・RAG 両方向スキップ
実 subprocess / 実 ollama は一切起動しない (すべて fake / monkeypatch)。"""
import asyncio
import subprocess

import anyio
import httpx
from fastapi.testclient import TestClient

from hisho_core import sensors
from hisho_core import server as server_mod
from hisho_core import tools
from hisho_core.config import load_config
from hisho_core.context import SENSOR_NOTE
from hisho_core.server import create_app, extract_library_query, _guess_topic
from hisho_core.store import Store


# --- extract_library_query (検索語の決定的抽出) ---

def test_extract_query_strips_noun_and_particle_before_doko():
    assert extract_library_query("Buffaloのメモどこ") == "Buffalo"


def test_extract_query_strips_sagashite():
    assert extract_library_query("議事録探して") == "議事録"


def test_extract_query_strips_full_ask_phrase_and_punctuation():
    assert extract_library_query("Buffaloのファイルはどこ？") == "Buffalo"


def test_extract_query_strips_shoko_prefix_and_kensaku():
    assert extract_library_query("書庫でネットワーク監査を検索して") == "ネットワーク監査"


def test_extract_query_keeps_inner_genitive_no():
    # 名詞句の中の「の」(会議の議事録) は残す。剥がすのは定型句側の助詞だけ
    assert extract_library_query("会議の議事録どこだっけ") == "会議の議事録"


def test_extract_query_empty_when_only_ask_words():
    assert extract_library_query("どこ") == ""
    assert extract_library_query("探して") == ""
    assert extract_library_query("書庫を検索して") == ""
    assert extract_library_query("メモを探して") == ""   # 名詞クラス語だけでは検索語にならない
    assert extract_library_query("") == ""


def test_extract_query_particle_residue_is_treated_as_empty():
    # 「どこかにあるか探して」→ 削除後に助詞「に」だけ残る残骸は空扱い (聞き返しへ)
    assert extract_library_query("どこかにあるか探して") == ""


# --- topic ルーティング (_guess_topic) ---

def test_guess_topic_library_single_group():
    assert _guess_topic("Buffaloのメモどこ") == "library"
    assert _guess_topic("議事録探して") == "library"
    assert _guess_topic("書庫にUPSの納品書ある?") == "library"


def test_guess_topic_library_plus_other_group_falls_to_all():
    # backup + library の 2 群一致 → all (all は書庫検索を含まない = 測りすぎ側で安全)
    assert _guess_topic("バックアップのメモどこ") == "all"


def test_topics_enum_contains_library():
    assert "library" in sensors.TOPICS


def test_ledger_items_library_returns_empty_without_raising(tmp_path):
    # library は台帳を持たない (check_status が library_search へ直接ルーティング)
    items, missing = sensors.ledger_items("library", tmp_path)
    assert items == []
    assert missing == []


# --- sensors.library_search (subprocess は FakePopen に差し替え) ---

class FakePopen:
    """subprocess.Popen の差し替え。timeout_first=True だと最初の communicate
    (timeout 指定あり) で TimeoutExpired を投げ、kill 後の回収呼び出しには応じる。"""
    def __init__(self, stdout="", stderr="", returncode=0, timeout_first=False):
        self.pid = 4242
        self.returncode = returncode
        self._stdout, self._stderr = stdout, stderr
        self._timeout_first = timeout_first

    def communicate(self, timeout=None):
        if self._timeout_first and timeout is not None:
            raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return (self._stdout, self._stderr)


def test_library_search_passes_query_as_single_argv_element(tmp_path, monkeypatch):
    """安全前提の要: 検索語は shell を経由せず argv の 1 要素のまま渡る
    (空白や ; を含んでもコマンドとして解釈される経路が無い)。"""
    seen = {}

    def spy_popen(argv, **kw):
        seen["argv"] = list(argv)
        seen.update(kw)
        return FakePopen(stdout="1件: Buffalo設定メモ.md\n")

    monkeypatch.setattr(sensors.subprocess, "Popen", spy_popen)
    out = sensors.library_search("Buffalo; rm -rf ~", tmp_path)
    assert seen["argv"] == ["uv", "run", "jarvis", "find", "Buffalo; rm -rf ~"]
    assert seen.get("shell") is False               # shell 非経由
    assert seen.get("start_new_session") is True    # timeout 時に孫ごと殺せる
    assert seen["cwd"] == str(tmp_path)             # 書庫 dir を cwd に実行
    assert out == {"name": "書庫検索: Buffalo; rm -rf ~", "output": "1件: Buffalo設定メモ.md"}


def test_library_search_zero_hits_passthrough(tmp_path, monkeypatch):
    """0 件時は jarvis find 自身の出力をそのまま返す (勝手に言い換えない)。"""
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(stdout="検索結果なし: Buffalo\n"))
    out = sensors.library_search("Buffalo", tmp_path)
    assert out["output"] == "検索結果なし: Buffalo"


def test_library_search_timeout_kills_group_and_reports(tmp_path, monkeypatch):
    assert sensors.LIBRARY_TIMEOUT == 10   # 予算固定 (発注仕様)
    killed = []
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(timeout_first=True))
    monkeypatch.setattr(sensors.os, "getpgid", lambda pid: pid)
    monkeypatch.setattr(sensors.os, "killpg", lambda pgid, sig: killed.append(pgid))
    out = sensors.library_search("Buffalo", tmp_path)
    assert out["output"] == f"実測失敗: タイムアウト ({sensors.LIBRARY_TIMEOUT}秒)"
    assert killed == [4242]   # プロセスグループごと kill


def test_library_search_nonzero_exit_is_failure_not_zero_hits(tmp_path, monkeypatch):
    """検索コマンド自体の失敗 (traceback 等) を「0件」と混同させない。"""
    monkeypatch.setattr(sensors.subprocess, "Popen",
                        lambda *a, **kw: FakePopen(stderr="Traceback: boom", returncode=1))
    out = sensors.library_search("Buffalo", tmp_path)
    assert out["output"].startswith("実測失敗 (exit 1):")
    assert "Traceback: boom" in out["output"]


def test_library_search_uv_missing_is_captured_not_raised(tmp_path, monkeypatch):
    def raise_err(*a, **kw):
        raise FileNotFoundError("uv")

    monkeypatch.setattr(sensors.subprocess, "Popen", raise_err)
    out = sensors.library_search("Buffalo", tmp_path)
    assert out["output"].startswith("実測失敗:")


# --- tools.check_status の library 分岐 ---

class FakeStoreUnused:
    """library 分岐は store を使わない (最終既知値フォールバック無し) ことの見張り。"""
    def latest_status_chunk(self):
        raise AssertionError("library topic で status チャンクを参照してはいけない")


def _cfg(tmp_path, **extra_env):
    env = {"HISHO_DB": str(tmp_path / "secretary.db")}
    env.update(extra_env)
    return load_config(env=env)


def test_check_status_library_uses_config_dir_and_formats_report(tmp_path, monkeypatch):
    called = {}

    def fake_search(query, library_dir):
        called["query"], called["dir"] = query, library_dir
        return {"name": f"書庫検索: {query}", "output": "2件ヒット:\n- buffalo_memo.md"}

    monkeypatch.setattr(sensors, "library_search", fake_search)
    cfg = _cfg(tmp_path, HISHO_LIBRARY_DIR="/x/library-db")
    out = asyncio.run(tools.check_status(
        {"topic": "library", "query": "Buffalo"},
        store=FakeStoreUnused(), config=cfg, write_lock=None))
    assert out["topic"] == "library"                  # all に潰されない
    assert called == {"query": "Buffalo", "dir": "/x/library-db"}
    assert "実測" in out["report"]                    # 「HH:MM 実測」ヘッダ (health と同形式)
    assert "【書庫検索: Buffalo】" in out["report"]
    assert "2件ヒット" in out["report"]


def test_check_status_library_default_dir_points_to_library_db(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(sensors, "library_search",
                        lambda q, d: seen.update(dir=d) or {"name": "書庫検索: x", "output": "ok"})
    asyncio.run(tools.check_status({"topic": "library", "query": "x"},
                                   store=FakeStoreUnused(), config=_cfg(tmp_path), write_lock=None))
    assert seen["dir"].endswith("sandbox/library-db")   # config 既定値


def test_check_status_library_empty_query_does_not_run_search(tmp_path, monkeypatch):
    """server 側で定型応答に落ちるため通常来ないが、来ても検索を実行しない (第二層)。"""
    ran = []
    monkeypatch.setattr(sensors, "library_search", lambda q, d: ran.append(q))
    out = asyncio.run(tools.check_status({"topic": "library", "query": "  "},
                                         store=FakeStoreUnused(), config=_cfg(tmp_path), write_lock=None))
    assert ran == []
    assert "検索語が空" in out["report"]


def test_check_status_all_topic_does_not_run_library_search(tmp_path, monkeypatch):
    """all は書庫検索を含まない (検索語が無いのに jarvis find が走る経路の否定)。"""
    ran = []
    monkeypatch.setattr(sensors, "library_search", lambda q, d: ran.append(q))
    monkeypatch.setattr(sensors, "run_all", lambda items: [])

    class FakeStoreWithStatus:
        def latest_status_chunk(self):
            return None

    out = asyncio.run(tools.check_status({"topic": "all"},
                                         store=FakeStoreWithStatus(), config=_cfg(tmp_path),
                                         write_lock=None))
    assert ran == []
    assert out["topic"] == "all"


# --- server 統合 (事前実測 → 注入 / 空検索語の定型 / RAG スキップ) ---

def _sse_text(resp_bytes):
    return resp_bytes.decode("utf-8", "replace")


def _make_chat_fn(script):
    """script: 呼び出し回数ごとに返すイベント列のリスト。tools と messages の受領を記録。"""
    calls = {"tools_seen": [], "messages_seen": [], "n": 0}

    async def chat_fn(messages, *, model, ollama_host, num_ctx, keep_alive, think=False, tools=None):
        calls["tools_seen"].append(tools)
        calls["messages_seen"].append(list(messages))
        seq = script[min(calls["n"], len(script) - 1)]
        calls["n"] += 1
        for e in seq:
            yield e

    return chat_fn, calls


async def _ok_probe():
    return {"reachable": True, "version": "0", "model_present": True, "model_loaded": True, "models": ["m"]}


async def _true():
    return True


def _plain_answer_script(text="見つかりました"):
    return [[{"type": "delta", "content": text}, {"type": "done", "finish_reason": "stop"}]]


def _fake_check_status(called):
    """args を記録して固定レポートを返す fake check_status を作る。"""
    async def fake(args, **kw):
        called["args"] = dict(args)
        called["n"] = called.get("n", 0) + 1
        return {"topic": args.get("topic"),
                "report": "12:00 実測\n\n【書庫検索: Buffalo】\n1件: buffalo_memo.md"}
    return fake


def test_library_question_extracts_query_and_injects_report(tmp_path):
    """「Buffaloのメモどこ」→ サーバが決定的に検索語を抽出して check_status(library) を
    呼び、レポートが SENSOR_NOTE 前置の system メッセージとして注入される。
    モデルにツールは渡さない (要約する係のみ)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "lib1", "messages": [{"role": "user", "content": "Buffaloのメモどこ"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
    assert called["args"] == {"topic": "library", "query": "Buffalo"}
    assert calls["tools_seen"][0] is None            # ツールは一切渡さない
    sys_msgs = [m for m in calls["messages_seen"][0] if m["role"] == "system"]
    injected = [m for m in sys_msgs if m["content"].startswith(SENSOR_NOTE)]
    assert len(injected) == 1
    assert "12:00 実測" in injected[0]["content"]
    assert "buffalo_memo.md" in injected[0]["content"]


def test_library_empty_query_returns_canned_ask_without_llm(tmp_path):
    """検索語が抽出できない発話 (「探して」だけ等) は check_status もモデルも呼ばず、
    サーバ定型「何を探すか一言で教えてください」で完結する。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "lib2", "messages": [{"role": "user", "content": "探して"}]},
                   headers={"X-Hisho-Source": "popover"})
        assert r.status_code == 200
        body = _sse_text(r.content)
    assert "何を探すか一言で教えてください" in body
    assert called.get("n", 0) == 0                   # 検索は実行しない
    assert calls["n"] == 0                           # モデルも呼ばない


def test_external_source_does_not_trigger_library(tmp_path):
    """実行系の経路は popover 限定 (external から書庫語で来ても検索しない)。"""
    store = Store(str(tmp_path / "t.db"))
    chat_fn, calls = _make_chat_fn(_plain_answer_script("ok"))
    app = create_app(store, load_config(), chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}
    with TestClient(app) as c:
        r = c.post("/v1/chat/completions",
                   json={"session_id": "lib3", "messages": [{"role": "user", "content": "Buffaloのメモどこ"}]},
                   headers={"X-Hisho-Source": "external"})
        assert r.status_code == 200
    assert called.get("n", 0) == 0


async def test_library_turn_skips_memories_and_indexing(tmp_path, monkeypatch):
    """書庫検索ターンは他の sensor 同様 RAG を両方向で遮断する:
    注入なし (古いメモが実測と矛盾して語りを汚染しない)・索引なし (揮発性の
    検索結果が「既知の事実」として将来の会話に注入されない)。"""
    cfg = load_config(env={"HISHO_DB": str(tmp_path / "t.db")})
    store = Store(cfg.db_path, vec_dim=4)
    chat_fn, calls = _make_chat_fn(_plain_answer_script())
    app = create_app(store, cfg, chat_fn=chat_fn,
                     probe_fn=lambda: _ok_probe(), warmup_fn=_true, unload_fn=_true)
    called = {}
    app.state.tool_registry = {"check_status": _fake_check_status(called)}

    retrieved, indexed = [], []

    async def spy_retrieve(store_, msg, *, config, exclude_session_id, client_factory=None):
        retrieved.append(msg)
        return ["古い書庫メモ"]

    async def spy_index(store_, lock, turn_id, session_id, content, *, config, client_factory=None):
        indexed.append(content)
        return True

    monkeypatch.setattr(server_mod.rag, "retrieve", spy_retrieve)
    monkeypatch.setattr(server_mod.rag, "index_turn", spy_index)

    async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        # 検索応答ターンと聞き返しターンの両方を確認
        r1 = await c.post("/v1/chat/completions", headers={"X-Hisho-Source": "popover"},
                          json={"session_id": "libR", "messages": [{"role": "user", "content": "Buffaloのメモどこ"}]})
        assert r1.status_code == 200
        r2 = await c.post("/v1/chat/completions", headers={"X-Hisho-Source": "popover"},
                          json={"session_id": "libR", "messages": [{"role": "user", "content": "探して"}]})
        assert r2.status_code == 200
    await anyio.sleep(0.05)  # fire-and-forget の索引タスクがもしあれば走りきるのを待つ
    assert retrieved == []   # memories 注入なし
    assert indexed == []     # 索引なし

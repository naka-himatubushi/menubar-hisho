"""FastAPI アプリ組立。/healthz, /v1/models, /v1/chat/completions を提供。"""
from __future__ import annotations
import asyncio
import json
import logging
import re
import time
import uuid
import anyio
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from pathlib import Path
from . import actions as actions_module
from . import context
from . import llm
from . import rag
from . import tools as tools_module
from .config import Config
from .store import Store
from .sse import sse, chunk, DONE, error_frame

logger = logging.getLogger("hisho")

MAX_TOOL_ITERS = 3


def _now_ms() -> int:
    return int(time.time() * 1000)


_FORGET_TAIL = re.compile(r"(のこと|について|の情報|に関する|を|は|が|も|、|。|\s)+$")


def _forget_query(user_message: str) -> str:
    """決定的フォールバック用に user 発話から忘却対象語を抽出する。
    例: 「ハムスターのこと忘れて」→「ハムスター」。抽出できなければ全文を返す。"""
    head = re.split(r"忘れ|消し|消去|削除|覚えなくて", user_message)[0]
    return _FORGET_TAIL.sub("", head).strip() or user_message


# topic 推定用の語群。ちょうど 1 群に一致した時だけその topic、
# 0 群 or 複数群一致は曖昧とみなし all で全部測る (読み取り専用なので過剰測定が安全側)。
# 注: library と briefing は all に含まれない (library は検索語が要る、briefing は
# all を包含する上位 topic のため) ため、複数群一致で all に倒れた場合は書庫検索されず
# 期限セクションも付かない — それも「測りすぎ側」で安全。
_TOPIC_PATTERNS = (
    ("backup", re.compile(r"バックアップ")),
    ("storage", re.compile(r"温度|容量|空き|ディスク")),
    ("machines", re.compile(r"稼働|生きて|落ちて|マシン|動い")),
    ("health", re.compile(r"警報|異常|アラート|レポート|健康")),
    ("library", re.compile(r"書庫|どこ|探して|検索")),
    ("briefing", re.compile(r"おはよう|朝の報告|ブリーフィング|今日の状況")),
)


def _guess_topic(user_message: str) -> str:
    """決定的事前実測用に user 発話から check_status の topic を推定する。"""
    matched = [t for t, rx in _TOPIC_PATTERNS if rx.search(user_message)]
    return matched[0] if len(matched) == 1 else "all"


# 書庫検索の検索語抽出用: 定型の尋ね句・場所の前置き・末尾の残骸を「削除リスト」として
# 順に剥がす (LLM に抽出させない決定的方式。check_status をサーバ決定的に呼ぶのと同じ思想)。
_LIBRARY_STRIP = (
    # 場所の前置き: 「書庫で」「書庫の中から」「ライブラリに」
    re.compile(r"(?:書庫|ライブラリ)(?:の中)?(?:で|から|に|を)?"),
    # 尋ね句 (どこ系): 「のメモはどこ(にある|だっけ)?」ごと除去
    re.compile(r"(?:の|を)?(?:メモ|ファイル|資料|文書|書類|データ)?(?:って)?(?:は|が|を)?"
               r"(?:どこ|何処)(?:にある|にあった|にあります)?(?:か(?:な)?|だっけ|でしたっけ|ですか)?"),
    # 尋ね句 (探して系)
    re.compile(r"(?:の|を)?(?:メモ|ファイル|資料|文書|書類|データ)?(?:って)?(?:は|が|を)?"
               r"(?:探して|捜して|さがして)(?:きて|みて|くれ|ください|ほしい|もらえる|おいて)?"),
    # 尋ね句 (検索系)
    re.compile(r"(?:の|を)?(?:メモ|ファイル|資料|文書|書類|データ)?(?:って)?(?:は|が|を)?"
               r"検索(?:して|かけて)?(?:きて|みて|くれ|ください|おいて)?"),
    # 末尾の残骸: 「ある?」「あったっけ」
    re.compile(r"(?:ある|あった|あります)(?:か(?:な)?|っけ)?[\s?？!！。、]*$"),
)
# 削除後にこれ「だけ」残ったら検索語なしとみなす助詞 (「どこかにあるか探して」→「に」等の残骸対策)
_LIBRARY_PARTICLE_ONLY = frozenset("にでをはがのもへとか")


def extract_library_query(text: str) -> str:
    """書庫検索の検索語をユーザー発話から決定的に抽出する (LLM を経ない)。
    削除リスト regex で定型句を除去 → 前後の空白/記号を strip → 残りが検索語。
    例: 「Buffaloのメモどこ」→「Buffalo」。空文字を返したら呼び出し側が聞き返す。"""
    q = text or ""
    for rx in _LIBRARY_STRIP:
        q = rx.sub("", q)
    q = q.strip(" \t\r\n　、。・?？!！「」『』")
    if q in _LIBRARY_PARTICLE_ONLY:
        return ""  # 助詞 1 文字だけの残骸は検索語にしない
    return q


# M2 ゲート: 忘却ターンでモデルに渡す tool specs は forget_memories だけに絞る。
# TOOL_SPECS 全渡しだと他ツールの specs が混ざり、意図しないターンで破壊的ツールが
# 幻覚実行される経路が開く (adversarial レビュー #1 で再現済み)。
_FORGET_SPECS = [s for s in tools_module.TOOL_SPECS
                 if s["function"]["name"] == "forget_memories"]


async def _default_probe(config: Config) -> dict:
    """Ollama に /api/version, /api/tags, /api/ps を叩いて健康状態を取得。例外時は reachable=False。"""
    import httpx
    out = {
        "reachable": False,
        "version": None,
        "model_present": False,
        "model_loaded": False,
        "models": [],
    }
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            # /api/version
            v = await c.get(f"{config.ollama_host}/api/version")
            out["reachable"] = v.status_code == 200
            out["version"] = v.json().get("version") if v.status_code == 200 else None

            # /api/tags
            tags = await c.get(f"{config.ollama_host}/api/tags")
            names = (
                [m["name"] for m in tags.json().get("models", [])]
                if tags.status_code == 200
                else []
            )
            out["models"] = names
            out["model_present"] = any(
                n.split(":")[0] == config.chat_model.split(":")[0]
                or n == config.chat_model
                for n in names
            )

            # /api/ps
            ps = await c.get(f"{config.ollama_host}/api/ps")
            loaded = (
                [m["name"] for m in ps.json().get("models", [])]
                if ps.status_code == 200
                else []
            )
            out["model_loaded"] = config.chat_model in loaded
    except Exception:
        logger.debug("ollama probe failed", exc_info=True)
    return out


async def warmup_when_ready(probe, warmup, *, attempts=120, interval=2.0,
                            sleep=asyncio.sleep) -> bool:
    """ollama が reachable になり warm-up が成功するまで繰り返す。戻り値=成功したか。"""
    for _ in range(attempts):
        try:
            p = await probe()
            if p.get("reachable") and await warmup():
                return True
        except Exception:
            logger.debug("warmup attempt failed", exc_info=True)
        await sleep(interval)
    return False


def create_app(
    store: Store, config: Config, *, chat_fn=None, probe_fn=None,
    warmup_fn=None, unload_fn=None, action_executor=None,
) -> FastAPI:
    """FastAPI アプリを組立。

    Args:
        store: Store インスタンス
        config: Config インスタンス
        chat_fn: LLM チャット関数 (既定: llm.chat_stream)
        probe_fn: 健康状態プローブ関数 (既定: _default_probe(config))
        warmup_fn: warm-up 関数 (既定: llm.warmup with config 値)
        unload_fn: アンロード関数 (既定: llm.unload with config 値)
        action_executor: アクション実行関数 (argv) -> str (既定: actions.execute。
            work CLI が無いテスト/CI 環境向けの注入口)

    Returns:
        FastAPI アプリ
    """
    import contextlib
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(
            warmup_when_ready(app.state.probe_fn, app.state.warmup_fn))
        backfill_task = asyncio.create_task(
            rag.backfill(app.state.store, app.state.write_lock, config=config))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        backfill_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await backfill_task
        try:
            await asyncio.wait_for(app.state.unload_fn(), timeout=2.0)
        except Exception:
            logger.debug("unload on shutdown failed", exc_info=True)

    app = FastAPI(lifespan=_lifespan)
    app.state.store = store
    app.state.config = config
    app.state.chat_fn = chat_fn or llm.chat_stream
    app.state.probe_fn = probe_fn or (lambda: _default_probe(config))
    app.state._probe_cache = {"t": 0.0, "v": None}
    app.state.write_lock = asyncio.Lock()
    app.state.tool_registry = tools_module.REGISTRY
    # imperative 形に限定して否定 (「忘れないで」「消さないで」) を除外。
    # フォールバックが誤って削除しないための一次ゲート。
    app.state.forget_intent = re.compile(r"忘れて|消して|消去|削除|覚えなくて")
    # センサー系キーワードゲート。読み取り専用なので forget ほど厳密でなくてよいが、
    # forget ゲートが先勝ち (下の is_sensor 判定で is_forget を優先する)。
    # 3 行目は library (書庫検索) 語、4 行目は briefing (朝ブリーフィング) 語 —
    # _TOPIC_PATTERNS の語は必ずここにも足す
    # (ゲート ⊇ パターンの不変条件。test_sensor_gate_covers_all_topic_pattern_words が守る)。
    app.state.sensor_intent = re.compile(
        r"状態|状況|調子|バックアップ|温度|容量|空き|稼働|生きて|落ちて|ディスク|動い"
        r"|マシン|警報|異常|アラート|レポート|健康"
        r"|書庫|どこ|探して|検索"
        r"|おはよう|朝の報告|ブリーフィング|今日の状況")
    # アクション意図ゲート。優先順位: forget > 確認 (pending あり) > 提案 > sensor。
    # 提案の取り違えは確認フロー (「はい」以外で破棄) が無害化するので、緩くてよい。
    # ただし bare「して」は「状態を確認して」(sensor 意図) まで提案化するので、
    # 第1節は動作動詞に限定し、名詞直結の「バックアップして」だけ別枝で拾う。
    app.state.action_intent = re.compile(
        r"(バックアップ|TM).*(回|取っ|開始|走|実行)"
        r"|(バックアップ|TM)(を|も)?(して|しといて|しておいて|お願い)"
        r"|((スタジオ|studio|ミニ|mini).*(投げ|回し|任せ|やらせ|やって))",
        re.IGNORECASE)
    # 確認待ちの操作 (session 束縛・TTL 5分・一回限り)。再起動で消える = 安全側。
    app.state.pending_actions = actions_module.PendingActions()
    # アクション実行係。テスト/CI では fake を注入して実 subprocess を避ける。
    app.state.action_executor = action_executor or actions_module.execute
    app.state.warmup_fn = warmup_fn or (lambda: llm.warmup(
        model=config.chat_model, ollama_host=config.ollama_host,
        num_ctx=config.num_ctx, keep_alive=config.keep_alive))
    app.state.unload_fn = unload_fn or (lambda: llm.unload(
        model=config.chat_model, ollama_host=config.ollama_host))

    async def _probe() -> dict:
        """probe_fn の結果を ~3秒キャッシュ。"""
        cache = app.state._probe_cache
        if (
            cache["v"] is not None
            and (time.time() - cache["t"]) < 3.0
        ):
            return cache["v"]
        v = await app.state.probe_fn()
        cache["t"], cache["v"] = time.time(), v
        return v

    @app.get("/healthz")
    async def healthz():
        """層状の健康状態。core=常に true, ollama={reachable, version}, model={present, loaded}。"""
        p = await _probe()
        return {
            "core": True,
            "ollama": {"reachable": p["reachable"], "version": p["version"]},
            "model": {"present": p["model_present"], "loaded": p["model_loaded"],
                      "name": config.chat_model},
        }

    @app.get("/v1/models")
    async def models():
        """モデル一覧。probe の models または config.chat_model にフォールバック。"""
        p = await _probe()
        names = p["models"] or [config.chat_model]
        return {"object": "list", "data": [{"id": n, "object": "model"} for n in names]}

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        """SSE ストリーミングチャット。user ターンを記録→LLM 呼出→assistant ターン確定(finally 安全)。"""
        cfg = app.state.config
        store = app.state.store
        body = await request.json()
        source = request.headers.get("X-Hisho-Source", "external")
        model = body.get("model") or cfg.chat_model
        session_id = body.get("session_id") or f"sess-{uuid.uuid4().hex[:12]}"
        msgs_in = body.get("messages", [])
        user_message = (msgs_in[-1].get("content") or "") if msgs_in else ""
        if not isinstance(user_message, str):
            user_message = str(user_message)

        now = _now_ms()
        async with app.state.write_lock:
            await anyio.to_thread.run_sync(store.get_or_create_session, session_id, now)
            user_turn_id = await anyio.to_thread.run_sync(store.append_user_turn, session_id, user_message, now, source)

        is_forget = source == "popover" and bool(app.state.forget_intent.search(user_message))

        # アクション確認/破棄 (優先順位: forget > 確認 > 提案 > sensor)。
        # pending は pop で一回限り: 確認語で成立、それ以外の発話 (forget 含む) で破棄。
        pending_confirm = None
        pending_discarded = False
        if source == "popover":
            pa_prev = app.state.pending_actions.pop(session_id)
            if pa_prev is not None:
                if not is_forget and actions_module.is_confirmation(user_message):
                    pending_confirm = pa_prev
                else:
                    pending_discarded = True

        # 実行待ちが無いのに確認語だけが来た。モデルに任せると実行を演技する
        # (実LLMスモークで実測: 破棄直後の「はい」/新規会話初手の「はい」の両方) ため、
        # ①墓標がある session (破棄/期限切れ/実行済みの直後) と ②履歴ゼロの初手 (下の
        # popover 分岐で確定) はサーバ定型で止める。会話中の日常の「はい」は素通し。
        confirm_shaped_no_pending = (
            source == "popover" and not is_forget and pending_confirm is None
            and actions_module.is_confirmation(user_message))
        confirm_without_pending = (
            confirm_shaped_no_pending
            and app.state.pending_actions.recently_gone(session_id))

        is_action = (source == "popover" and not is_forget and pending_confirm is None
                     and not confirm_without_pending
                     and bool(app.state.action_intent.search(user_message)))
        # forget / アクションのゲートが先勝ち。
        is_sensor = (source == "popover" and not is_forget and pending_confirm is None
                     and not is_action
                     and bool(app.state.sensor_intent.search(user_message)))
        app_support_dir = Path(cfg.db_path).expanduser().parent

        # 確認成立 → サーバが決定的に実行 (安全不変条件: 実行経路はここだけ。
        # argv は提案時に組み立て済みのリスト直渡しで、モデルは実行に一切関与しない)。
        action_report = None
        if pending_confirm is not None:
            try:
                out = await anyio.to_thread.run_sync(
                    app.state.action_executor, pending_confirm.argv)
            except Exception:
                logger.warning("action execution failed", exc_info=True)
                out = "実行失敗: 内部エラー"
            action_report = actions_module.execution_report(pending_confirm, out)

        # 決定的事前実測 (安全性の要 #4): センサー系の質問はモデルに任せず、
        # 応答生成の前にサーバが実測して結果を文脈注入する (モデルは要約のみ)。
        # 実LLMスモーク (gemma4:12b) で tool-calling 方式は「モデルの語りが実測より
        # 先に生成され、古い記憶で汚染された前説を語る」欠陥を確認したため一方通行にした。
        # library topic は検索語もサーバが regex で決定的に抽出する (LLM に抽出させない)。
        sensor_report = None
        library_ask = False   # 書庫検索の意図だが検索語が空 → LLM を経ない定型で聞き返す
        if is_sensor:
            sensor_args = {"topic": _guess_topic(user_message)}
            if sensor_args["topic"] == "library":
                lib_query = extract_library_query(user_message)
                if lib_query:
                    sensor_args["query"] = lib_query
                else:
                    library_ask = True   # 実測 (検索) はしない。gen() の定型応答で完結
            if not library_ask:
                try:
                    res = await app.state.tool_registry["check_status"](
                        sensor_args,
                        store=store, config=cfg, write_lock=app.state.write_lock)
                    sensor_report = (res or {}).get("report") or "実測に失敗しました (内部エラー)"
                except Exception:
                    logger.warning("sensor pre-measurement failed", exc_info=True)
                    sensor_report = "実測に失敗しました (内部エラー)"

        if source == "popover":
            recent = await anyio.to_thread.run_sync(store.recent_turns, session_id, cfg.history_replay_turns)
            recent_wo_last = recent[:-1] if recent and recent[-1]["role"] == "user" else recent
            if confirm_shaped_no_pending and not recent_wo_last:
                # 履歴ゼロの初手「はい」は会話として成立しない。実LLMスモークで
                # RAG 記憶を根拠に実行を演技した (新規会話直後の誤爆経路) ため定型で止める。
                confirm_without_pending = True
            # 忘却/センサー/アクション関連ターンは過去メモを注入しない:
            # - forget: 削除対象の事実が context に居るとモデルが tool を呼ばず
            #   「消しました」と幻覚する (実測: qwen3.6)
            # - sensor: 古い状態記憶が新しい実測と矛盾し、語りを汚染する (実測: gemma4:12b)
            # - action: 古い記憶が提案/実行報告を汚染するのを防ぐ (sensors と同じ思想)
            skip_memories = (is_forget or is_sensor or is_action
                             or pending_confirm is not None or confirm_without_pending)
            memories = [] if skip_memories else await rag.retrieve(
                store, user_message, config=cfg, exclude_session_id=session_id)
            messages = context.build_messages(recent_wo_last, user_message,
                                              cfg.num_ctx, cfg.response_reserve,
                                              memories=memories,
                                              sensor_report=sensor_report,
                                              action_report=action_report)
        else:
            messages = msgs_in

        # 忘却ターンだけツールを渡す。それも forget_memories のみ (M2 ゲート)。
        # センサー/確認ターンはレポートの要約だけさせるのでツールは一切渡さない。
        # アクション提案ターンは gen() 内の隠し呼び出しだけに ACTION_SPECS を公開する。
        use_tools = _FORGET_SPECS if is_forget else None

        async with app.state.write_lock:
            assistant_id = await anyio.to_thread.run_sync(store.add_assistant_placeholder, session_id, model, _now_ms(), source)

        cid = f"chatcmpl-{uuid.uuid4().hex[:24]}"

        async def gen():
            acc: list[str] = []
            status = "partial"          # default partial; only success promotes to complete
            finish = "stop"
            forget_fired = False
            forget_count = 0
            forget_result: dict | None = None
            tool_used_forget = False   # H1: forget を「試みた」かどうか(成否・例外を問わない)
            convo = list(messages)      # tool ラウンドで伸ばす作業用コピー
            tools_for_round = use_tools

            async def _index_pair():
                """user + assistant ターンを索引(失敗しても無害)。"""
                try:
                    await rag.index_turn(store, app.state.write_lock, user_turn_id,
                                         session_id, user_message, config=cfg)
                    text = "".join(acc)
                    if text:
                        await rag.index_turn(store, app.state.write_lock, assistant_id,
                                             session_id, text, config=cfg)
                except Exception:
                    logger.warning("index after chat failed", exc_info=True)

            try:
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={"role": "assistant"}, finish_reason=None))
                if pending_discarded:
                    # 確認以外の発話が来たので保留操作は破棄済み (安全側)。決定的に一言添える。
                    note = "[保留中の操作は取り消しました]\n\n"
                    acc.append(note)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": note}, finish_reason=None))
                if confirm_without_pending:
                    # 実行待ちなしの確認語はサーバ定型で完結 (モデルの実行演技を遮断)。
                    line = actions_module.no_pending_text()
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason="stop"))
                    yield DONE
                    status = "complete"
                    return
                if library_ask:
                    # 書庫検索の意図だが検索語が抽出できなかった。モデルに任せると
                    # 検索した体で語る恐れがあるため、LLM を経ないサーバ定型で聞き返す。
                    line = "何を探すか一言で教えてください"
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason="stop"))
                    yield DONE
                    status = "complete"
                    return
                if is_action:
                    # 提案ターン (安全不変条件 1): 初回ターンでは絶対に実行しない。
                    # モデルには ACTION_SPECS だけ公開し、content は流さず tool_call だけ拾う
                    # (引数抽出をモデルに手伝わせるが、応答文はサーバ定型に固定する)。
                    proposed = None
                    try:
                        async for evt in app.state.chat_fn(
                                convo, model=model, ollama_host=cfg.ollama_host,
                                num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False,
                                tools=actions_module.ACTION_SPECS):
                            if evt["type"] == "tool_call":
                                proposed = evt
                            elif evt["type"] == "error":
                                logger.warning("action elicitation error: %s", evt.get("message"))
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("action elicitation failed", exc_info=True)
                    pa = None
                    build_error = None
                    if proposed is not None and proposed.get("name") in actions_module.ACTION_NAMES:
                        try:
                            pa = actions_module.build_pending(
                                proposed["name"], proposed.get("arguments") or {},
                                session_id=session_id, app_support_dir=app_support_dir,
                                user_message=user_message)
                        except actions_module.ActionError as e:
                            build_error = str(e)  # 台帳の問題 (どの案でも同じ)
                        except Exception:
                            # モデル案の引数不正 (enum 外等) → 決定的構築へフォールバック
                            logger.warning("model-proposed action invalid — fallback", exc_info=True)
                    if pa is None and build_error is None:
                        name, args = actions_module.guess_action(user_message)
                        try:
                            pa = actions_module.build_pending(
                                name, args, session_id=session_id,
                                app_support_dir=app_support_dir, user_message=user_message)
                        except actions_module.ActionError as e:
                            build_error = str(e)
                        except Exception:
                            logger.warning("deterministic action build failed", exc_info=True)
                            build_error = "操作を組み立てられませんでした (内部エラー)"
                    if pa is not None:
                        app.state.pending_actions.put(session_id, pa)
                        line = actions_module.proposal_text(pa)
                    else:
                        line = build_error or "操作を組み立てられませんでした (内部エラー)"
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason="stop"))
                    yield DONE
                    status = "complete"
                    return
                for _ in range(MAX_TOOL_ITERS):
                    pending_tool = None
                    async for evt in app.state.chat_fn(
                            convo, model=model, ollama_host=cfg.ollama_host,
                            num_ctx=cfg.num_ctx, keep_alive=cfg.keep_alive, think=False,
                            tools=tools_for_round):
                        if evt["type"] == "delta":
                            acc.append(evt["content"])
                            yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": evt["content"]}, finish_reason=None))
                        elif evt["type"] == "tool_call":
                            pending_tool = evt
                        elif evt["type"] == "error":
                            status = "error"
                            yield sse(error_frame(evt["message"]))
                            return
                        elif evt["type"] == "done":
                            finish = evt.get("finish_reason", "stop")
                    if pending_tool is None:
                        break  # ツール呼び出しなし = 通常の最終回答で完了
                    fn = app.state.tool_registry.get(pending_tool["name"])
                    if fn is None:
                        logger.warning("unknown tool called: %s", pending_tool.get("name"))
                        break
                    if pending_tool["name"] == "forget_memories" and not is_forget:
                        # 多層防御 (M2): 忘却ゲートを通っていないターンで破壊的ツールは
                        # 実行しない。ツールを渡していなくてもモデル/実装の異常で
                        # tool_call が届く可能性に備える第二層。
                        logger.warning("forget_memories rejected: no forget intent in this turn")
                        break
                    if pending_tool["name"] == "forget_memories":
                        # H1: 試行した時点でフラグを立てる。成否・例外に関わらず
                        # finally での索引スキップ判定はこのフラグだけを見る。
                        tool_used_forget = True
                    try:
                        result = await fn(pending_tool.get("arguments") or {},
                                          store=store, config=cfg, write_lock=app.state.write_lock)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("tool %s failed", pending_tool["name"], exc_info=True)
                        status = "error"
                        yield sse(error_frame(f"tool {pending_tool['name']} failed"))
                        return
                    if pending_tool["name"] == "forget_memories" and "error" not in result:
                        forget_fired = True
                        forget_count = result.get("count", 0)
                        forget_result = result
                    convo = convo + [
                        {"role": "assistant", "content": "", "tool_calls": [
                            {"id": pending_tool.get("id"), "type": "function",
                             "function": {"name": pending_tool["name"],
                                          "arguments": pending_tool.get("arguments") or {}}}]},
                        {"role": "tool", "content": json.dumps(result, ensure_ascii=False)},
                    ]
                    tools_for_round = None  # 2周目以降はツール無し (無限ループ防止の一助)
                # 決定的フォールバック (安全性の要): 明示的忘却意図なのにモデルが forget を
                # 呼ばなかった場合 (qwen3.6 は memories 無しでも ~25% 幻覚で素通りする) →
                # サーバが確実に forget を実行し、「消した」という嘘が実削除なしで通るのを防ぐ。
                if is_forget and not tool_used_forget:
                    tool_used_forget = True   # H1: この往復も索引しない (試行した)
                    try:
                        fb = await app.state.tool_registry["forget_memories"](
                            {"query": _forget_query(user_message)},
                            store=store, config=cfg, write_lock=app.state.write_lock)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.warning("fallback forget failed", exc_info=True)
                        fb = None
                    if isinstance(fb, dict) and "error" not in fb:
                        forget_fired = True
                        forget_count = fb.get("count", 0)
                        forget_result = fb
                if finish == "tool_calls":
                    # ループが tool_call のまま終了した (MAX_TOOL_ITERS 到達 or 未知ツールで break) →
                    # クライアントには非ツールの終端理由を返す (H-Task5-1)
                    finish = "stop"
                if forget_fired:
                    # silent cap 禁止 (spec §5): truncated の場合はモデルの narrative に頼らず
                    # 決定的な行で実際の該当件数と上限到達を明示する。
                    if forget_result and forget_result.get("truncated"):
                        matched = forget_result.get("matched", forget_count)
                        line = f"\n\n[記憶を {forget_count}件 忘れました (該当 {matched}件中、上限まで)]"
                    else:
                        line = f"\n\n[記憶を {forget_count}件 忘れました]"
                    acc.append(line)
                    yield sse(chunk(cid, model, _now_ms() // 1000, delta={"content": line}, finish_reason=None))
                yield sse(chunk(cid, model, _now_ms() // 1000, delta={}, finish_reason=finish))
                yield DONE
                status = "complete"
            except asyncio.CancelledError:   # BaseException-derived: NOT caught by except Exception
                raise
            except Exception as ex:
                status = "error"
                try:
                    yield sse(error_frame(str(ex)))
                except Exception:
                    logger.warning("failed to send error frame to client", exc_info=True)
            finally:
                with anyio.CancelScope(shield=True):
                    async with app.state.write_lock:
                        await anyio.to_thread.run_sync(store.finalize_turn, assistant_id, "".join(acc), None, status, _now_ms())
                        await anyio.to_thread.run_sync(store.touch_session, session_id, _now_ms())
                # H1: 忘却を試みた往復は(成否問わず)索引しない。
                # センサー往復も索引しない (レビュー#7: 揮発性の実測値が「既知の事実」として
                # 将来の会話に注入されるのを防ぐ)。library の検索応答/聞き返しターンも
                # is_sensor 経由で同様に除外。アクション関連往復 (提案/確認/破棄) も
                # 同じ理由で索引しない。
                skip_index = (tool_used_forget or is_sensor or is_action
                              or pending_confirm is not None or pending_discarded
                              or confirm_without_pending)
                if source == "popover" and not skip_index:
                    asyncio.create_task(_index_pair())

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/history")
    async def history(session_id: str | None = None):
        """セッション一覧またはセッション内ターン一覧を取得(読み取り専用)。"""
        store = app.state.store
        if session_id is None:
            return {"sessions": await anyio.to_thread.run_sync(store.list_sessions, 100)}
        return {"session_id": session_id, "turns": await anyio.to_thread.run_sync(store.recent_turns, session_id, 500)}

    @app.post("/v1/admin/model/unload")
    async def model_unload():
        """モデルを手動アンロードして VRAM を即時解放。unload_fn を呼び出し結果を返す。"""
        ok = await app.state.unload_fn()
        app.state._probe_cache["v"] = None  # healthz が古い loaded=true を返さないよう即無効化
        return {"unloaded": bool(ok)}

    @app.post("/v1/admin/model/load")
    async def model_load():
        """モデルを手動ロード(VRAM 先読み)。warmup_fn を呼び出し結果を返す。"""
        ok = await app.state.warmup_fn()
        app.state._probe_cache["v"] = None  # healthz が古い loaded=false を返さないよう即無効化
        return {"loaded": bool(ok)}

    return app

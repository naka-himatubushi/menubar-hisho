"""役割: 秘書 JARVIS のツール群 (REGISTRY) と tool spec 定義。
- forget_memories: 記憶の soft-delete (副作用あり)。忘却ターンでモデルに tool-calling させる
- check_status: センサー実測 (読み取り専用)。**tool 面は封印中** — モデルには渡さず、
  server の決定的事前注入が REGISTRY 経由で呼ぶだけ (実LLMスモークで tool-calling 方式は
  モデルの語りが実測に先行して汚染される欠陥を確認したため)。spec 定義は将来の再開用に残す"""
from __future__ import annotations

import logging
import time
from pathlib import Path

import anyio

from . import rag
from . import sensors

logger = logging.getLogger("hisho")

FORGET_THRESHOLD = 0.85   # 距離(L2,非正規化)。実測校正: 「猫」直接一致≈0.82, 関連≈0.55-0.80, 無関係≈0.91+。0.85 で topic を捕捉し犬/カレー等は残す。soft-delete 可逆。
MAX_FORGET = 15

TOOL_SPECS = [{
    "type": "function",
    "function": {
        "name": "forget_memories",
        "description": (
            "ユーザーが特定の記憶を明示的に「忘れて/消して/覚えなくていい」と要求した時だけ呼ぶ。"
            "query には忘れる対象を表す語句を入れる (例: 猫、私の好物)。"),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "忘れる対象の語句"}},
            "required": ["query"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "check_status",
        "description": (
            "読み取り専用。バックアップ状況・マシンの稼働・ディスク容量など「今どうなってる?」"
            "という実測が必要な質問で呼ぶ。何も変更しない。"),
        "parameters": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "enum": ["backup", "machines", "storage", "health", "library", "all", "briefing"],
                    "description": (
                        "backup=バックアップ状況, machines=マシンの稼働状態, "
                        "storage=ディスク容量/温度, health=mini の監視レポート/警報, "
                        "library=書庫 (Library-DB) のファイル検索, "
                        "all=まとめて全部 (library は検索語が要るため含まない), "
                        "briefing=朝のブリーフィング (all 相当の実測 + 期限リストの残日数)"),
                },
                "query": {
                    "type": "string",
                    "description": "topic=library の時だけ使う書庫の検索語",
                },
            },
            "required": ["topic"],
        },
    },
}]


async def forget_memories(args, *, store, config, write_lock, embed=rag.embed, now_ms=None):
    """query に意味マッチする active な turn/document チャンクを soft-delete する。
    戻り値: {count, matched, truncated, items} / embed 失敗時 {error, message}。"""
    query = (args or {}).get("query", "")
    query = query.strip() if isinstance(query, str) else ""
    if not query:
        return {"count": 0, "matched": 0, "truncated": False, "items": []}

    blobs = await embed([query], model=config.embed_model, ollama_host=config.ollama_host)
    if not blobs:
        logger.warning("forget: embed 失敗")
        return {"error": "embed_failed", "message": "今 記憶を整理できません"}

    hits = await anyio.to_thread.run_sync(
        lambda: store.search_forgettable(blobs[0], MAX_FORGET * 3))
    matched = [h for h in hits if h["distance"] < FORGET_THRESHOLD]
    truncated = len(matched) > MAX_FORGET
    chosen = matched[:MAX_FORGET]
    if not chosen:
        return {"count": 0, "matched": 0, "truncated": False, "items": []}

    ts = now_ms if now_ms is not None else int(time.time() * 1000)
    chunk_ids = [h["id"] for h in chosen]
    turn_ids = [h["source_id"] for h in chosen if h["source_type"] == "turn"]
    async with write_lock:
        await anyio.to_thread.run_sync(store.soft_delete_chunks, chunk_ids, ts)
        await anyio.to_thread.run_sync(store.mark_turns_forgotten, turn_ids)

    return {
        "count": len(chosen),
        "matched": len(matched),
        "truncated": truncated,
        "items": [h["content"][:60] for h in chosen],
    }


async def _measure_ledger_topic(topic: str, *, store, config) -> str:
    """backup/machines/storage/health/all の台帳実測本体を組み立てる
    (「HH:MM 実測」ヘッダ + 整形済みレポート文字列。dict 化は呼び出し側の責務)。
    全項目実測失敗なら store の最新 status チャンクを「最終既知値」として追記する。
    briefing はこれを topic="all" で呼び、期限セクションを追記するだけで済ませる
    (briefing が all を包含する側 — all 側には briefing 用の分岐を足さない)。"""
    app_support_dir = Path(config.db_path).expanduser().parent
    items, missing = await anyio.to_thread.run_sync(sensors.ledger_items, topic, app_support_dir)
    header = sensors.now_header()

    if not items:
        body = "\n".join(missing) if missing else "台帳にコマンドが登録されていません"
        return f"{header}\n\n{body}"

    results = await anyio.to_thread.run_sync(sensors.run_all, items)
    report = f"{header}\n\n{sensors.format_report(results, missing)}"

    if sensors.all_failed(results):
        latest = await anyio.to_thread.run_sync(store.latest_status_chunk)
        if latest:
            report = f"{report}\n\n実測できなかったため最終既知値 (定期収集分):\n{latest}"

    return report


async def check_status(args, *, store, config, write_lock=None):
    """topic (backup/machines/storage/health/library/all/briefing) を実測し
    「HH:MM 実測」ヘッダつきレポートを返す。読み取り専用 — DB を書き換えない
    (write_lock は forget_memories と同じ呼び出し規約に合わせるため受け取るだけで使わない)。
    戻り値: {topic, report}。
    topic=library は台帳でなく args["query"] (server の決定的抽出由来) で書庫を検索する。
    topic=briefing は all 相当の実測結果 + 期限セクション (briefing_targets.json の残日数、
    サーバ側 Python で決定的に計算。LLM には計算させない) を1レポートに合成する。"""
    topic = (args or {}).get("topic")
    if topic not in ("backup", "machines", "storage", "health", "library", "all", "briefing"):
        topic = "all"  # LLM が enum 外を出しても落とさず全体で拾う (安全側)

    if topic == "library":
        # 書庫検索: 台帳の固定 cmd と違い動的な検索語が要るため専用経路。
        # query は server.extract_library_query (regex 決定的抽出) 由来で、LLM には作らせない。
        # 検索結果は揮発値で status チャンクと無関係なので、最終既知値フォールバックは付けない。
        query = (args or {}).get("query", "")
        query = query.strip() if isinstance(query, str) else ""
        header = sensors.now_header()
        if not query:
            # server 側で定型応答に落ちるため通常ここには来ない (防御的な第二層)
            return {"topic": "library",
                    "report": f"{header}\n\n書庫検索: 検索語が空のため実行しませんでした"}
        item = await anyio.to_thread.run_sync(
            sensors.library_search, query, config.library_db_dir)
        return {"topic": "library",
                "report": f"{header}\n\n{sensors.format_report([item], [])}"}

    if topic == "briefing":
        # 朝ブリーフィング: all 相当の実測 + 期限セクション。期限リストの欠損/壊れは
        # deadlines_report 側が「期限リストなし」に丸めるのでここでは例外を気にしない。
        sensor_body = await _measure_ledger_topic("all", store=store, config=config)
        deadlines = await anyio.to_thread.run_sync(
            sensors.deadlines_report, config.briefing_targets_path)
        return {"topic": "briefing", "report": f"{sensor_body}\n\n{deadlines}"}

    report = await _measure_ledger_topic(topic, store=store, config=config)
    return {"topic": topic, "report": report}


REGISTRY = {"forget_memories": forget_memories, "check_status": check_status}

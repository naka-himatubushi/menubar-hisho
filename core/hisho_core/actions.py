"""役割: JARVIS の「手」。固定レジストリ2アクション (start_backup / fleet_submit) の
argv 組み立て・pending 管理・確認語判定・実行を担う。

安全前提 (このモジュールを変更する時に必ず守ること):
- アクションは start_backup / fleet_submit の 2 本だけ。これ以外は永遠に作らない前提で設計
- 実行は必ず argv リスト直渡し (shell=False)。task などユーザー由来の文字列は
  argv の 1 要素として渡し、コマンドとして解釈される経路をつくらない
- ssh 宛先・work CLI パスは台帳 (action_targets.json、リポジトリ外) から読む。
  コードに IP/ホスト名/ユーザー名を書かない — リポジトリは public
- 実行は確認フローを通ってのみ (このモジュールは実行の道具を提供するだけで、
  「いつ実行するか」は server の 提案→確認 状態機械が決める)
- pending は session 束縛・TTL 300 秒・pop で一回限り。プロセス再起動で消えて良い (安全側)
"""
from __future__ import annotations

import json
import logging
import re
import shlex
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

logger = logging.getLogger("hisho")

ACTIONS_LEDGER = "action_targets.json"
ACTION_NAMES = ("start_backup", "fleet_submit")
BACKUP_MACHINES = ("macbook", "studio", "mini")
FLEET_MACHINES = ("studio", "mini")
EXEC_TIMEOUT = 30      # 秒。アクション実行の上限 (fleet_submit は投入だけなので数秒)
PENDING_TTL = 300.0    # 秒。提案から確認までの猶予 (5分)

# LLM がモデル提案ターンで見る tool specs。実行には絶対つながらない
# (server は tool_call を pending に変換するだけ。tools.REGISTRY にも載せない)。
ACTION_SPECS = [{
    "type": "function",
    "function": {
        "name": "start_backup",
        "description": (
            "Time Machine バックアップの開始を提案する。実行はユーザーが「はい」で"
            "確認した後にサーバが行う。冪等 (既に実行中なら無害)。"),
        "parameters": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "enum": list(BACKUP_MACHINES),
                    "description": "バックアップを開始する機体",
                },
            },
            "required": ["machine"],
        },
    },
}, {
    "type": "function",
    "function": {
        "name": "fleet_submit",
        "description": (
            "Studio または mini に作業タスクを投げる提案を作る。実行はユーザーが"
            "「はい」で確認した後にサーバが行う。"),
        "parameters": {
            "type": "object",
            "properties": {
                "machine": {
                    "type": "string",
                    "enum": list(FLEET_MACHINES),
                    "description": "タスクを投げる機体",
                },
                "task": {
                    "type": "string",
                    "description": "投げる作業内容 (自由文)",
                },
            },
            "required": ["machine", "task"],
        },
    },
}]


class ActionError(Exception):
    """台帳欠落・宛先未設定など、ユーザーにそのまま見せられる説明を message に持つ。"""


@dataclass
class PendingAction:
    """確認待ちの操作 1 件。argv は組み立て済み (確認後はそのまま実行するだけ)。"""
    action: str
    args: dict
    argv: list[str]
    display: str        # argv の人間可読形 (提案文と実行報告に使う)
    session_id: str


class PendingActions:
    """session_id → 確認待ち操作。TTL 300 秒、取り出しは pop で一回限り。
    プロセス内メモリのみ (再起動で消える = 安全側)。clock はテスト用の注入口。"""

    def __init__(self, ttl: float = PENDING_TTL, clock: Callable[[], float] = time.monotonic):
        self._items: dict[str, tuple[float, PendingAction]] = {}
        self.ttl = ttl
        self._clock = clock

    def put(self, session_id: str, pa: PendingAction) -> None:
        self._items[session_id] = (self._clock(), pa)

    def pop(self, session_id: str) -> PendingAction | None:
        """自分の session の pending を取り出して消す (一回限り)。
        無い・期限切れは None。期限切れも消えたまま (安全側)。"""
        entry = self._items.pop(session_id, None)
        if entry is None:
            return None
        created, pa = entry
        if self._clock() - created > self.ttl:
            return None
        return pa


# 確認語: 短文の先頭一致のみ。「はいはい、話戻すけど」のような長文では成立しない。
_CONFIRM = re.compile(r"^(はい|yes|ok|やって|実行して)[。!！.]?$", re.IGNORECASE)


def is_confirmation(text: str) -> bool:
    """発話全体が確認語 (はい/yes/ok/やって/実行して + 句点程度) かどうか。"""
    return bool(_CONFIRM.match((text or "").strip()))


_RX_STUDIO = re.compile(r"スタジオ|studio", re.IGNORECASE)
_RX_MINI = re.compile(r"ミニ|mini", re.IGNORECASE)
_RX_BACKUP = re.compile(r"バックアップ|TM", re.IGNORECASE)


def guess_action(user_message: str) -> tuple[str, dict]:
    """決定的フォールバック用にユーザー発話からアクションと引数を推定する。
    バックアップ語があれば start_backup (機体語なしは macbook)、
    なければ fleet_submit (task はユーザー発話全文)。
    取り違えても実行は確認後なので無害 (ユーザーが「はい」を出さなければ流れる)。"""
    if _RX_BACKUP.search(user_message):
        if _RX_STUDIO.search(user_message):
            machine = "studio"
        elif _RX_MINI.search(user_message):
            machine = "mini"
        else:
            machine = "macbook"
        return "start_backup", {"machine": machine}
    if _RX_STUDIO.search(user_message):
        machine = "studio"
    elif _RX_MINI.search(user_message):
        machine = "mini"
    else:
        machine = "studio"  # ゲートは機体語で開くので通常来ない。防御的既定
    return "fleet_submit", {"machine": machine, "task": user_message}


def _load_ledger(app_support_dir: Path | str) -> dict:
    """台帳を読む。無い/壊れている/形式不正は ActionError (ユーザー向け説明つき)。"""
    path = Path(app_support_dir) / ACTIONS_LEDGER
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ActionError(
            f"{ACTIONS_LEDGER} が見つかりません (台帳未設置)。操作を提案できません") from None
    except Exception as e:
        logger.warning("action 台帳の読み込みに失敗: %s", path, exc_info=True)
        raise ActionError(f"{ACTIONS_LEDGER} が読めません: {e}") from None
    if not isinstance(data, dict):
        raise ActionError(f"{ACTIONS_LEDGER} の形式が不正です")
    return data


def build_pending(action: str, args: dict, *, session_id: str,
                  app_support_dir: Path | str, user_message: str) -> PendingAction:
    """アクション名と引数を検証して argv を組み立て、確認待ち PendingAction を返す。

    - action / machine が enum 外なら ValueError (LLM 由来の値を弾く境界)
    - 台帳の欠落・宛先未設定は ActionError (ユーザー向け説明)
    - fleet_submit の task 既定はユーザー発話全文。argv の 1 要素として渡す
      (shell=False 前提なのでコマンドとして解釈されない)
    """
    args = args if isinstance(args, dict) else {}
    ledger = _load_ledger(app_support_dir)

    if action == "start_backup":
        machine = args.get("machine")
        if machine not in BACKUP_MACHINES:
            raise ValueError(f"start_backup: unknown machine {machine!r}")
        if machine == "macbook":
            argv = ["tmutil", "startbackup"]
        else:
            dest = (ledger.get("backup_ssh") or {}).get(machine)
            if not isinstance(dest, str) or not dest:
                raise ActionError(f"台帳に {machine} の ssh 宛先がありません。操作を提案できません")
            # リモート側コマンドは人間が固定した文字列のみ (ユーザー入力は混ぜない)
            argv = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8",
                    dest, "tmutil startbackup"]
        return PendingAction(action=action, args={"machine": machine}, argv=argv,
                             display=shlex.join(argv), session_id=session_id)

    if action == "fleet_submit":
        machine = args.get("machine")
        if machine not in FLEET_MACHINES:
            raise ValueError(f"fleet_submit: unknown machine {machine!r}")
        task = args.get("task")
        if not isinstance(task, str) or not task.strip():
            task = user_message  # 既定はユーザー発話全文
        work_cli = ledger.get("work_cli")
        if not isinstance(work_cli, str) or not work_cli:
            raise ActionError("台帳に work_cli のパスがありません。操作を提案できません")
        # task は argv の 1 要素。shell を経由しないのでそのまま文字列として届く
        argv = [str(Path(work_cli).expanduser()), machine, task]
        return PendingAction(action=action, args={"machine": machine, "task": task},
                             argv=argv, display=shlex.join(argv), session_id=session_id)

    raise ValueError(f"unknown action: {action!r}")


def proposal_text(pa: PendingAction) -> str:
    """提案ターンのサーバ定型文 (モデル生成に任せない)。実際に走る argv を可読形で提示。"""
    return f"実行内容: {pa.display}\n実行していい? (はい で実行、5分で無効)"


def execution_report(pa: PendingAction, output: str,
                     now: Callable[[], datetime] = datetime.now) -> str:
    """確認後に実行した結果のサーバ定型レポート (文脈注入用)。実行時刻つき。"""
    return (f"{now().strftime('%H:%M')} 実行\n"
            f"実行内容: {pa.display}\n"
            f"結果:\n{output}")


def execute(argv: list[str], *, timeout: int = EXEC_TIMEOUT) -> str:
    """argv を shell 非経由 (shell=False) で実行し、結果を人間可読文字列で返す。
    例外は投げない (timeout / コマンド不在 / 非ゼロ exit は全て説明文になる)。
    テスト/CI では server 側の executor 注入でこの関数ごと差し替えられる。"""
    try:
        p = subprocess.run(argv, shell=False, capture_output=True, text=True,
                           timeout=timeout)
    except FileNotFoundError:
        return f"実行失敗: コマンドが見つかりません ({argv[0]})"
    except subprocess.TimeoutExpired:
        return f"実行失敗: タイムアウト ({timeout}秒)"
    except Exception as e:  # noqa: BLE001 — 実行係は何があっても説明文で返す
        logger.warning("action execute failed", exc_info=True)
        return f"実行失敗: {e}"
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0:
        return f"実行失敗 (exit {p.returncode}):\n{err or out or '(出力なし)'}"
    return out or "(出力なし。コマンドは正常終了)"

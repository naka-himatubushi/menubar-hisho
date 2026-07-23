"""役割: JARVIS の「手」。固定レジストリ3アクション (start_backup / fleet_submit / atelier) の
argv 組み立て・pending 管理・確認語判定・実行を担う。

安全前提 (このモジュールを変更する時に必ず守ること):
- アクションはこの3本だけ。追加する時は必ずこの安全前提を保てるか再検証する
  (2026-07-23: atelier 追加。repo-key を allow-list 完全一致でしか受け付けず、
  実行内容は laguna 工房への「投げるだけ」dispatch script 1本に固定 (merge/検収はしない)
  という、既存2本と同じ「確認後にサーバが決定的に実行するだけ」の型に収まる設計だったため
  「2本だけ」の前提を緩めて追加した — 4本目以降を足す時も同じ再検証を必ず行うこと)
- 実行は必ず argv リスト直渡し (shell=False)。task などユーザー由来の文字列は
  argv の 1 要素として渡し、コマンドとして解釈される経路をつくらない
- ssh 宛先・work CLI パスは台帳 (action_targets.json、リポジトリ外) から読む。
  atelier の repo パスも同様に別台帳 (atelier_targets.json、リポジトリ外) の allow-list から。
  コードに IP/ホスト名/ユーザー名/実ファイルパスを書かない — リポジトリは public
- 実行は確認フローを通ってのみ (このモジュールは実行の道具を提供するだけで、
  「いつ実行するか」は server の 提案→確認 状態機械が決める)
- pending は session 束縛・TTL 300 秒・pop で一回限り。プロセス再起動で消えて良い (安全側)
- atelier の repo-key/task-text 抽出は LLM を一切経由しない (サーバの regex + 台帳照合のみ)。
  ACTION_SPECS には載せるが ACTION_NAMES には加えない — モデル発の tool_call 経由で
  atelier の PendingAction が作られる経路を構造的に閉じておくため (下記 ACTION_NAMES 参照)
"""
from __future__ import annotations

import json
import logging
import os
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
ATELIER_LEDGER = "atelier_targets.json"   # 工房発注の repo allow-list ({"repos": {key: path}})
# モデルの隠し呼び出し (elicitation) が PendingAction を作れるのはこの2つだけ。
# atelier は ACTION_SPECS には載るが意図的にここへは加えない — repo-key を LLM に
# 決めさせない設計であり、resolve_atelier_repo がサーバ側だけで判定を完結させる。
ACTION_NAMES = ("start_backup", "fleet_submit")
BACKUP_MACHINES = ("macbook", "studio", "mini")
FLEET_MACHINES = ("studio", "mini")
EXEC_TIMEOUT = 30      # 秒。アクション実行の上限 (fleet_submit/atelier は投入だけなので数秒)
PENDING_TTL = 300.0    # 秒。提案から確認までの猶予 (5分)
# 工房 dispatch script の場所。.app bundle には scripts/ が同梱されない (__file__ は
# site-packages 配下になり、相対で辿っても scripts/ に届かない) ため、安定実体である
# repo working copy を home 固定の既定で指す。HISHO_ATELIER_SCRIPT で上書き可。
# 無くても build_pending はここでは気にしない (execute() が FileNotFoundError を
# 説明文に変換してくれる — 安全側。actions.py 冒頭の shell=False 方針と同じ理由で
# パスは argv の 1 要素として渡すだけで、シェル経由では組み立てない)。
ATELIER_DISPATCH_SCRIPT = Path(os.environ.get(
    "HISHO_ATELIER_SCRIPT",
    str(Path.home() / "sandbox" / "menubar-hisho" / "scripts" / "atelier_dispatch.sh")))

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
}, {
    "type": "function",
    "function": {
        "name": "atelier",
        "description": (
            "会話から実装ジョブを laguna 工房 (Studio) へ発注する提案を作る。実行は"
            "ユーザーが「はい」で確認した後にサーバが行う。repo/task の決定はサーバが"
            "決定的に行うため、このツールをモデルが直接呼んで実行に繋がる経路は無い"
            "(ACTION_NAMES に含まれない — 提案の記録用の spec)。"),
        "parameters": {
            "type": "object",
            "properties": {
                "repo": {
                    "type": "string",
                    "description": "発注先リポジトリの key (atelier_targets.json の allow-list)",
                },
                "task": {
                    "type": "string",
                    "description": "発注する実装タスクの内容",
                },
            },
            "required": ["repo", "task"],
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
        # 墓標: 直近まで pending が居た session。破棄/期限切れ/実行済み直後の
        # 確認語 (「はい」) を日常会話の「はい」と区別するために使う。
        self._gone: dict[str, float] = {}
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
        self._gone[session_id] = self._clock()
        created, pa = entry
        if self._clock() - created > self.ttl:
            return None
        return pa

    def recently_gone(self, session_id: str) -> bool:
        """直近 (TTL 内) までこの session に pending が存在したか (墓標参照)。"""
        t = self._gone.get(session_id)
        if t is None:
            return False
        if self._clock() - t > self.ttl:
            del self._gone[session_id]
            return False
        return True


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


# --- 工房発注 (atelier): repo-key/task-text はサーバが完全決定的に抽出する。
# LLM には一切頼らない (repo を allow-list の外に広げさせないための設計上の要請。
# start_backup/fleet_submit のモデル隠し呼び出し + guess_action フォールバックとは別経路)。

# ゲート語。「作って」「直して」は日常会話 (資料/予定/お弁当等) でも頻出する動詞のため
# bare では採用しない — 既存 action_intent ゲートが bare「して」を避け動詞+名詞の組み合わせに
# 絞っているのと同じ判断。「実装して」は開発文脈以外でまず使われない語なので許可するが、
# 「実装してある/あります/あった」(既存実装の状態を尋ねる文) との誤爆だけは、後続が
# あ+る/り/っ (ある・あります・あった等の活用の頭) の時に除外する。
# 「工房に」は本機能専用の固有表現で日常語とはまず衝突しないが、「陶芸工房に行った」のような
# 無関係文を避けるため発注系の動詞を伴う時だけ発火させる。動詞までの距離 40 字は
# 「工房に aws-dojo のクイズ採点バグ修正を発注して」のように repo 名+タスク句を跨ぐ
# 実話法を拾うための幅 (誤発火しても後段の repo-key 解決が第二関門で、実害は聞き返し1回)。
ATELIER_GATE_PATTERN = (
    # 注意: alternation は最初にマッチした枝で確定する (最長一致ではない) ため、
    # 長い活用 (ておいて/といて) を先に置き、bare「て」は最後に置く。
    r"実装し(?:ておいて|といて|て(?!あ[るりっ]))"
    r"|工房(?:に|で).{0,40}?(?:発注|投げ|頼ん|お願い|やらせ)"
)
_ATELIER_GATE = re.compile(ATELIER_GATE_PATTERN, re.IGNORECASE)


def is_atelier_intent(user_message: str) -> bool:
    """action_intent ゲートが発火した発話が工房発注語によるものかを判定する。
    server はこれで start_backup/fleet_submit の隠し呼び出しフローと分岐する。"""
    return bool(_ATELIER_GATE.search(user_message or ""))


def _load_atelier_repos(app_support_dir: Path | str) -> dict:
    """atelier_targets.json の repos (key→絶対パス) を読む。無い/壊れは ActionError。"""
    path = Path(app_support_dir) / ATELIER_LEDGER
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        raise ActionError(
            f"{ATELIER_LEDGER} が見つかりません (台帳未設置)。操作を提案できません") from None
    except Exception as e:
        logger.warning("atelier 台帳の読み込みに失敗: %s", path, exc_info=True)
        raise ActionError(f"{ATELIER_LEDGER} が読めません: {e}") from None
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict) or not repos:
        raise ActionError(f"{ATELIER_LEDGER} の形式が不正です (repos が空/不正)")
    return repos


def resolve_atelier_repo(user_message: str, app_support_dir: Path | str) -> str | None:
    """発話に含まれる repo-key を allow-list から完全一致 (大小無視) で探す。
    0件/複数件一致は曖昧とみなし None を返す (呼び出し側が「どのリポジトリ?」と聞き返す —
    _guess_topic の複数一致→all 送りと同じ「測りすぎ/聞きすぎ側」の安全な倒し方)。
    台帳の欠落/破損は ActionError で呼び出し側に伝える。"""
    repos = _load_atelier_repos(app_support_dir)
    msg = (user_message or "").lower()
    matched = [key for key in repos if key.lower() in msg]
    return matched[0] if len(matched) == 1 else None


def atelier_repo_keys(app_support_dir: Path | str) -> list[str]:
    """聞き返し文言 (「どのリポジトリ?」) に列挙する repo-key 一覧 (台帳登録順)。"""
    return list(_load_atelier_repos(app_support_dir).keys())


# task-text 抽出用の削除リスト (extract_library_query と同型: 定型句を順に剥がして
# 残りを task-text とする決定的抽出。LLM には抽出させない)。
_ATELIER_WORKSHOP_RX = re.compile(
    r"工房(?:に|で).{0,10}?(?:発注|投げ|頼ん|お願い|やらせ)(?:して)?(?:ください|くれ)?")
_ATELIER_VERB_RX = re.compile(
    # ATELIER_GATE_PATTERN と同じ理由で長い活用を先に置く (最長一致ではないため)。
    r"(?:を|の)?(?:実装し(?:ておいて|といて|て(?!あ[るりっ]))"
    r"|作っ(?:ておいて|といて|て)|直し(?:ておいて|といて|て))"
    r"(?:ください|くれ|ほしい|もらえる)?")
# 削除後にこれ「だけ」残ったら task-text なしとみなす助詞 (_LIBRARY_PARTICLE_ONLY と同型)
_ATELIER_PARTICLE_ONLY = frozenset("にでをはがのもへとか")


def extract_atelier_task(text: str, repo_key: str) -> str:
    """工房発注の task-text を発話から決定的に抽出する (LLM を経ない)。
    repo-key の言及 (と隣接助詞) → 工房語 → 実装/作成/修正の定型語、の順で剥がし、
    残りを task-text として返す。例:
    「library-db に検索機能を実装しといて」→「検索機能」。
    空文字を返したら呼び出し側が「何を発注するか」を聞き返す。"""
    q = text or ""
    if repo_key:
        q = re.sub(re.escape(repo_key) + r"\s*(?:用に|向けに|に|で|の|は)?", "", q,
                   flags=re.IGNORECASE)
    q = _ATELIER_WORKSHOP_RX.sub("", q)
    q = _ATELIER_VERB_RX.sub("", q)
    q = q.strip(" \t\r\n　、。・?？!！「」『』")
    if q in _ATELIER_PARTICLE_ONLY:
        return ""
    return q


def build_pending(action: str, args: dict, *, session_id: str,
                  app_support_dir: Path | str, user_message: str) -> PendingAction:
    """アクション名と引数を検証して argv を組み立て、確認待ち PendingAction を返す。

    - action / machine が enum 外なら ValueError (LLM 由来の値を弾く境界)
    - 台帳の欠落・宛先未設定は ActionError (ユーザー向け説明)
    - fleet_submit の task 既定はユーザー発話全文。argv の 1 要素として渡す
      (shell=False 前提なのでコマンドとして解釈されない)
    - atelier は repo-key/task を呼び出し側 (server) が resolve_atelier_repo /
      extract_atelier_task で既に検証済みの値として渡してくる前提。ここでは
      action_targets.json は読まない (atelier_targets.json は resolve 側で既に
      照合済みのため、ここで二重に読む理由がない)
    """
    args = args if isinstance(args, dict) else {}

    if action == "start_backup":
        ledger = _load_ledger(app_support_dir)
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
        ledger = _load_ledger(app_support_dir)
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

    if action == "atelier":
        repo = args.get("repo")
        task = args.get("task")
        if not isinstance(repo, str) or not repo:
            raise ValueError(f"atelier: missing repo {repo!r}")
        if not isinstance(task, str) or not task.strip():
            raise ValueError("atelier: missing task")
        # repo は呼び出し側 (resolve_atelier_repo) が allow-list 完全一致で既に検証済み。
        # ここでは argv を組み立てるだけ (dispatch script 側も許可リストを再照合する —
        # 「許可リスト外 repo は拒否」の多層防御)。
        argv = [str(ATELIER_DISPATCH_SCRIPT), repo, task]
        display = f"工房発注 repo={repo} タスク=「{task}」"
        return PendingAction(action=action, args={"repo": repo, "task": task}, argv=argv,
                             display=display, session_id=session_id)

    raise ValueError(f"unknown action: {action!r}")


def proposal_text(pa: PendingAction) -> str:
    """提案ターンのサーバ定型文 (モデル生成に任せない)。実際に走る argv を可読形で提示。"""
    if pa.action == "atelier":
        return (f"工房に発注します: repo={pa.args['repo']}, タスク=「{pa.args['task']}」。"
                f"よろしいですか (はい で実行、5分で無効)")
    return f"実行内容: {pa.display}\n実行していい? (はい で実行、5分で無効)"


def no_pending_text() -> str:
    """確認語が来たが実行待ちが無い時のサーバ定型。モデルに任せると実行を
    演技する (実LLMスモークで実測) ため、決定的に止める。"""
    return "実行待ちの操作はありません (提案は5分で無効・一回限り)。必要ならもう一度依頼してください"


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

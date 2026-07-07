# sensors「health」topic 実装計画

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** JARVIS が mini の定例監視レポート (/tmp/health/*.json と Discord 朝レポート) を「聞かれた瞬間の実測」で読めるようにする。

**Architecture:** 既存 sensors (台帳駆動・読み取り専用・決定的事前注入) に topic「health」を1つ追加するだけ。sensors.py の enum + 分岐、server.py のキーワード1行、台帳 (リポジトリ外) に2エントリ。新しい仕組みは作らない。

**Tech Stack:** Python 3.13 / pytest / uv。台帳コマンドは ssh + python3 one-liner (shlex.quote で機械的に組み立て)。

**Spec:** `docs/superpowers/specs/2026-07-07-health-topic-design.md`

## Global Constraints

- 読み取り専用。書き込み・起動系コマンドは台帳に登録しない (sensors.py 冒頭の安全契約)
- Discord token は mini の `~/.secrets/.env` から出さない (MacBook に置かない)
- タイムアウトは既存値を変えない: コマンド8秒 / topic 全体12秒
- 既存テストを1本も壊さない。特に `_guess_topic("調子どう?") == "all"` は固定挙動 (「調子」を health 語群に入れない)
- 全ファイル先頭の役割 docstring を維持
- コミットはレビュー担当 (監督側) が行う。実装サブエージェントは commit しない
- テスト実行はすべて `cd ~/sandbox/menubar-hisho/core && uv run pytest ...`

---

### Task 1: sensors.py — TOPICS と ledger_items に health を追加

**Files:**
- Modify: `core/hisho_core/sensors.py:35` (TOPICS)、`core/hisho_core/sensors.py:147-157` (ledger_items の sensor_targets 分岐)
- Test: `core/tests/test_sensors.py` (末尾に追記)

**Interfaces:**
- Consumes: なし (既存構造のみ)
- Produces: `sensors.TOPICS` に `"health"` が含まれる。`ledger_items("health", dir)` が `sensor_targets.json` の `topics.health` を返す。`ledger_items("all", dir)` は machines/storage/health を合算。

- [ ] **Step 1: 失敗するテストを書く** — `core/tests/test_sensors.py` の `test_ledger_items_reads_machines_and_storage_topics` の直後に追記:

```python
def test_ledger_items_reads_health_topic(tmp_path):
    (tmp_path / "backup_targets.json").write_text('{"devices": []}')
    (tmp_path / "sensor_targets.json").write_text(
        '{"topics": {"machines": [{"name": "M1", "cmd": "echo"}], '
        '"storage": [{"name": "S1", "cmd": "echo"}], '
        '"health": [{"name": "H1", "cmd": "echo"}]}}')
    items, missing = sensors.ledger_items("health", tmp_path)
    assert [i["name"] for i in items] == ["H1"]
    assert missing == []

    items_all, _ = sensors.ledger_items("all", tmp_path)
    assert {i["name"] for i in items_all} == {"M1", "S1", "H1"}


def test_topics_enum_contains_health():
    assert "health" in sensors.TOPICS
```

- [ ] **Step 2: 失敗を確認**

Run: `cd ~/sandbox/menubar-hisho/core && uv run pytest tests/test_sensors.py -k health -v`
Expected: FAIL (`ValueError: unknown topic: 'health'` と AssertionError)

- [ ] **Step 3: 最小実装** — `core/hisho_core/sensors.py` を2箇所変更:

35行目:
```python
TOPICS = ("backup", "machines", "storage", "health", "all")
```

`ledger_items()` 内の sensor_targets 分岐 (現147行目付近):
```python
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
```

あわせて冒頭 docstring 10行目の enum 列挙を更新:
```
- topic は "backup" / "machines" / "storage" / "health" / "all" の enum だけを受け付ける。
```

- [ ] **Step 4: テスト通過を確認**

Run: `cd ~/sandbox/menubar-hisho/core && uv run pytest tests/test_sensors.py -v`
Expected: 全 PASS (既存分含む)

注: 既存台帳に health セクションがまだ無い状態で `all` を測っても、`topics_data.get("health", [])` は空リスト → 欠落メッセージも出ない (missing に積まれるのはファイル自体の欠落と形式不正のみ)。後方互換。

- [ ] **Step 5: Commit** (監督が実行)

```bash
cd ~/sandbox/menubar-hisho && git add core/hisho_core/sensors.py core/tests/test_sensors.py && git commit -m "feat(sensors): health topic を TOPICS と台帳読込に追加"
```

---

### Task 2: server.py — health のキーワードルーティング

**Files:**
- Modify: `core/hisho_core/server.py:41-45` (_TOPIC_PATTERNS)
- Test: `core/tests/test_server_sensors.py:102-109` (test_guess_topic_matrix に追記)

**Interfaces:**
- Consumes: Task 1 の `sensors.TOPICS` に "health" が存在すること (measure が ValueError を出さない)
- Produces: `_guess_topic()` が「警報|異常|アラート|レポート|健康」単独一致で `"health"` を返す

- [ ] **Step 1: 失敗するテストを書く** — `test_guess_topic_matrix` に4行追記:

```python
    assert _guess_topic("朝レポート見せて") == "health"
    assert _guess_topic("何か異常出てる?") == "health"
    assert _guess_topic("警報鳴った?") == "health"
    assert _guess_topic("バックアップの異常は?") == "all"   # backup+health 2群 → all
```

- [ ] **Step 2: 失敗を確認**

Run: `cd ~/sandbox/menubar-hisho/core && uv run pytest tests/test_server_sensors.py::test_guess_topic_matrix -v`
Expected: FAIL (「朝レポート見せて」が "all" になる)

- [ ] **Step 3: 最小実装** — `_TOPIC_PATTERNS` に1行追加:

```python
_TOPIC_PATTERNS = (
    ("backup", re.compile(r"バックアップ")),
    ("storage", re.compile(r"温度|容量|空き|ディスク")),
    ("machines", re.compile(r"稼働|生きて|落ちて|マシン|動い")),
    ("health", re.compile(r"警報|異常|アラート|レポート|健康")),
)
```

「調子」は入れない (既存テスト `調子どう? == "all"` の固定挙動を守る。全部測る方が自然)。

- [ ] **Step 4: テスト通過を確認**

Run: `cd ~/sandbox/menubar-hisho/core && uv run pytest tests/test_server_sensors.py tests/test_sensors.py -v`
Expected: 全 PASS

- [ ] **Step 5: 回帰確認 (全テスト)**

Run: `cd ~/sandbox/menubar-hisho/core && uv run pytest -q`
Expected: 全 PASS

- [ ] **Step 6: Commit** (監督が実行)

```bash
cd ~/sandbox/menubar-hisho && git add core/hisho_core/server.py core/tests/test_server_sensors.py && git commit -m "feat(sensors): health topic のキーワードルーティング"
```

---

### Task 3: 台帳に health セクションを追加 (リポジトリ外) + 実測スモーク

**Files:**
- Modify: `~/Library/Application Support/Hisho/sensor_targets.json` (リポジトリ外・人間管理台帳。コミットしない)
- Create: なし (更新スクリプトは scratchpad に書き捨て)

**Interfaces:**
- Consumes: Task 1 の `ledger_items("health", ...)`
- Produces: `topics.health` に2エントリ (「mini 健康サマリ」「朝レポート」)

- [ ] **Step 1: 台帳をバックアップ**

```bash
cp "$HOME/Library/Application Support/Hisho/sensor_targets.json" "$HOME/Library/Application Support/Hisho/sensor_targets.json.bak-20260707"
```

- [ ] **Step 2: 更新スクリプトで health セクションを追記** — 手編集は quote 地獄になるため、shlex.quote で機械的に組み立てる。scratchpad に以下を保存して実行:

```python
"""役割: sensor_targets.json に topics.health の2エントリを追記する書き捨てスクリプト。"""
import json, shlex
from pathlib import Path

LEDGER = Path.home() / "Library" / "Application Support" / "Hisho" / "sensor_targets.json"
MINI = "<mini-user>@<mini-tailnet-ip>"
# ConnectTimeout 4 + curl max-time 3 = 最悪 7 秒 < COMMAND_TIMEOUT 8 秒 (レビュー指摘対応)
SSH = f"ssh -o BatchMode=yes -o ConnectTimeout=4 {MINI} "

SUMMARY = '''
import json
def g(n):
    try:
        return json.load(open("/tmp/health/" + n))
    except Exception:
        return None
s = g("system-health.json")
if s:
    m = s.get("memory", {}); c = s.get("cpu_load", {}); dk = s.get("docker", {})
    print("mini本体: メモリ" + str(m.get("percent", "?")) + "% / CPU5分 " + str(c.get("5min", "?"))
          + " / ディスク" + str(s.get("disk", {}).get("percent", "?")) + "% / Docker " + str(dk.get("status", "?"))
          + " / 稼働" + str(s.get("uptime", {}).get("days", "?")) + "日 (計測 " + str(s.get("timestamp", "?")) + ")")
else:
    print("mini本体: 未計測")
d = g("docker-resources.json")
if d:
    cs = d.get("containers") or []
    print("コンテナ: " + str(len(cs)) + "個 " + " ".join(str(x.get("name", "?")) for x in cs))
else:
    print("コンテナ: 未計測")
o = g("ollama-status.json")
if o:
    print("ollama: API " + str(o.get("api", {}).get("status", "?")) + " / 推論テスト "
          + str(o.get("inference", {}).get("status", "?")))
else:
    print("ollama: 未計測")
i = g("immich-status.json")
if i:
    print("Immich: " + str(i.get("api", {}).get("status", "?")))
else:
    print("Immich: 未計測")
'''

REPORT = '''
import sys, json
try:
    msgs = json.load(sys.stdin)
except Exception:
    print("Discord読み取り失敗 (応答が JSON でない)"); raise SystemExit
if isinstance(msgs, dict):
    print("Discord読み取り失敗: " + str(msgs.get("message", "不明")))
else:
    for m in msgs:
        if "朝のヘルスレポート" in (m.get("content") or ""):
            print("(" + m["timestamp"][:16] + " 投稿)")
            print(m["content"][:1200])
            break
    else:
        print("直近20件に朝レポートが見つかりません")
'''

CHANNEL = "<bulletin-board-channel-id>"
CURL = (f'source ~/.secrets/.env; curl -s --max-time 3 '
        f'"https://discord.com/api/v10/channels/{CHANNEL}/messages?limit=20" '
        f'-H "Authorization: Bot ${{DISCORD_BOT_TOKEN}}"')

entries = [
    {"name": "mini 健康サマリ (毎時計測の最新値)",
     "cmd": SSH + shlex.quote("python3 -c " + shlex.quote(SUMMARY))
            + " 2>/dev/null || echo '接続不可 (電源/tailnet を確認)'"},
    {"name": "朝レポート (Discord 最新)",
     "cmd": SSH + shlex.quote(CURL + " | python3 -c " + shlex.quote(REPORT))
            + " 2>/dev/null || echo '接続不可 (電源/tailnet を確認)'"},
]

data = json.loads(LEDGER.read_text())
data["topics"]["health"] = entries
LEDGER.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
print("done:", LEDGER)
```

Run: `python3 <scratchpad>/update_ledger.py`
Expected: `done: .../sensor_targets.json`

- [ ] **Step 3: 実測スモーク (コード + 台帳の end-to-end)**

```bash
cd ~/sandbox/menubar-hisho/core && uv run python -c "
from pathlib import Path
from hisho_core import sensors
print(sensors.measure('health', Path.home() / 'Library' / 'Application Support' / 'Hisho'))
"
```

Expected: 「HH:MM 実測」ヘッダ +【mini 健康サマリ】に メモリ%/CPU/Docker 行、【朝レポート】に「🟢 朝のヘルスレポート」本文。「実測失敗」「接続不可」が無いこと。

- [ ] **Step 4: all topic でも壊れないことを確認**

```bash
cd ~/sandbox/menubar-hisho/core && uv run python -c "
from pathlib import Path
from hisho_core import sensors
r = sensors.measure('all', Path.home() / 'Library' / 'Application Support' / 'Hisho')
print(r[:400]); print('...'); print('健康サマリ' in r and '朝レポート' in r)
"
```

Expected: 末尾に `True` (12秒デッドライン内に全項目が返る)

---

### Task 4: 配備 (実機 Hisho へ反映) + 本番スモーク

**Files:**
- Modify (コピー先): `build/derived/Build/Products/Debug/Hisho.app/Contents/Resources/core/python/lib/python3.13/site-packages/hisho_core/{sensors,server}.py`

**Interfaces:**
- Consumes: Task 1-3 の全成果
- Produces: 実機メニューバー Hisho (port 51100) が health topic に応答

- [ ] **Step 1: 変更2ファイルをバンドルへコピー** — pure-python 2ファイルの変更なので full rebuild (build_core.sh + xcodebuild) は不要。site-packages へ直接反映:

```bash
B=~/sandbox/menubar-hisho/build/derived/Build/Products/Debug/Hisho.app/Contents/Resources/core/python/lib/python3.13/site-packages/hisho_core
cp ~/sandbox/menubar-hisho/core/hisho_core/sensors.py "$B/sensors.py"
cp ~/sandbox/menubar-hisho/core/hisho_core/server.py "$B/server.py"
ls -la "$B/sensors.py" "$B/server.py"
```

- [ ] **Step 2: Hisho を再起動**

```bash
pkill -x Hisho; sleep 2
open ~/sandbox/menubar-hisho/build/derived/Build/Products/Debug/Hisho.app
sleep 5
curl -s --max-time 5 http://127.0.0.1:51100/healthz || curl -s --max-time 5 http://127.0.0.1:51100/
```

Expected: core が応答 (エンドポイントは 200 系。無ければ次 step の chat で確認)

- [ ] **Step 3: 本番スモーク — 実際に聞く**

```bash
curl -s --max-time 120 -X POST http://127.0.0.1:51100/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"messages": [{"role": "user", "content": "朝レポート見せて"}], "session_id": "smoke-health"}'
```

Expected: 応答本文に朝レポート由来の内容 (「ヘルスレポート」等) が含まれる。sensor 注入は決定的なので、LLM の言い回しでなく「レポートの実データに言及しているか」で判定。

- [ ] **Step 4: Commit + push 判断** (監督が実行。台帳とバンドルはコミット対象外なので、このタスクでの repo 変更は無し — Task 1/2 のコミットが全て)

```bash
cd ~/sandbox/menubar-hisho && git log --oneline -3 && git status --short
```

Expected: 作業ツリーがクリーン (uv.lock 以外)

---

## Self-Review 済みチェック

- spec 全要件 → Task 1 (enum/台帳読込) / Task 2 (ルーティング) / Task 3 (台帳2エントリ・token 非流出) / Task 4 (配備) で網羅
- 「調子」を入れない判断は spec の regex から変更 — 理由: 既存テスト `調子どう? == "all"` が固定挙動 (spec 側もこの計画を正とする)
- placeholder 無し。全 step に実コード/実コマンド/期待値
- 型・名前の一貫性: TOPICS / ledger_items / _TOPIC_PATTERNS / _guess_topic は実コードの実名

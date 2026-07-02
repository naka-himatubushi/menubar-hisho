# Hisho Swift 殻 + パッケージング 実装プラン (Plan 2 / 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
> **実装ポリシー:** plan=Opus / 実装=Sonnet・Haiku サブエージェント。**サブエージェントは commit しない**(step の commit はレビュー通過後に親が実行、または明示許可時のみ)。

**Goal:** 完成済み `hisho_core` (Python) を子プロセスとして抱える SwiftUI メニューバーアプリを作り、同梱 Python ごと relocatable な `.app` に固める。

**Architecture:** ロジックと View は SwiftPM パッケージ **HishoKit** に置き `swift test` で回す(実測 2026-07-02: `import Testing` が CLT に無く、`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer swift test` が必要)。`.app` の組立・Info.plist・署名は **XcodeGen 生成の Xcode プロジェクト**(HishoApp) が担う薄殻。core は `Process` + stdin `Pipe` で spawn し、`/healthz` ポーリング → 純関数 reducer で 5 状態 (starting core / warming model / ready / ollama-down / core-stopped) を導出。チャットは `URLSession.bytes` の SSE を手書きパーサで読む。

**Tech Stack:** Swift 6 (tools 6.0) / SwiftUI / Swift Testing (`import Testing`) / XcodeGen (導入済 `/opt/homebrew/bin/xcodegen`) / uv 管理 python-build-standalone CPython 3.13。SwiftPM 外部依存 **0**。

## Global Constraints

- **`127.0.0.1` のみ**。`0.0.0.0`・外部送信・テレメトリ禁止。子プロセス env に `HF_HUB_OFFLINE=1` `TRANSFORMERS_OFFLINE=1`。
- ポート既定 **51100**。実ポートの真実は `~/Library/Application Support/Hisho/core.json`(core が書く。Swift は pid 照合して読む)。
- **Sandbox OFF・Hardened Runtime OFF・notarize なし**。署名は **ad-hoc** (`CODE_SIGN_IDENTITY: "-"`)。
- 同梱 Python は **python-build-standalone CPython 3.13 ツリー丸ごとコピー**(symlink venv 不可)。deps = `fastapi` + `uvicorn` + `httpx`(core/pyproject.toml の通り)。
- **自動バックオフ再起動しない**。想定外の子終了は「core 停止 — 再起動」を可視化して手動アクション。
- `xcodebuild`/`xcodegen` 実行時は **`DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer` を必ず前置**(この Mac の xcode-select は CLT を指している。`sudo xcode-select -s` はしない)。
- 全 Swift ファイル先頭に `// 役割:` ヘッダコメント。シェルスクリプトも同様に役割コメント。
- 成果物に実名を書かない。UI 文言は日本語。
- 色/フォント/モーションはこのプランでは**確定しない**(実機確認後に別途調整。Task 10 でユーザー目視チェック)。

## core 契約 (実測済 — 2026-07-02 に main HEAD `0e0e4cd` から採取)

Swift 側が消費するインターフェース。**変更禁止(読むだけ)**:

- **起動**: `<python3> -m hisho_core`。bind 成功後に `~/Library/Application Support/Hisho/core.json` へ `{"pid": <int>, "port": <int>}` を書いてから uvicorn 起動(= core.json 出現と healthz 応答の間に短い race あり。ポーリングで吸収)。
- **親死対策は core 実装済**: 子の stdin が EOF になると `os._exit(0)`(`lifecycle.start_stdin_death_watcher`)。**Swift は stdin に Pipe を挿して保持するだけ**(何も書かない)。
- **`GET /healthz`** → `{"core": true, "ollama": {"reachable": bool, "version": str|null}, "model": {"present": bool, "loaded": bool}}`(probe は core 側で ~3 秒キャッシュ)。
- **`POST /v1/chat/completions`** + ヘッダ `X-Hisho-Source: popover` + body `{"session_id": "<任意の安定文字列>", "stream": true, "messages": [{"role":"user","content":"<最新の発話のみ>"}]}`。popover マーカー時は**履歴 replay と persona 合成をサーバがやる**ので、Swift は最新 user メッセージ 1 件だけ送る。`model` 省略で core 既定 (`qwen3.6:35b-a3b`)。
- **SSE 形状**: `data: {chunk}\n\n` 行。初回 chunk は `delta:{"role":"assistant"}`(content なし)、以降 `delta:{"content":"…"}`、最後に `finish_reason` 付き chunk → `data: [DONE]`。**エラー時は `data: {"error":{"message":…,"type":"hisho_error",…}}` を 1 本出して `[DONE]` なしで close**。
- **`GET /history`** / `GET /history?session_id=` — 読み取り専用(MVP UI では未使用。将来の履歴画面 seam)。

---

## Setup (controller が実施 — サブエージェント不要)

- [ ] `docs/HANDOFF.md` を main にコミット(現在 untracked)。
- [ ] ブランチ作成: `git checkout -b feature/plan2-swift-shell`
- [ ] `.gitignore` に追記:

```gitignore
build/
.build/
HishoApp/HishoApp.xcodeproj/
HishoApp/Info.plist
*.swiftpm
DerivedData/
```

(`HishoApp.xcodeproj` と `Info.plist` は XcodeGen 生成物 = コミットしない。`project.yml` が真実。)

- [ ] Xcode ライセンス確認: `DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer xcodebuild -version` が `Xcode 26.x` を出せば OK。license エラーが出たら**ユーザーに** `sudo xcodebuild -license accept` を依頼(唯一の sudo ポイント)。

---

### Task 1: macOS 26 focus スパイク (30 分タイムボックス・使い捨て)

MenuBarExtra `.window` の TextField が「クリック→即入力」でキーフォーカスを得るかの実機確認。**結果が Task 10 のホスト実装(Variant A/B)を決める。**

**Files:**
- Create: `spikes/focus-spike/Package.swift`
- Create: `spikes/focus-spike/Sources/FocusSpike/main.swift` ではなく `FocusSpikeApp.swift`(`@main` 属性を使うため `main.swift` 禁止)

**Interfaces:** なし(使い捨て。ただし判定結果を本ファイル末尾の「スパイク結果」欄と SDD ledger に記録)

- [ ] **Step 1: パッケージ骨組み**

`spikes/focus-spike/Package.swift`:

```swift
// swift-tools-version: 6.0
// 役割: MenuBarExtra .window の TextField focus 挙動を macOS 26 実機で確かめる使い捨てスパイク。
import PackageDescription

let package = Package(
    name: "FocusSpike",
    platforms: [.macOS(.v15)],
    targets: [.executableTarget(name: "FocusSpike")]
)
```

- [ ] **Step 2: スパイク本体**

`spikes/focus-spike/Sources/FocusSpike/FocusSpikeApp.swift`:

```swift
// 役割: メニューバーアイコン→popover→TextField の focus 獲得/維持を目視確認する検証アプリ。
import SwiftUI

@main
struct FocusSpikeApp: App {
    var body: some Scene {
        MenuBarExtra("FocusSpike", systemImage: "keyboard") {
            SpikeView()
        }
        .menuBarExtraStyle(.window)
    }
}

struct SpikeView: View {
    @State private var input = ""
    @State private var log: [String] = []
    @FocusState private var focused: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(log.enumerated()), id: \.offset) { _, line in
                Text(line).font(.caption).monospaced()
            }
            TextField("開いた直後にそのまま打てる?", text: $input)
                .textFieldStyle(.roundedBorder)
                .focused($focused)
                .onSubmit {
                    log.append("送信: \(input)")
                    input = ""
                    // token 到着による再描画を疑似 — 2 秒後に focus が残っているか
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) {
                        log.append("2秒後 focus=\(focused)")
                    }
                }
        }
        .padding()
        .frame(width: 320, height: 220)
        .onAppear { focused = true }
    }
}
```

- [ ] **Step 3: ビルド & 起動**

Run: `cd spikes/focus-spike && swift build 2>&1 | tail -2 && .build/debug/FocusSpike & echo "spike pid: $!"`
Expected: ビルド成功、メニューバーに ⌨️ アイコン出現(CLT のみで動く。Xcode 不要)。GUI アプリはフォアグラウンドで待たない(background 起動、終了は pid を kill)。

- [ ] **Step 4: ユーザー実機チェック(AskUserQuestion で採否)**

判定 3 点、**全部 Yes で PASS**:
1. アイコンをクリック → **追加クリックなしで**即タイプでき、文字が field に入る
2. ⏎ 送信後、続けて即タイプできる(focus 維持)
3. 送信 2 秒後のログが `focus=true`

- [ ] **Step 5: 結果記録**

本プラン末尾「スパイク結果」欄に PASS/FAIL と観察メモを書く。FAIL なら Task 10 は Variant B (AppKit host) を使う。`Ctrl-C` でスパイク終了。

- [ ] **Step 6: Commit**

```bash
git add spikes/ && git commit -m "chore(spike): macOS 26 MenuBarExtra focus spike"
```

---

### Task 2: core warm-up / unload (Python — spec §8 の未実装分)

cold load (~23GB モデル) を起動直後に裏で温める。ollama reachable まで待って 1 トークン生成 (`keep_alive` 30m)、graceful 終了時に `keep_alive:0` でアンロード要求。**UI の warming-model 状態はこの warm-up が `/api/ps` に載せる `loaded` を待つ。**

**Files:**
- Modify: `core/hisho_core/llm.py`(末尾に `warmup` / `unload` 追加)
- Modify: `core/hisho_core/server.py`(`create_app` に lifespan + `warmup_when_ready` 追加)
- Test: `core/tests/test_warmup.py`

**Interfaces:**
- Consumes: `llm.chat_stream` と同じ `client_factory` 注入パターン、`config.Config`
- Produces: `llm.warmup(*, model, ollama_host, num_ctx, keep_alive, client_factory=None) -> bool` / `llm.unload(*, model, ollama_host, client_factory=None) -> bool` / `server.warmup_when_ready(probe, warmup, *, attempts=120, interval=2.0, sleep=asyncio.sleep) -> bool`
- **既存 35 テストに触らない**(httpx `ASGITransport` は lifespan を実行しないので影響なし)

- [ ] **Step 1: 失敗するテストを書く**

`core/tests/test_warmup.py`:

```python
"""warm-up/unload: ollama を実際に叩かず、注入 fake で request 形状と待機ロジックを検証。"""
import asyncio
import pytest
from hisho_core import llm
from hisho_core.server import warmup_when_ready


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code


class _FakeClient:
    """httpx.AsyncClient の post/aclose だけ真似て、送られた body を記録する。"""
    def __init__(self):
        self.calls = []

    async def post(self, url, json=None):
        self.calls.append({"url": url, "json": json})
        return _FakeResponse(200)

    async def aclose(self):
        pass


async def test_warmup_posts_single_token_generation():
    fake = _FakeClient()
    ok = await llm.warmup(model="m1", ollama_host="http://127.0.0.1:11434",
                          num_ctx=8192, keep_alive="30m", client_factory=lambda: fake)
    assert ok is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/chat")
    body = call["json"]
    assert body["model"] == "m1"
    assert body["stream"] is False
    assert body["think"] is False
    assert body["keep_alive"] == "30m"
    assert body["options"] == {"num_ctx": 8192, "num_predict": 1}


async def test_unload_posts_keep_alive_zero():
    fake = _FakeClient()
    ok = await llm.unload(model="m1", ollama_host="http://127.0.0.1:11434",
                          client_factory=lambda: fake)
    assert ok is True
    call = fake.calls[0]
    assert call["url"].endswith("/api/generate")
    assert call["json"] == {"model": "m1", "keep_alive": 0}


async def test_warmup_returns_false_on_connect_error():
    class _Boom:
        async def post(self, url, json=None):
            import httpx
            raise httpx.ConnectError("down")
        async def aclose(self):
            pass
    ok = await llm.warmup(model="m1", ollama_host="http://127.0.0.1:1",
                          num_ctx=8192, keep_alive="30m", client_factory=lambda: _Boom())
    assert ok is False


async def test_warmup_when_ready_waits_for_reachable_then_fires_once():
    probes = [{"reachable": False}, {"reachable": False}, {"reachable": True}]
    fired = []
    sleeps = []

    async def probe():
        return probes.pop(0)

    async def warmup():
        fired.append(1)
        return True

    async def fake_sleep(sec):
        sleeps.append(sec)

    ok = await warmup_when_ready(probe, warmup, attempts=10, interval=0.5, sleep=fake_sleep)
    assert ok is True
    assert fired == [1]          # 一度だけ
    assert sleeps == [0.5, 0.5]  # reachable まで 2 回待った


async def test_warmup_when_ready_retries_failed_warmup():
    """warm-up 自体の失敗(ollama 高負荷等)もリトライする。"""
    results = [False, True]
    fired = []

    async def probe():
        return {"reachable": True}

    async def warmup():
        fired.append(1)
        return results.pop(0)

    async def fake_sleep(sec):
        pass

    ok = await warmup_when_ready(probe, warmup, attempts=5, interval=0.1, sleep=fake_sleep)
    assert ok is True
    assert fired == [1, 1]


async def test_warmup_when_ready_gives_up_after_attempts():
    async def probe():
        return {"reachable": False}

    async def warmup():
        raise AssertionError("must not fire")

    async def fake_sleep(sec):
        pass

    ok = await warmup_when_ready(probe, warmup, attempts=3, interval=0.1, sleep=fake_sleep)
    assert ok is False
```

- [ ] **Step 2: 失敗確認**

Run: `core/.venv/bin/python -m pytest core/tests/test_warmup.py -q`
Expected: FAIL — `AttributeError: module 'hisho_core.llm' has no attribute 'warmup'` / `ImportError: cannot import name 'warmup_when_ready'`

- [ ] **Step 3: llm.py に warmup/unload 実装**

`core/hisho_core/llm.py` 末尾に追加:

```python
async def warmup(*, model, ollama_host, num_ctx, keep_alive, client_factory=None) -> bool:
    """1トークン生成でモデルを VRAM にロードし cold start を隠す。失敗は False(例外を上げない)。"""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "stream": False,
        "think": False,
        "keep_alive": keep_alive,
        "options": {"num_ctx": num_ctx, "num_predict": 1},
    }
    factory = client_factory or (lambda: httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=None, write=5.0, pool=5.0)))
    client = factory()
    try:
        r = await client.post(f"{ollama_host}/api/chat", json=body)
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        await client.aclose()


async def unload(*, model, ollama_host, client_factory=None) -> bool:
    """keep_alive:0 で即アンロードを要求(graceful 終了時のベストエフォート)。"""
    factory = client_factory or (lambda: httpx.AsyncClient(timeout=2.0))
    client = factory()
    try:
        r = await client.post(f"{ollama_host}/api/generate",
                              json={"model": model, "keep_alive": 0})
        return r.status_code == 200
    except httpx.HTTPError:
        return False
    finally:
        await client.aclose()
```

- [ ] **Step 4: server.py に lifespan + warmup_when_ready**

`core/hisho_core/server.py` — module レベルに追加(`_default_probe` の下):

```python
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
```

`create_app` を変更 — シグネチャに `warmup_fn=None, unload_fn=None` を追加し、`app = FastAPI()` を lifespan 付きに:

```python
def create_app(
    store: Store, config: Config, *, chat_fn=None, probe_fn=None,
    warmup_fn=None, unload_fn=None,
) -> FastAPI:
    import contextlib
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _lifespan(app: FastAPI):
        task = asyncio.create_task(
            warmup_when_ready(app.state.probe_fn, app.state.warmup_fn))
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        try:
            await asyncio.wait_for(app.state.unload_fn(), timeout=2.0)
        except Exception:
            logger.debug("unload on shutdown failed", exc_info=True)

    app = FastAPI(lifespan=_lifespan)
    ...(既存の app.state.* 設定はそのまま)...
    app.state.warmup_fn = warmup_fn or (lambda: llm.warmup(
        model=config.chat_model, ollama_host=config.ollama_host,
        num_ctx=config.num_ctx, keep_alive=config.keep_alive))
    app.state.unload_fn = unload_fn or (lambda: llm.unload(
        model=config.chat_model, ollama_host=config.ollama_host))
```

(docstring の Args にも 2 引数を追記。)

- [ ] **Step 5: 全テスト green 確認**

Run: `core/.venv/bin/python -m pytest core/tests/ -q`
Expected: `41 passed`(既存 35 + 新 6)

- [ ] **Step 6: Commit**

```bash
git add core/hisho_core/llm.py core/hisho_core/server.py core/tests/test_warmup.py
git commit -m "feat(core): warm-up on startup + keep_alive:0 unload on shutdown (spec §8)"
```

---

### Task 3: HishoKit scaffold + Models + SSEParser

SwiftPM パッケージの器と、SSE 手書きパーサ(純関数)。

**Files:**
- Create: `HishoKit/Package.swift`
- Create: `HishoKit/Sources/HishoKit/Models.swift`
- Create: `HishoKit/Sources/HishoKit/SSEParser.swift`
- Test: `HishoKit/Tests/HishoKitTests/SSEParserTests.swift`

**Interfaces:**
- Produces: `enum SSEEvent { case delta(String); case finish(reason: String); case done; case error(message: String) }` / `struct SSEParser { func parse(line: String) -> SSEEvent? }` / `enum CoreState` / `struct ChatMessage` / `struct HealthSnapshot`

- [ ] **Step 1: Package.swift**

`HishoKit/Package.swift`:

```swift
// swift-tools-version: 6.0
// 役割: Hisho の Swift ロジックと View を持つパッケージ。swift test で単体テスト可能(Xcode 不要)。
import PackageDescription

let package = Package(
    name: "HishoKit",
    platforms: [.macOS(.v15)],
    products: [.library(name: "HishoKit", targets: ["HishoKit"])],
    targets: [
        .target(name: "HishoKit"),
        .testTarget(name: "HishoKitTests", dependencies: ["HishoKit"]),
    ]
)
```

- [ ] **Step 2: Models.swift**

`HishoKit/Sources/HishoKit/Models.swift`:

```swift
// 役割: HishoKit 全体で共有する値型 — core の状態・チャットメッセージ・healthz スナップショット。
import Foundation

/// Swift 殻から見た core の 5 状態(spec §3 の状態表示)。
public enum CoreState: Equatable, Sendable {
    case startingCore
    case warmingModel
    case ready
    case ollamaDown
    case coreStopped(reason: String)
}

/// popover に並ぶ 1 メッセージ。assistant は streaming で生まれ complete/error で確定する。
public struct ChatMessage: Identifiable, Equatable, Sendable {
    public enum Role: String, Sendable { case user, assistant }
    public enum Status: Equatable, Sendable { case streaming, complete, error(String) }

    public let id: UUID
    public let role: Role
    public var text: String
    public var status: Status

    public init(id: UUID = UUID(), role: Role, text: String, status: Status) {
        self.id = id
        self.role = role
        self.text = text
        self.status = status
    }
}

/// /healthz の Swift 側表現。nil(応答なし)は CoreState 導出側で扱う。
public struct HealthSnapshot: Equatable, Sendable {
    public var ollamaReachable: Bool
    public var modelLoaded: Bool

    public init(ollamaReachable: Bool, modelLoaded: Bool) {
        self.ollamaReachable = ollamaReachable
        self.modelLoaded = modelLoaded
    }
}
```

- [ ] **Step 3: 失敗するテストを書く**

`HishoKit/Tests/HishoKitTests/SSEParserTests.swift`:

```swift
// 役割: SSEParser が core の実 SSE 形状(role初回/delta/finish/[DONE]/error frame)を正しく分類するか検証。
import Testing
@testable import HishoKit

@Suite struct SSEParserTests {
    let parser = SSEParser()

    @Test func contentDelta() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"content":"こん"},"finish_reason":null}]}"#
        #expect(parser.parse(line: line) == .delta("こん"))
    }

    @Test func roleOnlyFirstChunkIsIgnored() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}"#
        #expect(parser.parse(line: line) == nil)
    }

    @Test func finishChunk() {
        let line = #"data: {"id":"chatcmpl-1","object":"chat.completion.chunk","created":1,"model":"m","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}"#
        #expect(parser.parse(line: line) == .finish(reason: "stop"))
    }

    @Test func doneSentinel() {
        #expect(parser.parse(line: "data: [DONE]") == .done)
    }

    @Test func errorFrame() {
        let line = #"data: {"error":{"message":"ollama 500: boom","type":"hisho_error","param":null,"code":null}}"#
        #expect(parser.parse(line: line) == .error(message: "ollama 500: boom"))
    }

    @Test func nonDataLinesAreIgnored() {
        #expect(parser.parse(line: "") == nil)
        #expect(parser.parse(line: ": comment") == nil)
        #expect(parser.parse(line: "event: x") == nil)
    }
}
```

- [ ] **Step 4: 失敗確認**

Run: `cd HishoKit && swift test 2>&1 | tail -5`
Expected: コンパイルエラー(`SSEParser` 未定義)

- [ ] **Step 5: SSEParser 実装**

`HishoKit/Sources/HishoKit/SSEParser.swift`:

```swift
// 役割: core の SSE 1 行を SSEEvent に変換する純関数パーサ。OpenAI chunk / error frame / [DONE] を判別。
import Foundation

/// SSE ストリームから出てくる意味のあるイベント。
public enum SSEEvent: Equatable, Sendable {
    case delta(String)
    case finish(reason: String)
    case done
    case error(message: String)
}

public struct SSEParser: Sendable {
    public init() {}

    /// "data: …" 以外の行・role だけの初回 delta・空 delta は nil(呼び手は読み飛ばす)。
    public func parse(line: String) -> SSEEvent? {
        guard line.hasPrefix("data:") else { return nil }
        let payload = line.dropFirst(5).trimmingCharacters(in: .whitespaces)
        if payload == "[DONE]" { return .done }
        guard let data = payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return nil }

        if let err = obj["error"] as? [String: Any] {
            return .error(message: (err["message"] as? String) ?? "unknown error")
        }
        guard let choices = obj["choices"] as? [[String: Any]],
              let first = choices.first
        else { return nil }

        if let delta = first["delta"] as? [String: Any],
           let content = delta["content"] as? String, !content.isEmpty {
            return .delta(content)
        }
        if let reason = first["finish_reason"] as? String {
            return .finish(reason: reason)
        }
        return nil
    }
}
```

- [ ] **Step 6: green 確認**

Run: `cd HishoKit && swift test 2>&1 | tail -3`
Expected: `Test run with 6 tests … passed`

- [ ] **Step 7: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): HishoKit scaffold + models + SSE parser"
```

---

### Task 4: ChatRequestBuilder + CoreChatClient

リクエスト組立(純関数・テスト対象)と、`URLSession.bytes` で SSE を流す薄いトランスポート(結合は Task 11 の実機スモークで担保)。

**Files:**
- Create: `HishoKit/Sources/HishoKit/ChatClient.swift`
- Test: `HishoKit/Tests/HishoKitTests/ChatRequestBuilderTests.swift`

**Interfaces:**
- Consumes: `SSEParser`, `SSEEvent` (Task 3)
- Produces: `struct ChatRequestBuilder { init(port: Int); func makeRequest(sessionID: String, userMessage: String) throws -> URLRequest }` / `protocol ChatStreaming: Sendable { func stream(sessionID: String, userMessage: String, port: Int) -> AsyncThrowingStream<SSEEvent, Error> }` / `struct CoreChatClient: ChatStreaming`

- [ ] **Step 1: 失敗するテストを書く**

`HishoKit/Tests/HishoKitTests/ChatRequestBuilderTests.swift`:

```swift
// 役割: popover マーカー・session_id・「最新 user メッセージ 1 件のみ」という core 契約をリクエストが守るか検証。
import Foundation
import Testing
@testable import HishoKit

@Suite struct ChatRequestBuilderTests {
    @Test func requestFollowsCoreContract() throws {
        let req = try ChatRequestBuilder(port: 51100)
            .makeRequest(sessionID: "sess-abc123", userMessage: "こんにちは")

        #expect(req.url?.absoluteString == "http://127.0.0.1:51100/v1/chat/completions")
        #expect(req.httpMethod == "POST")
        #expect(req.value(forHTTPHeaderField: "X-Hisho-Source") == "popover")
        #expect(req.value(forHTTPHeaderField: "Content-Type") == "application/json")

        let body = try JSONSerialization.jsonObject(with: req.httpBody!) as! [String: Any]
        #expect(body["session_id"] as? String == "sess-abc123")
        #expect(body["stream"] as? Bool == true)
        let msgs = body["messages"] as! [[String: String]]
        #expect(msgs.count == 1)  // 履歴 replay はサーバ側 — 最新 1 件だけ送る
        #expect(msgs[0] == ["role": "user", "content": "こんにちは"])
        #expect(body["model"] == nil)  // core 既定モデルに任せる
    }

    @Test func fallbackPortIsRespected() throws {
        let req = try ChatRequestBuilder(port: 60123)
            .makeRequest(sessionID: "s", userMessage: "x")
        #expect(req.url?.absoluteString.contains("127.0.0.1:60123") == true)
    }
}
```

- [ ] **Step 2: 失敗確認**

Run: `cd HishoKit && swift test 2>&1 | tail -5`
Expected: コンパイルエラー(`ChatRequestBuilder` 未定義)

- [ ] **Step 3: 実装**

`HishoKit/Sources/HishoKit/ChatClient.swift`:

```swift
// 役割: core への chat リクエスト組立と、URLSession.bytes による SSE ストリーム消費。
// ChatStreaming protocol が ChatStore へのテスト用差し込み口(seam)。
import Foundation

/// core 契約に沿った /v1/chat/completions リクエストを組み立てる純関数部。
public struct ChatRequestBuilder: Sendable {
    private let baseURL: URL

    public init(port: Int) {
        self.baseURL = URL(string: "http://127.0.0.1:\(port)")!
    }

    public func makeRequest(sessionID: String, userMessage: String) throws -> URLRequest {
        var req = URLRequest(url: baseURL.appendingPathComponent("v1/chat/completions"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.setValue("popover", forHTTPHeaderField: "X-Hisho-Source")
        req.timeoutInterval = 300  // streaming 中はデータ到着ごとにリセットされる
        let body: [String: Any] = [
            "session_id": sessionID,
            "stream": true,
            "messages": [["role": "user", "content": userMessage]],
        ]
        req.httpBody = try JSONSerialization.data(withJSONObject: body)
        return req
    }
}

/// ChatStore が消費するストリーミング抽象。テストでは Fake、実機では CoreChatClient。
public protocol ChatStreaming: Sendable {
    func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error>
}

/// 実トランスポート。core が error frame 後に [DONE] なしで close する仕様に合わせ、
/// エラーイベント後は素直にストリームを終える。
public struct CoreChatClient: ChatStreaming {
    public init() {}

    public func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let req = try ChatRequestBuilder(port: port)
                        .makeRequest(sessionID: sessionID, userMessage: userMessage)
                    let (bytes, response) = try await URLSession.shared.bytes(for: req)
                    guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
                        throw URLError(.badServerResponse)
                    }
                    let parser = SSEParser()
                    for try await line in bytes.lines {
                        guard let event = parser.parse(line: line) else { continue }
                        continuation.yield(event)
                        if case .error = event { break }
                        if case .done = event { break }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
```

- [ ] **Step 4: green 確認**

Run: `cd HishoKit && swift test 2>&1 | tail -3`
Expected: `8 tests … passed`

- [ ] **Step 5: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): chat request builder + SSE streaming client"
```

---

### Task 5: ChatStore (アプリ層ストリーム保持)

会話状態の唯一の持ち主。**popover の View でなくアプリ層に住む**ので、popover が破棄されても stream は切れない(spec §3)。

**Files:**
- Create: `HishoKit/Sources/HishoKit/ChatStore.swift`
- Test: `HishoKit/Tests/HishoKitTests/ChatStoreTests.swift`

**Interfaces:**
- Consumes: `ChatStreaming` (Task 4), `ChatMessage`/`SSEEvent` (Task 3)
- Produces: `@MainActor @Observable final class ChatStore { init(client: any ChatStreaming); var messages: [ChatMessage]; var isStreaming: Bool; let sessionID: String; func send(_ text: String, port: Int); func cancel(); func awaitStreamEnd() async }`

- [ ] **Step 1: 失敗するテストを書く**

`HishoKit/Tests/HishoKitTests/ChatStoreTests.swift`:

```swift
// 役割: ChatStore のメッセージ状態遷移 — 正常完走 / error frame / 途中切断 / 二重送信ガード。
import Foundation
import Testing
@testable import HishoKit

/// 台本どおりのイベントを流す Fake。finish(throwing:) で異常系も再現。
struct FakeStreamer: ChatStreaming {
    let events: [SSEEvent]
    var thrown: URLError? = nil  // Sendable な具象型に限定 (Swift 6 strict concurrency)

    func stream(sessionID: String, userMessage: String, port: Int)
        -> AsyncThrowingStream<SSEEvent, Error> {
        AsyncThrowingStream { c in
            for e in events { c.yield(e) }
            c.finish(throwing: thrown)
        }
    }
}

@Suite @MainActor struct ChatStoreTests {
    @Test func happyPathAccumulatesDeltasAndCompletes() async {
        let store = ChatStore(client: FakeStreamer(
            events: [.delta("こん"), .delta("にちは"), .finish(reason: "stop"), .done]))
        store.send("やあ", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages.count == 2)
        #expect(store.messages[0].role == .user)
        #expect(store.messages[0].text == "やあ")
        #expect(store.messages[1].role == .assistant)
        #expect(store.messages[1].text == "こんにちは")
        #expect(store.messages[1].status == .complete)
        #expect(store.isStreaming == false)
    }

    @Test func errorFrameMarksAssistantError() async {
        let store = ChatStore(client: FakeStreamer(
            events: [.delta("途中"), .error(message: "ollama 500: boom")]))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages[1].text == "途中")  // partial 内容は残す
        #expect(store.messages[1].status == .error("ollama 500: boom"))
    }

    @Test func abruptCloseWithoutDoneIsError() async {
        // core 死亡などで [DONE] なしに stream が終わったケース
        let store = ChatStore(client: FakeStreamer(events: [.delta("とち")]))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        #expect(store.messages[1].status == .error("応答が途中で切れました"))
    }

    @Test func transportErrorIsSurfaced() async {
        let store = ChatStore(client: FakeStreamer(
            events: [], thrown: URLError(.cannotConnectToHost)))
        store.send("q", port: 51100)
        await store.awaitStreamEnd()

        if case .error = store.messages[1].status {} else {
            Issue.record("expected .error, got \(store.messages[1].status)")
        }
    }

    @Test func emptyAndDuplicateSendsAreIgnored() async {
        let store = ChatStore(client: FakeStreamer(events: [.done]))
        store.send("   ", port: 51100)
        #expect(store.messages.isEmpty)

        store.send("a", port: 51100)
        store.send("b", port: 51100)  // streaming 中の二重送信は無視
        await store.awaitStreamEnd()
        #expect(store.messages.filter { $0.role == .user }.count == 1)
    }
}
```

- [ ] **Step 2: 失敗確認**

Run: `cd HishoKit && swift test 2>&1 | tail -5`
Expected: コンパイルエラー(`ChatStore` 未定義)

- [ ] **Step 3: 実装**

`HishoKit/Sources/HishoKit/ChatStore.swift`:

```swift
// 役割: 会話状態の唯一の持ち主(アプリ層)。popover 破棄でも stream を保持し続ける。
// session_id はアプリ起動ごとに 1 つ生成し core に渡す(履歴 replay はサーバ側)。
import Foundation
import Observation

@MainActor
@Observable
public final class ChatStore {
    public private(set) var messages: [ChatMessage] = []
    public let sessionID: String
    public var isStreaming: Bool { streamTask != nil }

    private let client: any ChatStreaming
    private var streamTask: Task<Void, Never>?

    public init(client: any ChatStreaming) {
        self.client = client
        self.sessionID = "sess-" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased()
    }

    public func send(_ text: String, port: Int) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, streamTask == nil else { return }

        messages.append(ChatMessage(role: .user, text: trimmed, status: .complete))
        let assistantID = UUID()
        messages.append(ChatMessage(id: assistantID, role: .assistant, text: "", status: .streaming))

        streamTask = Task { [weak self] in
            guard let self else { return }
            var finished = false
            do {
                for try await event in self.client.stream(
                    sessionID: self.sessionID, userMessage: trimmed, port: port) {
                    switch event {
                    case .delta(let piece):
                        self.update(assistantID) { $0.text += piece }
                    case .finish:
                        break  // 完了確定は .done で行う
                    case .done:
                        finished = true
                        self.update(assistantID) { $0.status = .complete }
                    case .error(let message):
                        finished = true
                        self.update(assistantID) { $0.status = .error(message) }
                    }
                }
                if !finished {
                    self.update(assistantID) { $0.status = .error("応答が途中で切れました") }
                }
            } catch is CancellationError {
                self.update(assistantID) { $0.status = .error("キャンセルしました") }
            } catch {
                self.update(assistantID) { $0.status = .error(error.localizedDescription) }
            }
            self.streamTask = nil
        }
    }

    public func cancel() {
        streamTask?.cancel()
    }

    /// テストと graceful 終了用: 進行中 stream の完了を待つ。
    public func awaitStreamEnd() async {
        await streamTask?.value
    }

    private func update(_ id: UUID, _ mutate: (inout ChatMessage) -> Void) {
        guard let i = messages.firstIndex(where: { $0.id == id }) else { return }
        mutate(&messages[i])
    }
}
```

- [ ] **Step 4: green 確認**

Run: `cd HishoKit && swift test 2>&1 | tail -3`
Expected: `13 tests … passed`

- [ ] **Step 5: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): ChatStore — app-layer stream ownership + state transitions"
```

---

### Task 6: CoreStateReducer + HTTPHealthProber

状態導出は**純関数**に隔離してテスト、HTTP プローブは薄い実装。

**Files:**
- Create: `HishoKit/Sources/HishoKit/CoreHealth.swift`
- Test: `HishoKit/Tests/HishoKitTests/CoreStateReducerTests.swift`

**Interfaces:**
- Consumes: `CoreState`/`HealthSnapshot` (Task 3)
- Produces: `protocol HealthProbing: Sendable { func probe(port: Int) async -> HealthSnapshot? }` / `struct HTTPHealthProber: HealthProbing` / `enum CoreStateReducer { static func derive(processRunning: Bool, health: HealthSnapshot?, secondsSinceLaunch: Double, startupTimeout: Double) -> CoreState }`

- [ ] **Step 1: 失敗するテストを書く**

`HishoKit/Tests/HishoKitTests/CoreStateReducerTests.swift`:

```swift
// 役割: 5 状態導出の全分岐 — プロセス死/応答なし(タイムアウト前後)/ollama down/warming/ready。
import Testing
@testable import HishoKit

@Suite struct CoreStateReducerTests {
    @Test func processDeadIsCoreStopped() {
        let s = CoreStateReducer.derive(processRunning: false, health: nil,
                                        secondsSinceLaunch: 1, startupTimeout: 20)
        #expect(s == .coreStopped(reason: "core プロセスが終了"))
    }

    @Test func noHealthBeforeTimeoutIsStarting() {
        let s = CoreStateReducer.derive(processRunning: true, health: nil,
                                        secondsSinceLaunch: 5, startupTimeout: 20)
        #expect(s == .startingCore)
    }

    @Test func noHealthAfterTimeoutIsStopped() {
        let s = CoreStateReducer.derive(processRunning: true, health: nil,
                                        secondsSinceLaunch: 21, startupTimeout: 20)
        #expect(s == .coreStopped(reason: "起動タイムアウト"))
    }

    @Test func ollamaUnreachableIsOllamaDown() {
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: false, modelLoaded: false),
            secondsSinceLaunch: 5, startupTimeout: 20)
        #expect(s == .ollamaDown)
    }

    @Test func reachableButNotLoadedIsWarming() {
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: true, modelLoaded: false),
            secondsSinceLaunch: 5, startupTimeout: 20)
        #expect(s == .warmingModel)
    }

    @Test func loadedIsReady() {
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: true, modelLoaded: true),
            secondsSinceLaunch: 5, startupTimeout: 20)
        #expect(s == .ready)
    }
}
```

- [ ] **Step 2: 失敗確認**

Run: `cd HishoKit && swift test 2>&1 | tail -5`
Expected: コンパイルエラー(`CoreStateReducer` 未定義)

- [ ] **Step 3: 実装**

`HishoKit/Sources/HishoKit/CoreHealth.swift`:

```swift
// 役割: /healthz の取得(HealthProbing)と、観測事実→CoreState の純関数導出(CoreStateReducer)。
import Foundation

public protocol HealthProbing: Sendable {
    /// nil = core が HTTP に応答しない(未起動/起動中/死亡)。
    func probe(port: Int) async -> HealthSnapshot?
}

/// 実装: GET /healthz を 2 秒タイムアウトで叩き、core:true の応答だけ採用。
public struct HTTPHealthProber: HealthProbing {
    public init() {}

    public func probe(port: Int) async -> HealthSnapshot? {
        guard let url = URL(string: "http://127.0.0.1:\(port)/healthz") else { return nil }
        var req = URLRequest(url: url)
        req.timeoutInterval = 2
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              (resp as? HTTPURLResponse)?.statusCode == 200,
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              obj["core"] as? Bool == true
        else { return nil }
        let ollama = obj["ollama"] as? [String: Any]
        let model = obj["model"] as? [String: Any]
        return HealthSnapshot(
            ollamaReachable: ollama?["reachable"] as? Bool ?? false,
            modelLoaded: model?["loaded"] as? Bool ?? false)
    }
}

/// 観測事実(プロセス生死・healthz・経過秒)から表示状態を導く。I/O なし。
public enum CoreStateReducer {
    public static func derive(processRunning: Bool, health: HealthSnapshot?,
                              secondsSinceLaunch: Double, startupTimeout: Double) -> CoreState {
        guard processRunning else { return .coreStopped(reason: "core プロセスが終了") }
        guard let health else {
            return secondsSinceLaunch > startupTimeout
                ? .coreStopped(reason: "起動タイムアウト")
                : .startingCore
        }
        guard health.ollamaReachable else { return .ollamaDown }
        return health.modelLoaded ? .ready : .warmingModel
    }
}
```

- [ ] **Step 4: green 確認**

Run: `cd HishoKit && swift test 2>&1 | tail -3`
Expected: `19 tests … passed`

- [ ] **Step 5: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): health prober + pure core-state reducer"
```

---### Task 7: CoreProcessManager (子プロセス供給)

spawn / stdin-Pipe 保持 / core.json ポート発見 / stale 殺し / ログ / SIGTERM 終了 / 手動再起動。プロセステストは `/bin/cat` を子に使う(stdin EOF で死ぬ性質が core と同じ)。

**Files:**
- Create: `HishoKit/Sources/HishoKit/CoreProcessManager.swift`
- Test: `HishoKit/Tests/HishoKitTests/CoreProcessManagerTests.swift`

**Interfaces:**
- Consumes: `HealthProbing`/`CoreStateReducer` (Task 6), `CoreState` (Task 3)
- Produces: `struct CoreLaunchConfig { init(pythonURL:arguments:appSupportDir:defaultPort:startupTimeout:pollInterval:) }` / `@MainActor @Observable final class CoreProcessManager { init(config: CoreLaunchConfig, prober: any HealthProbing); var state: CoreState; var port: Int; func start(); func stop(); func restart() }`

- [ ] **Step 1: 失敗するテストを書く**

`HishoKit/Tests/HishoKitTests/CoreProcessManagerTests.swift`:

```swift
// 役割: CoreProcessManager のプロセス供給を /bin/cat + FakeProber で検証 —
// 起動/状態遷移/予期せぬ死の検出/graceful 停止/stale core 殺し。
import Foundation
import Testing
@testable import HishoKit

/// 差し替え可能な healthz 応答。
final class FakeProber: HealthProbing, @unchecked Sendable {
    private let lock = NSLock()
    private var _result: HealthSnapshot?
    var result: HealthSnapshot? {
        get { lock.withLock { _result } }
        set { lock.withLock { _result = newValue } }
    }
    init(_ result: HealthSnapshot? = nil) { self._result = result }
    func probe(port: Int) async -> HealthSnapshot? { result }
}

/// 条件成立まで最大 timeout ポーリング(MainActor 上で評価 — closure を跨がせない)。
@MainActor
func eventually(timeout: Duration = .seconds(5),
                _ condition: () -> Bool) async -> Bool {
    let deadline = ContinuousClock.now + timeout
    while ContinuousClock.now < deadline {
        if condition() { return true }
        try? await Task.sleep(for: .milliseconds(50))
    }
    return condition()
}

func testConfig(dir: URL, timeout: Double = 30) -> CoreLaunchConfig {
    CoreLaunchConfig(
        pythonURL: URL(filePath: "/bin/cat"),
        arguments: [],                       // cat は stdin を待ち続け EOF で終了 = core と同じ寿命
        appSupportDir: dir,
        defaultPort: 59999,
        startupTimeout: timeout,
        pollInterval: .milliseconds(50))
}

func tempDir() throws -> URL {
    let url = FileManager.default.temporaryDirectory
        .appendingPathComponent("hisho-test-\(UUID().uuidString)")
    try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
    return url
}

@Suite struct CoreProcessManagerTests {
    @Test @MainActor func reachesReadyWhenHealthGreen() async throws {
        let prober = FakeProber(HealthSnapshot(ollamaReachable: true, modelLoaded: true))
        let mgr = CoreProcessManager(config: testConfig(dir: try tempDir()), prober: prober)
        mgr.start()
        #expect(await eventually { mgr.state == .ready })
        mgr.stop()
    }

    @Test @MainActor func walksWarmingThenReady() async throws {
        let prober = FakeProber(HealthSnapshot(ollamaReachable: true, modelLoaded: false))
        let mgr = CoreProcessManager(config: testConfig(dir: try tempDir()), prober: prober)
        mgr.start()
        #expect(await eventually { mgr.state == .warmingModel })
        prober.result = HealthSnapshot(ollamaReachable: true, modelLoaded: true)
        #expect(await eventually { mgr.state == .ready })
        mgr.stop()
    }

    @Test @MainActor func ollamaDownIsSurfaced() async throws {
        let prober = FakeProber(HealthSnapshot(ollamaReachable: false, modelLoaded: false))
        let mgr = CoreProcessManager(config: testConfig(dir: try tempDir()), prober: prober)
        mgr.start()
        #expect(await eventually { mgr.state == .ollamaDown })
        mgr.stop()
    }

    @Test @MainActor func startupTimeoutStops() async throws {
        let mgr = CoreProcessManager(
            config: testConfig(dir: try tempDir(), timeout: 0.2),
            prober: FakeProber(nil))
        mgr.start()
        #expect(await eventually { mgr.state == .coreStopped(reason: "起動タイムアウト") })
    }

    @Test @MainActor func externalKillIsDetectedAsUnexpected() async throws {
        let prober = FakeProber(HealthSnapshot(ollamaReachable: true, modelLoaded: true))
        let mgr = CoreProcessManager(config: testConfig(dir: try tempDir()), prober: prober)
        mgr.start()
        #expect(await eventually { mgr.state == .ready })
        kill(mgr.childPIDForTesting!, SIGKILL)
        #expect(await eventually {
            if case .coreStopped(let r) = mgr.state { return r.contains("予期せず") }
            return false
        })
    }

    @Test @MainActor func gracefulStopKillsChildQuietly() async throws {
        let prober = FakeProber(HealthSnapshot(ollamaReachable: true, modelLoaded: true))
        let mgr = CoreProcessManager(config: testConfig(dir: try tempDir()), prober: prober)
        mgr.start()
        #expect(await eventually { mgr.state == .ready })
        let pid = mgr.childPIDForTesting!
        mgr.stop()
        #expect(mgr.state == .coreStopped(reason: "停止済"))
        #expect(await eventually { kill(pid, 0) != 0 })  // 子は消えている
    }

    @Test @MainActor func staleCoreFromCoreJSONIsKilledBeforeSpawn() async throws {
        let dir = try tempDir()
        // 「前回の core」を偽装: 生きてる /bin/cat を立て、その pid を core.json に書く
        let stale = Process()
        stale.executableURL = URL(filePath: "/bin/cat")
        stale.standardInput = Pipe()
        try stale.run()
        let cj = ["pid": Int(stale.processIdentifier), "port": 59999]
        try JSONSerialization.data(withJSONObject: cj)
            .write(to: dir.appendingPathComponent("core.json"))

        // prober が応答する = healthz 署名一致 = 我々の stale → SIGKILL される
        let prober = FakeProber(HealthSnapshot(ollamaReachable: true, modelLoaded: true))
        let mgr = CoreProcessManager(config: testConfig(dir: dir), prober: prober)
        mgr.start()
        #expect(await eventually { !stale.isRunning })  // Foundation が reap するので isRunning で観測
        mgr.stop()
    }
}
```

- [ ] **Step 2: 失敗確認**

Run: `cd HishoKit && swift test 2>&1 | tail -5`
Expected: コンパイルエラー(`CoreProcessManager` 未定義)

- [ ] **Step 3: 実装**

`HishoKit/Sources/HishoKit/CoreProcessManager.swift`:

```swift
// 役割: hisho_core 子プロセスの唯一の供給者 — spawn(stdin Pipe 保持)・core.json ポート発見・
// stale core 殺し・healthz ポーリング→状態導出・SIGTERM 終了・手動再起動。
// 自動バックオフ再起動はしない(spec §6: crash-loop 回避)。
import Darwin
import Foundation
import Observation

/// 起動パラメータ。テストでは /bin/cat + 短い timeout を注入する。
public struct CoreLaunchConfig: Sendable {
    public var pythonURL: URL
    public var arguments: [String]
    public var appSupportDir: URL
    public var defaultPort: Int
    public var startupTimeout: Double
    public var pollInterval: Duration

    public init(
        pythonURL: URL,
        arguments: [String] = ["-m", "hisho_core"],
        appSupportDir: URL = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("Hisho"),
        defaultPort: Int = 51100,
        startupTimeout: Double = 20,
        pollInterval: Duration = .seconds(1)
    ) {
        self.pythonURL = pythonURL
        self.arguments = arguments
        self.appSupportDir = appSupportDir
        self.defaultPort = defaultPort
        self.startupTimeout = startupTimeout
        self.pollInterval = pollInterval
    }
}

@MainActor
@Observable
public final class CoreProcessManager {
    public private(set) var state: CoreState = .coreStopped(reason: "未起動")
    public private(set) var port: Int

    private let config: CoreLaunchConfig
    private let prober: any HealthProbing
    private var process: Process?
    private var stdinPipe: Pipe?  // 保持するだけ。落とすと EOF→core 自死(それが狙いの保険)
    private var launchedAt: ContinuousClock.Instant?
    private var pollTask: Task<Void, Never>?
    private var stopping = false

    /// テスト専用: 子 pid の覗き穴。
    var childPIDForTesting: pid_t? { process?.processIdentifier }

    public init(config: CoreLaunchConfig, prober: any HealthProbing = HTTPHealthProber()) {
        self.config = config
        self.prober = prober
        self.port = config.defaultPort
    }

    public func start() {
        guard pollTask == nil else { return }
        stopping = false
        state = .startingCore
        pollTask = Task { [weak self] in await self?.runLoop() }
    }

    /// SIGTERM → 最大 gracePeriod 待つ(core の graceful shutdown = ollama unload を走らせる)→
    /// まだ生きていれば stdin pipe 解放(EOF 自死) + SIGKILL。
    /// 注意: stdinPipe を SIGTERM より先に手放すと EOF 自死が graceful shutdown に勝って
    /// unload が走らない(Codex Critical 指摘)。解放は必ず「待った後」。
    /// 呼び所はアプリ終了時とテストのみ — 最大 gracePeriod ブロックを許容する。
    public func stop(gracePeriod: TimeInterval = 3.0) {
        stopping = true
        pollTask?.cancel()
        pollTask = nil
        if let p = process, p.isRunning {
            p.terminate()  // SIGTERM
            let deadline = Date().addingTimeInterval(gracePeriod)
            while p.isRunning && Date() < deadline {
                usleep(50_000)  // 50ms
            }
            if p.isRunning {
                stdinPipe = nil
                kill(p.processIdentifier, SIGKILL)
                p.waitUntilExit()
            }
        }
        process = nil
        stdinPipe = nil
        state = .coreStopped(reason: "停止済")
    }

    public func restart() {
        stop()
        start()
    }

    // MARK: - internals

    private func runLoop() async {
        await killStaleCoreIfAny()
        do {
            try spawn()
        } catch {
            state = .coreStopped(reason: "起動失敗: \(error.localizedDescription)")
            pollTask = nil
            return
        }
        while !Task.isCancelled {
            await tick()
            try? await Task.sleep(for: config.pollInterval)
        }
    }

    /// spec §6: core.json の pid が生存し、そのポートの /healthz が応答(=我々の署名)なら
    /// 前回の孤児と断定して SIGKILL。応答しない未知 pid は殺さない(bind fallback が吸収)。
    private func killStaleCoreIfAny() async {
        guard let cj = readCoreJSON(), cj.pid > 0, kill(cj.pid, 0) == 0 else { return }
        guard await prober.probe(port: cj.port) != nil else { return }
        kill(cj.pid, SIGKILL)
        try? await Task.sleep(for: .milliseconds(300))  // ポート解放待ち
    }

    private func spawn() throws {
        let p = Process()
        p.executableURL = config.pythonURL
        p.arguments = config.arguments
        var env = ProcessInfo.processInfo.environment
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"
        p.environment = env

        let pipe = Pipe()
        p.standardInput = pipe  // 書かない。親死→カーネルが閉じる→EOF→core 自死
        let log = try logFileHandle()
        p.standardOutput = log
        p.standardError = log

        p.terminationHandler = { [weak self] proc in
            let status = proc.terminationStatus
            Task { @MainActor [weak self] in
                guard let self, !self.stopping else { return }
                self.state = .coreStopped(reason: "core が予期せず終了 (exit \(status))")
                self.process = nil
                self.pollTask?.cancel()
                self.pollTask = nil
            }
        }

        try p.run()
        process = p
        stdinPipe = pipe
        launchedAt = ContinuousClock.now
    }

    private func tick() async {
        guard let p = process else { return }
        if let cj = readCoreJSON(), cj.pid == p.processIdentifier {
            port = cj.port  // :0 fallback 時はここで実ポートに追随
        }
        let health = await prober.probe(port: port)
        let elapsed = launchedAt.map {
            let c = (ContinuousClock.now - $0).components
            return Double(c.seconds) + Double(c.attoseconds) * 1e-18  // 小数秒を捨てない
        } ?? 0
        let next = CoreStateReducer.derive(
            processRunning: p.isRunning, health: health,
            secondsSinceLaunch: elapsed, startupTimeout: config.startupTimeout)
        if case .coreStopped(let reason) = next {
            stopping = true                    // 先にセット: terminationHandler の「予期せず」上書きを抑止
            if p.isRunning { p.terminate() }  // 起動タイムアウトで固まった子を回収
            process = nil
            stdinPipe = nil
            pollTask?.cancel()
            pollTask = nil
            state = .coreStopped(reason: reason)
            return
        }
        state = next
    }

    private struct CoreJSON: Decodable {
        let pid: Int32
        let port: Int
    }

    private func readCoreJSON() -> CoreJSON? {
        let url = config.appSupportDir.appendingPathComponent("core.json")
        guard let data = try? Data(contentsOf: url) else { return nil }
        return try? JSONDecoder().decode(CoreJSON.self, from: data)
    }

    /// stdout/stderr 集約先。5MB 超で core.log → core.log.1 に単純ローテート。
    private func logFileHandle() throws -> FileHandle {
        let dir = config.appSupportDir.appendingPathComponent("logs")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let url = dir.appendingPathComponent("core.log")
        if let size = try? FileManager.default
            .attributesOfItem(atPath: url.path)[.size] as? Int, size > 5_000_000 {
            let old = dir.appendingPathComponent("core.log.1")
            try? FileManager.default.removeItem(at: old)
            try? FileManager.default.moveItem(at: url, to: old)
        }
        if !FileManager.default.fileExists(atPath: url.path) {
            FileManager.default.createFile(atPath: url.path, contents: nil)
        }
        let handle = try FileHandle(forWritingTo: url)
        try handle.seekToEnd()
        return handle
    }
}
```

- [ ] **Step 4: green 確認**

Run: `cd HishoKit && swift test 2>&1 | tail -3`
Expected: `26 tests … passed`(タイミング依存 flake が出たら `eventually` の timeout を伸ばす方向で直す。sleep 直書き加算は禁止)

- [ ] **Step 5: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): CoreProcessManager — spawn/stale-kill/health-poll/terminate"
```

---

### Task 8: ChatView + StatusBanner (host-agnostic UI)

MenuBarExtra/NSPopover **どちらのホストにも挿せる** SwiftUI。見た目の作り込みはしない(実機確認後に別途)。

**Files:**
- Create: `HishoKit/Sources/HishoKit/ChatView.swift`

**Interfaces:**
- Consumes: `ChatStore` (Task 5), `CoreProcessManager` (Task 7) — どちらも `@Observable` なので SwiftUI が自動追跡
- Produces: `public struct ChatView: View { init(chat: ChatStore, core: CoreProcessManager) }`

- [ ] **Step 1: 実装**(純 View — 単体テストなし。コンパイル + Task 10 実機確認で担保)

`HishoKit/Sources/HishoKit/ChatView.swift`:

```swift
// 役割: popover の中身 — 状態バナー + 会話ログ(逐次描画・自動スクロール) + 入力欄。
// ホスト(MenuBarExtra / NSPopover)非依存。状態は外から渡された @Observable を描くだけ。
import SwiftUI

public struct ChatView: View {
    private let chat: ChatStore
    private let core: CoreProcessManager
    @State private var input = ""
    @FocusState private var inputFocused: Bool

    public init(chat: ChatStore, core: CoreProcessManager) {
        self.chat = chat
        self.core = core
    }

    public var body: some View {
        VStack(spacing: 0) {
            StatusBanner(state: core.state) { core.restart() }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        ForEach(chat.messages) { message in
                            MessageRow(message: message).id(message.id)
                        }
                    }
                    .padding(8)
                }
                .onChange(of: chat.messages.last?.text) {
                    if let last = chat.messages.last {
                        proxy.scrollTo(last.id, anchor: .bottom)
                    }
                }
            }
            Divider()
            HStack(spacing: 8) {
                TextField("メッセージを入力…", text: $input)
                    .textFieldStyle(.roundedBorder)
                    .focused($inputFocused)
                    .onSubmit(send)
                    .disabled(!canSend)
                Button("送信", action: send)
                    .disabled(!canSend || input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            }
            .padding(8)
        }
        .frame(width: 360, height: 480)
        .onAppear { inputFocused = true }
    }

    private var canSend: Bool {
        core.state == .ready && !chat.isStreaming
    }

    private func send() {
        chat.send(input, port: core.port)
        input = ""
        inputFocused = true
    }
}

/// ready 以外のときだけ出る一行バナー。core 停止時は手動再起動ボタン(自動再起動はしない)。
struct StatusBanner: View {
    let state: CoreState
    let onRestart: () -> Void

    var body: some View {
        switch state {
        case .ready:
            EmptyView()
        case .startingCore:
            banner("秘書を起動中…", tint: .secondary)
        case .warmingModel:
            banner("モデルを準備中…(初回は数十秒かかります)", tint: .secondary)
        case .ollamaDown:
            banner("ollama に接続できません — `ollama serve` を確認", tint: .orange)
        case .coreStopped(let reason):
            HStack {
                Text("core 停止: \(reason)").font(.caption).foregroundStyle(.red)
                Spacer()
                Button("再起動", action: onRestart).font(.caption)
            }
            .padding(6)
            .background(.red.opacity(0.08))
        }
    }

    private func banner(_ text: String, tint: Color) -> some View {
        HStack {
            ProgressView().controlSize(.small)
            Text(text).font(.caption).foregroundStyle(tint)
            Spacer()
        }
        .padding(6)
        .background(.quaternary.opacity(0.4))
    }
}

/// 1 メッセージの行。user は右寄せ、assistant は左寄せ + streaming/エラー表示。
struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 40) }
            VStack(alignment: .leading, spacing: 4) {
                Text(message.text.isEmpty && message.status == .streaming ? "…" : message.text)
                    .textSelection(.enabled)
                if case .error(let reason) = message.status {
                    Text("⚠️ \(reason)").font(.caption2).foregroundStyle(.red)
                }
            }
            .padding(8)
            .background(message.role == .user ? Color.accentColor.opacity(0.15)
                                              : Color.gray.opacity(0.12))
            .clipShape(RoundedRectangle(cornerRadius: 8))
            if message.role == .assistant { Spacer(minLength: 40) }
        }
    }
}
```

- [ ] **Step 2: コンパイル + 既存テスト green 確認**

Run: `cd HishoKit && swift build && swift test 2>&1 | tail -3`
Expected: build 成功、`26 tests … passed`

- [ ] **Step 3: Commit**

```bash
git add HishoKit/ && git commit -m "feat(shell): host-agnostic ChatView + status banner"
```

---

### Task 9: build_core.sh (同梱 Python ツリー組立)

python-build-standalone を取得し、hisho_core + deps を焼き込んだ relocatable ツリーを `build/core-dist/python` に作る。**Xcode 不要・単体で検証可能。**

**Files:**
- Create: `scripts/build_core.sh`(chmod +x)

**Interfaces:**
- Produces: `build/core-dist/python/bin/python3 -m hisho_core` が動くツリー(Task 10 の embed が rsync する)

- [ ] **Step 1: スクリプト作成**

`scripts/build_core.sh`:

```bash
#!/usr/bin/env bash
# 役割: uv で python-build-standalone CPython 3.13 を取得し、hisho_core と deps を
#       直接インストールした同梱用ツリーを build/core-dist/python に組み立てる。
#       symlink venv は .app 移動で壊れるため「ツリー丸ごと + 直接 install」(spec §4)。
set -euo pipefail
cd "$(dirname "$0")/.."

PYTHON_SPEC="cpython-3.13"
DIST="build/core-dist"
PY_DIR="$DIST/python"

rm -rf "$DIST"
mkdir -p "$DIST"

echo "==> python-build-standalone 取得 ($PYTHON_SPEC)"
uv python install "$PYTHON_SPEC" --install-dir "$DIST/uv-python"

SRC_TREE=$(find "$DIST/uv-python" -maxdepth 1 -type d -name 'cpython-3.13*' | head -1)
[ -n "$SRC_TREE" ] || { echo "error: python ツリーが見つからない" >&2; exit 1; }
mv "$SRC_TREE" "$PY_DIR"
rm -rf "$DIST/uv-python"

PY="$PY_DIR/bin/python3"
echo "==> hisho_core + deps を直接インストール"
# uv 管理ツリーは EXTERNALLY-MANAGED マーカー付き → 同梱専用の私有ツリーなので明示的に上書き
# (実測 2026-07-02: フラグ無しだと "externally managed" で拒否される)
uv pip install --python "$PY" --break-system-packages ./core

echo "==> import 検証"
"$PY" -c "import hisho_core, fastapi, uvicorn, httpx; print('bundle imports OK')"

echo "==> relocatable 検証(ツリーを移動しても import できるか)"
RELOC="/tmp/hisho-reloc-check-$$"
cp -R "$PY_DIR" "$RELOC"
"$RELOC/bin/python3" -c "import hisho_core; print('relocation OK')"
rm -rf "$RELOC"

du -sh "$PY_DIR"
echo "==> core-dist ready: $PY_DIR"
```

- [ ] **Step 2: 実行**

Run: `chmod +x scripts/build_core.sh && scripts/build_core.sh`
Expected: `bundle imports OK` → `relocation OK` → サイズ表示(~70-120MB)→ `core-dist ready`

- [ ] **Step 3: ツリーから core 起動スモーク**

```bash
SMOKE_DIR=$(mktemp -d)
# tail -f /dev/null | … は必須: stdin を閉じたまま background 起動すると
# 親死検知(stdin EOF → 自死)が即発動して core が起動直後に死ぬ(実測 2026-07-02)
tail -f /dev/null | HISHO_DB="$SMOKE_DIR/secretary.db" HISHO_PORT=0 build/core-dist/python/bin/python3 -m hisho_core &
CORE_PID=$!
sleep 2
PORT=$(python3 -c "import json;print(json.load(open('$SMOKE_DIR/core.json'))['port'])")
curl -s "http://127.0.0.1:$PORT/healthz"
kill $CORE_PID
rm -rf "$SMOKE_DIR"
```

Expected: `{"core":true,"ollama":{…},"model":{…}}`(ollama 停止中でも `core:true` は返る)。
一時 DB + OS 割当ポートを使うのは、開発中の別 core や既定ポートの先客と衝突させないため。

- [ ] **Step 4: Commit**

```bash
git add scripts/build_core.sh && git commit -m "feat(packaging): build_core.sh — bundled relocatable CPython tree"
```

---

### Task 10: Xcode プロジェクト + アプリ組立 + 実機起動

XcodeGen で `.xcodeproj` を生成し、HishoKit + 同梱 Python を `.app` に固めて実機起動。**Task 1 のスパイク結果で Variant A/B を選ぶ。**

**Files:**
- Create: `HishoApp/project.yml`
- Create: `HishoApp/Sources/HishoApp.swift`(Variant A **または** B)
- Create: `HishoApp/Sources/AppRuntime.swift`
- Create: `scripts/embed_core.sh`(chmod +x)

**Interfaces:**
- Consumes: `ChatView`/`ChatStore`/`CoreProcessManager`/`CoreChatClient`/`CoreLaunchConfig` (HishoKit), `build/core-dist/python` (Task 9)
- Produces: `build/derived/Build/Products/Debug/Hisho.app`

- [ ] **Step 1: project.yml**

`HishoApp/project.yml`:

```yaml
# 役割: HishoApp.xcodeproj の生成定義(XcodeGen)。生成物はコミットせず、このファイルが真実。
name: HishoApp
options:
  bundleIdPrefix: dev.hisho
  deploymentTarget:
    macOS: "26.0"
packages:
  HishoKit:
    path: ../HishoKit
settings:
  base:
    SWIFT_VERSION: "6.0"
    MACOSX_DEPLOYMENT_TARGET: "26.0"
targets:
  HishoApp:
    type: application
    platform: macOS
    sources: [Sources]
    dependencies:
      - package: HishoKit
    info:
      path: Info.plist
      properties:
        CFBundleName: Hisho
        CFBundleDisplayName: Hisho
        LSUIElement: true            # Dock に出さないメニューバー常駐
        LSMinimumSystemVersion: "26.0"
        NSAppTransportSecurity:
          NSAllowsLocalNetworking: true
    settings:
      base:
        PRODUCT_NAME: Hisho
        PRODUCT_BUNDLE_IDENTIFIER: dev.hisho.app
        CODE_SIGN_STYLE: Manual
        CODE_SIGN_IDENTITY: "-"      # ad-hoc 署名(自機ローカル、spec §5)
        ENABLE_HARDENED_RUNTIME: "NO"   # quote 必須 — YAML boolean 化を避ける
        ENABLE_APP_SANDBOX: "NO"        # 同上。entitlements ファイルは作らない(sandbox OFF の実体)
    postBuildScripts:
      - name: Embed Python core
        script: '"${PROJECT_DIR}/../scripts/embed_core.sh"'
        basedOnDependencyAnalysis: false
schemes:
  HishoApp:
    build:
      targets:
        HishoApp: all
    run:
      config: Debug
```

- [ ] **Step 2: embed_core.sh**

`scripts/embed_core.sh`:

```bash
#!/usr/bin/env bash
# 役割: Xcode ビルド中に build/core-dist/python を .app の Contents/Resources/core/python へ rsync。
#       ビルド成果物の署名前フェーズで走る(Run Script)。
set -euo pipefail

SRC="${PROJECT_DIR}/../build/core-dist/python"
DST="${TARGET_BUILD_DIR}/${UNLOCALIZED_RESOURCES_FOLDER_PATH}/core/python"

if [ ! -d "$SRC" ]; then
  echo "error: $SRC がない。先に scripts/build_core.sh を実行" >&2
  exit 1
fi

mkdir -p "$DST"
rsync -a --delete "$SRC/" "$DST/"
echo "embedded python core -> $DST"
```

- [ ] **Step 3: AppRuntime**

`HishoApp/Sources/AppRuntime.swift`:

```swift
// 役割: アプリ層の合成ルート — CoreProcessManager と ChatStore を 1 箇所で生成・保持。
// popover(View)より寿命が長く、stream とプロセス監視がここに生き続ける。
import Foundation
import HishoKit

@MainActor
final class AppRuntime {
    static let shared = AppRuntime()

    let core: CoreProcessManager
    let chat: ChatStore

    private init() {
        let pythonURL = Bundle.main.resourceURL!
            .appendingPathComponent("core/python/bin/python3")
        core = CoreProcessManager(config: CoreLaunchConfig(pythonURL: pythonURL))
        chat = ChatStore(client: CoreChatClient())
    }

    func start() { core.start() }
    func shutdown() { core.stop() }
}
```

- [ ] **Step 4: アプリエントリ(スパイク結果で選択)**

**Variant A — スパイク PASS(MenuBarExtra `.window`)**: `HishoApp/Sources/HishoApp.swift`:

```swift
// 役割: アプリエントリ(MenuBarExtra ホスト)。UI は HishoKit.ChatView に全部委譲する薄殻。
import HishoKit
import SwiftUI

@main
struct HishoApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        MenuBarExtra("Hisho", systemImage: "person.crop.circle") {
            ChatView(chat: AppRuntime.shared.chat, core: AppRuntime.shared.core)
        }
        .menuBarExtraStyle(.window)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        AppRuntime.shared.start()
    }
    func applicationWillTerminate(_ notification: Notification) {
        // SIGTERM(core は graceful shutdown で ollama unload)。
        // これ自体が失敗しても、親死で stdin が EOF になり core は自死する。
        AppRuntime.shared.shutdown()
    }
}
```

**Variant B — スパイク FAIL(AppKit `NSStatusItem` + `NSPopover`)**: `HishoApp/Sources/HishoApp.swift`:

```swift
// 役割: アプリエントリ(AppKit popover ホスト)。MenuBarExtra の focus 不具合時の退避先。
// UI は HishoKit.ChatView に全部委譲 — ホストが変わっても chat view は同一。
import AppKit
import HishoKit
import SwiftUI

@main
struct HishoMain {
    static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.accessory)
        app.run()
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var statusItem: NSStatusItem!
    private let popover = NSPopover()

    func applicationDidFinishLaunching(_ notification: Notification) {
        AppRuntime.shared.start()

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem.button?.image = NSImage(
            systemSymbolName: "person.crop.circle", accessibilityDescription: "Hisho")
        statusItem.button?.action = #selector(togglePopover)
        statusItem.button?.target = self

        popover.behavior = .transient
        popover.contentViewController = NSHostingController(
            rootView: ChatView(chat: AppRuntime.shared.chat, core: AppRuntime.shared.core))
    }

    @objc private func togglePopover() {
        guard let button = statusItem.button else { return }
        if popover.isShown {
            popover.performClose(nil)
        } else {
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
            NSApp.activate()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        AppRuntime.shared.shutdown()
    }
}
```

- [ ] **Step 5: 生成 + ビルド**

```bash
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
cd HishoApp && xcodegen generate && cd ..
xcodebuild -project HishoApp/HishoApp.xcodeproj -scheme HishoApp \
  -configuration Debug -derivedDataPath build/derived build 2>&1 | tail -5
```

Expected: `** BUILD SUCCEEDED **`(初回はエラーを読みつつ project.yml を修正してよいが、**Global Constraints の設定値は変えない**)

- [ ] **Step 6: 実機起動スモーク**

```bash
APP=build/derived/Build/Products/Debug/Hisho.app
"$APP/Contents/MacOS/Hisho" &
sleep 3
cat "$HOME/Library/Application Support/Hisho/core.json"
PORT=$(python3 -c "import json,os;print(json.load(open(os.path.expanduser('~/Library/Application Support/Hisho/core.json')))['port'])")
curl -s "http://127.0.0.1:$PORT/healthz"
```

Expected: メニューバーにアイコン出現、core.json に `{pid, port}`、healthz が `core:true`。

- [ ] **Step 7: ユーザー実機チェック(AskUserQuestion)**

1. popover を開いて挨拶 → 逐次描画で返事が来る
2. streaming 中に popover を閉じて開き直す → 続きが描画されている(アプリ層 store 保持の確認)
3. focus 再確認(バンドル版でもスパイクと同じ 3 点)
4. UI 見た目の初期感想(色/フォント調整は後続の実機イテレーションで)

- [ ] **Step 8: Commit**

```bash
git add HishoApp/ scripts/embed_core.sh .gitignore
git commit -m "feat(shell): Xcode project (XcodeGen) + app entry + bundled core embed"
```

---

### Task 11: E2E スモーク + relocation + 孤児化テスト + SMOKE.md

**Files:**
- Create: `scripts/smoke_relocation.sh`(chmod +x)
- Create: `scripts/check_no_egress.sh`(chmod +x)
- Modify: `core/SMOKE.md`(Swift 殻の手順を追記)

**Interfaces:**
- Consumes: Task 10 の `.app`

- [ ] **Step 1: relocation smoke スクリプト**

`scripts/smoke_relocation.sh`:

```bash
#!/usr/bin/env bash
# 役割: ビルド済 .app を /tmp に移動して起動し、core が動く(relocatable)ことと
#       親死→core 自死(stdin EOF)を自動確認する(spec §4/§6)。
set -euo pipefail
cd "$(dirname "$0")/.."

SRC_APP="build/derived/Build/Products/Debug/Hisho.app"
DST_APP="/tmp/Hisho-reloc.app"
CORE_JSON="$HOME/Library/Application Support/Hisho/core.json"

[ -d "$SRC_APP" ] || { echo "error: 先に Task 10 のビルドを実行" >&2; exit 1; }

rm -rf "$DST_APP"
ditto "$SRC_APP" "$DST_APP"

"$DST_APP/Contents/MacOS/Hisho" &
APP_PID=$!
trap 'kill -9 $APP_PID 2>/dev/null || true' EXIT

# core.json 出現 → healthz を待つ (最大 15 秒)
for i in $(seq 1 30); do
  sleep 0.5
  PORT=$(python3 -c "import json;print(json.load(open('$CORE_JSON'))['port'])" 2>/dev/null) || continue
  curl -sf "http://127.0.0.1:$PORT/healthz" >/dev/null && break
done
curl -sf "http://127.0.0.1:$PORT/healthz" | grep -q '"core":true' \
  && echo "OK: relocated .app から core 起動" \
  || { echo "FAIL: healthz 不達" >&2; exit 1; }

CORE_PID=$(python3 -c "import json;print(json.load(open('$CORE_JSON'))['pid'])")

# 親を強制殺害 → stdin EOF → core 自死を確認 (最大 5 秒)
kill -9 "$APP_PID"
trap - EXIT
for i in $(seq 1 10); do
  sleep 0.5
  if ! kill -0 "$CORE_PID" 2>/dev/null; then
    echo "OK: 親死→core 自死 (孤児化なし)"
    rm -rf "$DST_APP"
    exit 0
  fi
done
echo "FAIL: core が孤児化 (pid $CORE_PID)" >&2
exit 1
```

- [ ] **Step 2: 実行**

Run: `chmod +x scripts/smoke_relocation.sh && scripts/smoke_relocation.sh`
Expected: `OK: relocated .app から core 起動` → `OK: 親死→core 自死 (孤児化なし)`

- [ ] **Step 3: egress チェックスクリプト(spec §11.4 のユーザ実行可能チェック)**

`scripts/check_no_egress.sh`:

```bash
#!/usr/bin/env bash
# 役割: Hisho / core / ollama が loopback 以外に接続していないことを目視確認する(spec §11)。
# 使い方: チャットを 1 往復してから実行。出力ゼロ = OK。
FOUND=$(lsof -i -nP 2>/dev/null | grep -iE 'hisho|python3.*hisho_core|ollama' \
        | grep -vE '127\.0\.0\.1|\[::1\]|localhost' || true)
if [ -z "$FOUND" ]; then
  echo "OK: 非 loopback 接続なし"
else
  echo "確認が必要な接続:"
  echo "$FOUND"
fi
```

- [ ] **Step 4: 手動スモーク(SMOKE.md 追記 + 実施)**

`core/SMOKE.md` に「Swift 殻 E2E」節を追記し、以下を実機実施:

```markdown
## Swift 殻 E2E (Plan 2)

前提: ollama 稼働 (`ollama serve`)、`scripts/build_core.sh` + Task 10 ビルド済。

1. **チャット往復**: .app 起動 → メニューバー → popover → 挨拶 → 逐次描画で応答。
2. **記録確認**: `sqlite3 "$HOME/Library/Application Support/Hisho/secretary.db" \
   "SELECT role, status, substr(content,1,20), json_extract(meta,'$.source') FROM turns ORDER BY id DESC LIMIT 4;"`
   → user/assistant が complete、source=popover。
3. **popover 破棄耐性**: 長い応答を要求 → streaming 中に popover を閉じ 3 秒後に再度開く → 続きが表示されている。
4. **graceful 終了**: アプリ終了 → `pgrep -f hisho_core` が空(開発中の別 core が居ない前提。居るなら core.json の pid で確認)。
5. **強制終了(孤児化なし)**: 再起動 → `kill -9 <Hisho pid>` → 2 秒以内に `pgrep -f hisho_core` が空。
6. **ollama down 表示**: `OLLAMA_HOST=http://127.0.0.1:9 build/derived/Build/Products/Debug/Hisho.app/Contents/MacOS/Hisho`
   → バナー「ollama に接続できません」→ 終了。
7. **core stopped 表示**: 通常起動 → `kill -9 $(pgrep -f hisho_core)` → バナー「core 停止」→ [再起動] → 復帰。
8. **外部ツール互換**: 稼働中に `curl -N http://127.0.0.1:51100/v1/chat/completions -H 'Content-Type: application/json' \
   -d '{"model":"qwen3.6:35b-a3b","stream":true,"messages":[{"role":"user","content":"1+1は?"}]}'`
   → SSE が流れ、DB に source=external で記録。
9. **relocation + 孤児化**: `scripts/smoke_relocation.sh` → 2 つの OK。
10. **egress なし**: チャット後 `scripts/check_no_egress.sh` → 「OK: 非 loopback 接続なし」。
```

- [ ] **Step 5: 全自動テスト最終確認**

```bash
core/.venv/bin/python -m pytest core/tests/ -q     # 41 passed
cd HishoKit && swift test 2>&1 | tail -3            # 26 tests passed
```

- [ ] **Step 6: Commit**

```bash
git add scripts/ core/SMOKE.md
git commit -m "test(e2e): relocation + orphan-death smoke, egress check, SMOKE.md swift-shell section"
```

---

## 完了条件 (Plan 2 DoD)

1. `swift test`(HishoKit) + `pytest`(core) 全 green。
2. SMOKE.md「Swift 殻 E2E」10 項目すべて OK(ユーザー確認込み)。
3. `.app` を移動しても動く。親をどう殺しても core が残らない。
4. ブランチ全体レビュー(最上位モデル) → main マージ(superpowers:finishing-a-development-branch)。

## 非目標 (このプランでやらない)

- **SMAppService(ログイン時自動起動)** — spec の scope cuts「LaunchAgent 常駐なし」に合わせ見送り。欲しくなったら `SMAppService.mainApp.register()` 1 箇所 + Xcode 設定で追加可能。
- 履歴画面(`/history` UI)・複数セッション・新規会話ボタン — MVP は起動ごとに 1 セッション。
- notarize / Developer ID / Hardened Runtime(spec §5 どおりドキュメントのみ)。
- 色/フォント/モーションの作り込み(実機を見てから別途イテレーション)。
- Python ツリーの strip / サイズ削減(~90-120MB を受容、spec §2)。

## spec からの意図的な逸脱 (レビュー用に明示)

1. **プロセスグループごと kill → 単一 pid kill に簡略化**。uvicorn は `workers=1` の in-process 実行で孫プロセスを作らないため、group kill は不要と判断。core が子を持つ日が来たら再導入。
2. **stale core の「我々の python か」照合を pid 生存 + healthz 署名のみに簡略化**(実行パス検査なし)。Python 側 `lifecycle.is_our_stale_core` と同じ判定基準に揃えた。pid 再利用 + 別プロセスが同ポートで core:true を返す確率は自機ローカルでは無視できると判断。
3. **HishoKit / HishoApp の 2 層構造**(spec §4 は HishoApp/ のみ)。chat view の host-agnostic 要求(spec §14)と `swift test` 高速サイクルのため。
4. **XcodeGen 導入**(導入済バイナリ使用)。.xcodeproj を手管理せず project.yml をコミットする。「Xcode がビルド/署名/plist を所有する」という spec の意図は維持。

## スパイク結果 (Task 1 で記入)

- [ ] 判定: PASS / FAIL(→ Task 10 Variant A / B)
- 観察メモ:

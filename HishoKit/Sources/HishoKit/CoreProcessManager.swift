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
    /// healthz が返す稼働モデル名(UI ヘッダ表示用)。
    public private(set) var modelName: String?

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
        if let name = health?.modelName { modelName = name }
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

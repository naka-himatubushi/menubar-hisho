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

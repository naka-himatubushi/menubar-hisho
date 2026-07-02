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

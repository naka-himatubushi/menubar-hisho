// 役割: 6 状態導出の全分岐 — プロセス死/応答なし(タイムアウト前後)/ollama down/warming/ready/idle。
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

    // --- 電源ボタン: manuallyUnloaded フラグの分岐 ---

    @Test func reachableNotLoadedManuallyUnloadedIsIdle() {
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: true, modelLoaded: false),
            secondsSinceLaunch: 5, startupTimeout: 20,
            manuallyUnloaded: true)
        #expect(s == .idle)
    }

    @Test func reachableNotLoadedNotManuallyUnloadedIsWarming() {
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: true, modelLoaded: false),
            secondsSinceLaunch: 5, startupTimeout: 20,
            manuallyUnloaded: false)
        #expect(s == .warmingModel)
    }

    @Test func loadedWinsOverManuallyUnloadedFlag() {
        // modelLoaded が最優先: フラグが立っていてもロード済なら .ready
        // (フラグ判定を loaded 判定より前に動かす回帰を検出する)
        let s = CoreStateReducer.derive(
            processRunning: true,
            health: HealthSnapshot(ollamaReachable: true, modelLoaded: true),
            secondsSinceLaunch: 5, startupTimeout: 20,
            manuallyUnloaded: true)
        #expect(s == .ready)
    }
}

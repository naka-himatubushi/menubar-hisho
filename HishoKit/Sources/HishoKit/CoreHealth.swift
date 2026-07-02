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
            modelLoaded: model?["loaded"] as? Bool ?? false,
            modelName: model?["name"] as? String)
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

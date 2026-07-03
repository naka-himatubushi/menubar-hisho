// 役割: HishoKit 全体で共有する値型 — core の状態・チャットメッセージ・healthz スナップショット。
import Foundation

/// Swift 殻から見た core の 6 状態(spec §3 の状態表示)。
public enum CoreState: Equatable, Sendable {
    case startingCore
    case warmingModel
    case ready
    /// ユーザーが手動でモデルをアンロードした状態。core + ollama は稼働中、再ロード待ち。
    case idle
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
    public var modelName: String?

    public init(ollamaReachable: Bool, modelLoaded: Bool, modelName: String? = nil) {
        self.ollamaReachable = ollamaReachable
        self.modelLoaded = modelLoaded
        self.modelName = modelName
    }
}

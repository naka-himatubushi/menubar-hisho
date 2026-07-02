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

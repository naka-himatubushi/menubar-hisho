// 役割: popover の中身 — ヘッダ(状態ドット) + 会話ログ(逐次描画・自動スクロール) + 入力欄。
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
            header
            StatusBanner(state: core.state) { core.restart() }
            if chat.messages.isEmpty {
                emptyState
            } else {
                messageLog
            }
            Divider()
            inputBar
        }
        .frame(width: 360, height: 480)
        .onAppear { inputFocused = true }
    }

    // MARK: - パーツ

    private var header: some View {
        HStack(spacing: 6) {
            Image(systemName: "mustache.fill")
                .font(.subheadline)
                .foregroundStyle(.secondary)
            Text("JARVIS").font(.headline)
            Circle()
                .fill(statusColor)
                .frame(width: 8, height: 8)
            if let model = core.modelName {
                Text(model).font(.caption2).foregroundStyle(.tertiary)
            }
            Spacer()
            Button(action: { chat.clear() }) {
                Image(systemName: "square.and.pencil")
                    .font(.subheadline)
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("新しい会話")
            .disabled(chat.messages.isEmpty && !chat.isStreaming)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var statusColor: Color {
        switch core.state {
        case .ready: .green
        case .startingCore, .warmingModel: .orange
        case .ollamaDown, .coreStopped: .red
        }
    }

    private var emptyState: some View {
        VStack(spacing: 8) {
            Spacer()
            Image(systemName: "mustache.fill")
                .font(.system(size: 40))
                .foregroundStyle(.tertiary)
            Text("何でも聞いてください")
                .font(.caption)
                .foregroundStyle(.secondary)
            Spacer()
        }
        .frame(maxWidth: .infinity)
    }

    private var messageLog: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 10) {
                    ForEach(chat.messages) { message in
                        MessageRow(message: message).id(message.id)
                    }
                }
                .padding(10)
            }
            .onChange(of: chat.messages.last?.text) {
                if let last = chat.messages.last {
                    proxy.scrollTo(last.id, anchor: .bottom)
                }
            }
        }
    }

    private var inputBar: some View {
        HStack(spacing: 8) {
            TextField("メッセージを入力…", text: $input)
                .textFieldStyle(.plain)
                .padding(.horizontal, 12)
                .padding(.vertical, 7)
                .background(Capsule().fill(Color.gray.opacity(0.12)))
                .focused($inputFocused)
                .onSubmit(send)
                .disabled(!canSend)
            Button(action: send) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.system(size: 24))
                    .foregroundStyle(sendEnabled ? Color.accentColor : Color.gray.opacity(0.4))
            }
            .buttonStyle(.plain)
            .disabled(!sendEnabled)
        }
        .padding(10)
    }

    private var canSend: Bool {
        // warmingModel でも送れる: ollama はリクエストで自動再ロードするため(初回だけ遅い)。
        // 30 分アイドルで unload された後もアプリ再起動なしで会話を再開できる。
        (core.state == .ready || core.state == .warmingModel) && !chat.isStreaming
    }

    private var sendEnabled: Bool {
        canSend && !input.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private func send() {
        guard sendEnabled else { return }
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
            banner("モデルを準備中…(送信できます。最初の返事だけ遅めです)", tint: .secondary)
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

/// 1 メッセージの行。user は右寄せアクセント色、assistant は左寄せ + streaming カーソル/エラー表示。
struct MessageRow: View {
    let message: ChatMessage

    var body: some View {
        HStack {
            if message.role == .user { Spacer(minLength: 48) }
            VStack(alignment: .leading, spacing: 4) {
                Text(inlineMarkdown(displayText))
                    .textSelection(.enabled)
                if case .error(let reason) = message.status {
                    Text("⚠️ \(reason)").font(.caption2).foregroundStyle(.red)
                }
            }
            .padding(.horizontal, 11)
            .padding(.vertical, 7)
            .background(
                RoundedRectangle(cornerRadius: 12)
                    .fill(message.role == .user ? Color.accentColor.opacity(0.18)
                                                : Color.gray.opacity(0.12)))
            if message.role == .assistant { Spacer(minLength: 48) }
        }
    }

    /// persona は平文出力を指示しているが、モデルが Markdown 記号を混ぜた時の保険として
    /// インライン要素 (**太字**・`等幅`) だけ描画に変換する。ブロック要素は平文のまま。
    private func inlineMarkdown(_ text: String) -> AttributedString {
        (try? AttributedString(
            markdown: text,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(text)
    }

    /// streaming 中は末尾にカーソル ▍ を出す(空なら ▍ のみ)。
    private var displayText: String {
        if message.status == .streaming {
            return message.text + "▍"
        }
        return message.text
    }
}

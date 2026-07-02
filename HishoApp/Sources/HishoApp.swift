// 役割: アプリエントリ(MenuBarExtra ホスト)。UI は HishoKit.ChatView に全部委譲する薄殻。
// focus 不具合が実機確認で出た場合は AppKit NSStatusItem+NSPopover ホストへ差し替える
// (Variant B、計画書 Task 10 Step 4 参照。ChatView は host-agnostic なので無変更)。
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
        // SIGTERM → 最大 3 秒待ち(core の graceful shutdown = ollama unload)。
        // これ自体が失敗しても、親死で stdin が EOF になり core は自死する。
        AppRuntime.shared.shutdown()
    }
}

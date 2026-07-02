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

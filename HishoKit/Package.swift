// swift-tools-version: 6.0
// 役割: Hisho の Swift ロジックと View を持つパッケージ。swift test で単体テスト可能(Xcode 不要)。
import PackageDescription

let package = Package(
    name: "HishoKit",
    platforms: [.macOS(.v15)],
    products: [.library(name: "HishoKit", targets: ["HishoKit"])],
    targets: [
        .target(name: "HishoKit"),
        .testTarget(name: "HishoKitTests", dependencies: ["HishoKit"]),
    ]
)

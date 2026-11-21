// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "BoxzOffice",
    platforms: [.macOS(.v14)],
    products: [.executable(name: "BoxzOffice", targets: ["BoxzOffice"])],
    targets: [
        // 与 MCP 工具同语义的只读 op 的适配器：身份只来自 socket 描述文件 + 令牌，App 不自称 actor。
        .target(name: "OfficeClient"),
        .executableTarget(name: "BoxzOffice", dependencies: ["OfficeClient"]),
        .testTarget(name: "OfficeClientTests", dependencies: ["OfficeClient"]),
    ]
)

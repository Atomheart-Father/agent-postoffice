import Foundation

/// 写入模式。老板就是这个 App 的使用者，对正式 ~/office11 默认允许写——但每个写操作都要先弹确认、结果按三态显示。
/// - `.isolated`：home 在 /private/tmp 下（隔离 home，自己起的独立 officed）——只给测试 / 取证用，顶栏标"隔离写入"。
/// - `.official`：其余一切（正式 home）。
public enum WriteMode: Sendable, Equatable { case isolated, official }

public enum WriteGate {
    public static let isolatedRoot = "/private/tmp/"

    public static func mode(home: URL) -> WriteMode {
        isIsolated(path: home.path) ? .isolated : .official
    }

    /// 解开符号链接再比：/tmp、/private/tmp/../x、指到别处的链接都不能混过去。
    /// 用 realpath（Foundation 的 resolvingSymlinksInPath 会把 /private/tmp 缩成 /tmp）；
    /// 路径还不存在时，解开最近的已存在祖先，再把剩下的接上。
    public static func isIsolated(path: String) -> Bool {
        let real = realPath(path)
        return real.hasPrefix(isolatedRoot) && real.count > isolatedRoot.count
    }

    static func realPath(_ path: String) -> String {
        var url = URL(fileURLWithPath: path).standardizedFileURL
        var rest: [String] = []
        while true {
            if let r = realpath(url.path, nil) {
                defer { free(r) }
                return ([String(cString: r)] + rest.reversed()).joined(separator: "/").replacingOccurrences(of: "//", with: "/")
            }
            if url.path == "/" { return path }
            rest.append(url.lastPathComponent)
            url = url.deletingLastPathComponent()
        }
    }
}

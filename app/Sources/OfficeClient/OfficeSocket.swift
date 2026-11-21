import Darwin
import Foundation

/// 老板这一端点的描述文件（`<home>/sock/boss.endpoint.json`）。令牌本体不在里面，只给路径。
struct EndpointDescriptor: Decodable {
    let socketPath: String
    let tokenFile: String
}

/// 最小 AF_UNIX 客户端：一行一个 JSON，一次请求一条连接。
/// 令牌只在发送那一刻从文件读进内存，拼进帧就丢；不落盘、不进日志、不进错误文本、不进界面。
public struct OfficeSocket: Sendable {
    public let home: URL
    public let timeout: TimeInterval
    /// 读取日志（默认关，由 App 注入）：给了路径就每次读 op 追加一行"时间 op"。只记时间和 op 名，不记参数、不记令牌。
    /// 用来实测每轮读了多少次、窗口不在前台时读取确实停了。
    public let readLog: URL?
    static let maxReply = 8 * 1024 * 1024

    public init(home: URL = FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("office11"),
                timeout: TimeInterval = 8,
                readLog: URL? = nil) {
        self.home = home
        self.timeout = timeout
        self.readLog = readLog
    }

    private static let logLock = NSLock()
    private static let stamp: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter(); f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]; return f
    }()
    private func logRead(_ op: String) {
        guard let url = readLog else { return }
        let line = Data("\(Self.stamp.string(from: Date())) \(op)\n".utf8)
        Self.logLock.lock(); defer { Self.logLock.unlock() }
        if let h = try? FileHandle(forWritingTo: url) { defer { try? h.close() }; _ = try? h.seekToEnd(); try? h.write(contentsOf: line) }
        else { try? line.write(to: url) }
    }

    /// 发一帧、读一帧。返回已解析的 JSON 对象；失败分成未连接 / 未知两类。
    /// `isolatedOnly`：写入用——端点描述里的 socket 与令牌文件也必须在隔离目录下，
    /// 否则 /private/tmp 里放一个指向正式 officed 的描述文件就能绕过开关。
    func exchange(_ request: [String: Any], isolatedOnly: Bool = false) -> Outcome<[String: Any]> {
        if !isolatedOnly { logRead(request["op"] as? String ?? "?") }
        let descriptorURL = home.appendingPathComponent("sock/boss.endpoint.json")
        guard let raw = try? Data(contentsOf: descriptorURL),
              let desc = try? JSONDecoder().decode(EndpointDescriptor.self, from: raw) else {
            return .notConnected(detail: tr("没有可用的端点描述文件", "No usable endpoint file"))
        }
        // 写入：检查的和实际连的 / 读的是同一份解析后的路径（检查之后再换符号链接也没用）。
        let socketPath = isolatedOnly ? WriteGate.realPath(desc.socketPath) : desc.socketPath
        let tokenFile = isolatedOnly ? WriteGate.realPath(desc.tokenFile) : desc.tokenFile
        if isolatedOnly, !(WriteGate.isIsolated(path: socketPath) && WriteGate.isIsolated(path: tokenFile)) {
            return .notConnected(detail: tr("写入被拒：端点不在隔离目录", "Write refused: endpoint is not in the isolated directory"))
        }
        guard var token = try? String(contentsOfFile: tokenFile, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty else {
            return .notConnected(detail: tr("没有可用的令牌文件", "No usable token file"))
        }
        defer { token = "" }
        var frame = request
        frame["session_token"] = token
        guard var line = try? JSONSerialization.data(withJSONObject: frame) else {
            return .unknown(detail: tr("请求无法编码", "Request cannot be encoded"))
        }
        line.append(0x0A)

        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return .notConnected(detail: tr("无法创建 socket", "Cannot create socket")) }
        defer { close(fd) }
        var tv = timeval(tv_sec: Int(timeout), tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        var nosigpipe: Int32 = 1
        setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &nosigpipe, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(socketPath.utf8CString)
        guard pathBytes.count <= MemoryLayout.size(ofValue: addr.sun_path) else {
            return .notConnected(detail: tr("socket 路径过长", "Socket path too long"))
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { dst in
            pathBytes.withUnsafeBytes { dst.copyMemory(from: $0) }
        }
        let connected = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else { return .notConnected(detail: tr("连不上 officed（\(String(cString: strerror(errno)))）", "Cannot reach officed (\(String(cString: strerror(errno))))")) }

        // 从这里起，任何失败都是"未知"：请求可能已经到了对方。
        var sent = 0
        let total = line.count
        let writeFailed: Bool = line.withUnsafeBytes { buf in
            while sent < total {
                let n = write(fd, buf.baseAddress!.advanced(by: sent), total - sent)
                if n <= 0 { return true }
                sent += n
            }
            return false
        }
        if writeFailed { return .unknown(detail: tr("发送中断", "Send interrupted")) }

        var reply = Data()
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        while !reply.contains(0x0A) {
            let n = read(fd, &chunk, chunk.count)
            if n < 0 { return .unknown(detail: errno == EAGAIN ? tr("等待答复超时", "Timed out waiting for reply") : tr("读取答复失败", "Failed to read reply")) }
            if n == 0 { break }
            reply.append(chunk, count: n)
            if reply.count > Self.maxReply { return .unknown(detail: tr("答复过大", "Reply too large")) }
        }
        guard let end = reply.firstIndex(of: 0x0A) else { return .unknown(detail: tr("连接在答复前关闭", "Connection closed before reply")) }
        guard let obj = (try? JSONSerialization.jsonObject(with: reply[..<end])) as? [String: Any] else {
            return .unknown(detail: tr("答复读不懂", "Reply not understood"))
        }
        return .ok(obj)
    }
}

import Foundation

/// 一次读的结果。四件事分开：成功、明确拒绝、结果未知、未连接。零值（成功但为 0）不在这里，在 `.ok` 的数据里。
public enum Outcome<T: Sendable>: Sendable {
    case ok(T)
    /// 后端明确拒绝（`ok:false` + code）；`message` 是后端给的一句话，原样显示。
    case refused(code: String, message: String)
    /// 没拿到可信的答复：连上之后超时/断线/回包读不懂，或后端自己说 `unknown`。不是零，也不是拒绝。
    case unknown(detail: String)
    /// 连都没连上：描述文件、令牌或 socket 不在/不可用。
    case notConnected(detail: String)

    /// 成功就换个类型，失败原样带过去。
    public func map<U: Sendable>(_ f: (T) -> U) -> Outcome<U> { flatMap { .ok(f($0)) } }

    public func flatMap<U: Sendable>(_ f: (T) -> Outcome<U>) -> Outcome<U> {
        switch self {
        case .ok(let v): f(v)
        case .refused(let c, let m): .refused(code: c, message: m)
        case .unknown(let d): .unknown(detail: d)
        case .notConnected(let d): .notConnected(detail: d)
        }
    }
}

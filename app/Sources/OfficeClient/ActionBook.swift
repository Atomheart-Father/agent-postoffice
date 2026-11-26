import Foundation

/// 决策入口的状态机（纯逻辑，不碰界面和网络，单测直接测）。
/// key = "decide:HR-2" / "reply:<信号>" / "review:<work_ref>"：一个意图一个 key。

/// 一次决策意图：发出去的内容在这里定死，重试时一个字都不改（幂等键对应的就是这一份内容）。
public enum Intent: Equatable, Sendable, Codable {
    case decide(ref: String, version: Int, decision: RequestDecision)
    case adminApproval(enabled: Bool)
    case reply(to: String, replyTo: String)
    case archive(letterID: String)
    case archiveCompleted(target: String?)
    case positionDuties(position: String, expected: [String])
    case review(workRef: String, subRef: String, decision: ReviewDecision)
    /// 给一个地址写信（不是回信）。
    case compose(to: String)
    /// 下线一个绑定：提 request.create(kind=unbind)。老板自己提的会当场批。
    case unbind(bindingID: String, label: String, generation: Int? = nil)

    /// 收件人（回信 / 写信）。
    public var recipient: String? {
        switch self { case .reply(let to, _): to; case .compose(let to): to; default: nil }
    }

    public var needRequired: Bool {
        if case .compose = self { return true }
        return false
    }

    public var needsText: Bool {
        switch self {
        case .compose, .positionDuties: true
        case .unbind: false
        case .adminApproval, .archive, .archiveCompleted: false
        case .decide(_, _, let d): d == .refuse            // 拒绝可以不写原因，但给框
        case .reply: true
        case .review(_, _, let d): d == .return            // 退回必须写理由
        }
    }
    public var textRequired: Bool {
        switch self {
        case .decide, .unbind, .adminApproval, .archive, .archiveCompleted, .positionDuties: false
        case .reply, .compose: true
        case .review(_, _, let d): d == .return
        }
    }
    public var title: String {
        switch self {
        case .decide(_, _, let d): d == .approve ? tr("同意", "Approve") : tr("拒绝", "Refuse")
        case .adminApproval(let enabled): enabled ? tr("开启管理员审批", "Enable admin approval") : tr("停用管理员审批", "Disable admin approval")
        case .reply: tr("回信", "Reply")
        case .archive: tr("归档", "Archive")
        case .archiveCompleted: tr("归档已结清", "Archive completed")
        case .positionDuties: tr("保存职责", "Save duties")
        case .compose: tr("写信", "Write")
        case .unbind: tr("下线", "Take offline")
        case .review(_, _, let d): d == .accept ? tr("通过", "Accept") : tr("退回", "Return")
        }
    }
}

public enum ActionPhase: Equatable, Sendable {
    case editing
    case sending
    case done(WriteDone)
    /// 后端明确拒绝：这一笔没有发生。
    case refused(code: String, message: String)
    /// 版本冲突：别人先动过。不是"改一改再发"——要重新读取，在新版本上由人重新决定。
    case versionChanged(currentVersion: Int?)
    /// 结果未知：可能已提交。只允许"用同一个键再发"，不允许改内容。
    case unknown(detail: String)
    case notConnected(detail: String)
    /// 这一笔没有发：夹具页上的申请 / 绑定是假的，不能写。
    case disabled(reason: String)
}

public struct ActionState: Equatable, Sendable {
    public var intent: Intent
    public var subject = ""
    public var text = ""
    public var need = ""
    public var outgoingNeed: String {
        if case .reply = intent, need.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return "FYI" }
        return need
    }
    /// 一个意图一个键；重试沿用，被明确拒绝后改后重发才换新键。
    public var actKey: String
    public var retryRef: String?
    public var mayHaveCommitted = false
    public var phase: ActionPhase = .editing

    public init(intent: Intent, subject: String = "", text: String = "", need: String = "", actKey: String,
                retryRef: String? = nil, phase: ActionPhase = .editing) {
        self.intent = intent; self.subject = subject; self.text = text; self.need = need
        self.actKey = actKey; self.retryRef = retryRef; self.phase = phase
    }
}

public struct ActionBook: Equatable, Sendable {
    /// 界面上正在显示的。
    public var actions: [String: ActionState] = [:]
    /// 结果未知、被"关闭"收起来的：键 / retry_ref / 内容都原样留着，直到这一笔有了确切结果。
    public private(set) var held: [String: ActionState] = [:]

    public init() {}

    public var unresolved: [String: ActionState] {
        var result = held
        for (key, action) in actions where action.phase == .sending || action.mayHaveCommitted { result[key] = action }
        return result
    }
    public mutating func restore(_ entries: [String: ActionState]) { held = entries }
    public mutating func sync(_ entries: [String: ActionState]) {
        var next = entries.filter { actions[$0.key] == nil }
        for (key, action) in held where action.phase == .sending && actions[key] == nil { next[key] = action }
        held = next
    }
    public mutating func adopt(_ key: String, _ entry: ActionState) {
        if actions[key] == nil { held[key] = entry } else { actions[key] = entry }
    }

    /// 开始一个意图。该 key 下有"结果未知"的旧笔：不管点的是哪个按钮，都回到那一笔（同一个键、同一份内容）。
    public mutating func begin(_ key: String, _ intent: Intent, subject: String = "", text: String = "",
                               allowed: Bool, deniedReason: String, newKey: () -> String) {
        if let h = held[key] { actions[key] = h; held[key] = nil; return }
        if let a = actions[key], a.phase == .sending || a.mayHaveCommitted { return }
        guard allowed else {
            actions[key] = ActionState(intent: intent, actKey: "-", phase: .disabled(reason: deniedReason)); return
        }
        actions[key] = ActionState(intent: intent, subject: subject, text: text, actKey: newKey())
    }

    public func isHeld(_ key: String) -> Bool { held[key] != nil }

    /// 关闭。结果未知的那一笔不能丢键：收起来，下次点同一个入口接着用。
    public mutating func cancel(_ key: String) {
        guard let a = actions[key] else { return }
        if a.phase == .sending || a.mayHaveCommitted { held[key] = a }
        actions[key] = nil
    }

    /// 被明确拒绝（不是版本冲突）之后改一改再发：回到编辑，换新键（旧键对应的是被拒的那一份内容）。
    public mutating func reedit(_ key: String, newKey: () -> String) {
        guard var a = actions[key], case .refused = a.phase else { return }
        a.phase = .editing; a.actKey = newKey(); a.retryRef = nil
        actions[key] = a
    }

    /// 能不能（再）发：编辑中、结果未知（同键重试）、没连上（根本没发出）。
    public func canSend(_ key: String) -> Bool {
        guard let a = actions[key] else { return false }
        switch a.phase { case .editing, .unknown, .notConnected: break; default: return false }
        if a.intent.textRequired, a.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
        if a.intent.recipient != nil, a.subject.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
        if a.intent.needRequired, a.need.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { return false }
        return true
    }

    /// 写的结果落地。返回是否要重读后端事实：成功、被拒、未知都读（以后端为准，不靠本地猜）。
    @discardableResult
    public mutating func apply(_ out: WriteOutcome, to key: String) -> Bool {
        let wasHeld = actions[key] == nil
        guard var a = actions[key] ?? held[key] else { return false }
        var refresh = false
        switch out {
        case .done(let d): a.mayHaveCommitted = false; a.phase = .done(d); held[key] = nil; refresh = true
        case .refused(let c, let m, _, _) where a.mayHaveCommitted:
            a.phase = .unknown(detail: tr("原请求仍未核实；本次重试被拒 \(c) \(m)", "Original request remains unverified; this retry was refused: \(c) \(m)"))
            refresh = true
        case .refused(let c, let m, let reason, let v):
            a.mayHaveCommitted = false; held[key] = nil; refresh = true
            if reason == "version_conflict" || reason == "sub_not_current" || (c == "conflict" && v != nil) { a.phase = .versionChanged(currentVersion: v) }
            else { a.phase = .refused(code: c, message: m) }
        case .unknown(let d, let r): a.mayHaveCommitted = true; a.phase = .unknown(detail: d); if let r { a.retryRef = r }; refresh = true
        case .notConnected(let d): a.phase = a.mayHaveCommitted ? .unknown(detail: d) : .notConnected(detail: d)
        }
        if wasHeld {
            if case .unknown = a.phase { held[key] = a } else { held[key] = nil }
        } else { actions[key] = a }
        return refresh
    }
}

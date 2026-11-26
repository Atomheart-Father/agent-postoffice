import Foundation

/// App 写入走公开 op，并沿用受控 socket。
/// 不碰 host.*、round.* 等内部 op。每个写必须带调用方生成的 act_key（幂等键）；同一件事重试用**同一个** act_key。
public enum RequestDecision: String, Sendable, Codable { case approve, refuse }
public enum ReviewDecision: String, Sendable, Codable { case accept, `return` }

/// 写的结果，比读多一件事：结果未知（要用同一个 act_key / retry_ref 重试）。
public enum WriteOutcome: Sendable, Equatable {
    case done(WriteDone)
    /// 本次尝试被拒；不能据此判断先前未决尝试。`currentVersion` 是后端报告的当前版本。
    case refused(code: String, message: String, reasonCode: String?, currentVersion: Int?)
    /// 没拿到可信答复：这一笔可能已经提交。**不是失败**——用同一个 act_key 再发，账本会给出是否已提交。
    case unknown(detail: String, retryRef: String?)
    case notConnected(detail: String)
}

public struct WriteDone: Sendable, Equatable {
    public let op: String
    public let state: String?
    public let version: Int?
    /// 后端 result 里认得的几个标量事实（编号、状态…），原样字符串，界面只挑着显示。
    public let facts: [String: String]
}

public struct OfficeWrites: Sendable {
    let socket: OfficeSocket
    public let mode: WriteMode

    public init(socket: OfficeSocket = OfficeSocket(), mode: WriteMode? = nil) {
        self.socket = socket
        self.mode = mode ?? WriteGate.mode(home: socket.home)
    }

    /// 批 / 拒申请。`expectedVersion` 必带：别人先动过就拿到版本冲突，不会盖掉。
    public func decide(requestRef: String, decision: RequestDecision, reason: String, expectedVersion: Int,
                       actKey: String) -> WriteOutcome {
        send("request.decide", ["request_ref": requestRef, "decision": decision.rawValue, "reason": reason,
                                "expected_version": expectedVersion, "act_key": actKey])
    }

    public func adminApproval(enabled: Bool, actKey: String) -> WriteOutcome {
        send("request.admin_set", ["enabled": enabled, "act_key": actKey])
    }

    public func archive(letterID: String, actKey: String) -> WriteOutcome {
        send("letter.archive", ["letter_id": letterID, "act_key": actKey])
    }

    public func archiveCompleted(target: String?, actKey: String) -> WriteOutcome {
        var args: [String: Any] = ["act_key": actKey]
        args["target"] = target
        return send("letter.archive_completed", args)
    }

    public func setPositionDuties(_ position: String, duties: [String], expected: [String], actKey: String) -> WriteOutcome {
        send("position.duties.set", ["position": position, "duties": duties, "expected_duties": expected, "act_key": actKey])
    }

    /// 写信（letter.send）。`replyTo` 给了就是回信，指向被回的那封。`retryRef` 只对 letter.send 有意义（线路只在这个 op 收它）。
    /// `need` 是单行字符串（后端校验）：换行压成空格，空的不带。
    public func letter(to: String, subject: String, body: String, replyTo: String? = nil, actKey: String,
                       retryRef: String? = nil, need: String? = nil) -> WriteOutcome {
        var p: [String: Any] = ["to": to, "subject": subject, "body": body, "act_key": actKey]
        if let replyTo { p["reply_to"] = replyTo }
        if let retryRef { p["retry_ref"] = retryRef }
        let n = (need ?? "").components(separatedBy: .newlines).joined(separator: " ").trimmingCharacters(in: .whitespaces)
        if !n.isEmpty { p["need"] = n }
        return send("letter.send", p)
    }

    /// 下线一个绑定：request.create(kind=unbind, target=绑定号)。老板自己提的申请后端当场批。
    public func unbind(bindingID: String, reason: String, actKey: String, generation: Int? = nil) -> WriteOutcome {
        var p: [String: Any] = ["kind": "unbind", "target": bindingID, "reason": reason, "act_key": actKey]
        if let generation { p["params"] = ["target_generation": generation] }
        return send("request.create", p)
    }

    /// 审核一份提交：accept 通过，return 退回（退回必须写理由）。
    public func review(workRef: String, subRef: String, decision: ReviewDecision, reason: String,
                       actKey: String) -> WriteOutcome {
        var p: [String: Any] = ["work_ref": workRef, "sub_ref": subRef, "decision": decision.rawValue, "act_key": actKey]
        if !reason.isEmpty { p["reason"] = reason }
        return send("work.review", p)
    }

    private func send(_ op: String, _ params: [String: Any]) -> WriteOutcome {
        var frame = params; frame["op"] = op
        switch socket.exchange(frame, isolatedOnly: mode == .isolated) {
        case .notConnected(let d): return .notConnected(detail: d)
        case .unknown(let d): return .unknown(detail: d, retryRef: nil)
        case .refused(let c, let m):
            if ["unknown", "outcome_unknown", "internal"].contains(c) { return .unknown(detail: "\(c): \(m)", retryRef: nil) }
            return .refused(code: c, message: m, reasonCode: nil, currentVersion: nil)
        case .ok(let f):
            if f["ok"] as? Bool == true {
                guard let r = f["result"] as? [String: Any] else { return .unknown(detail: tr("答复缺少 result", "Reply is missing result"), retryRef: nil) }
                return .done(Self.done(op, r))
            }
            let code = f["code"] as? String ?? ""
            if ["unknown", "outcome_unknown", "internal"].contains(code) {
                return .unknown(detail: code + ": " + (f["message"] as? String ?? tr("后端报告结果未知", "The office reports the result as unknown")), retryRef: f["retry_ref"] as? String)
            }
            guard !code.isEmpty else { return .unknown(detail: tr("答复既不是成功也不是拒绝", "Reply is neither success nor refusal"), retryRef: nil) }
            let detail = f["detail"] as? [String: Any]
            return .refused(code: code, message: f["message"] as? String ?? "",
                            reasonCode: f["reason_code"] as? String ?? detail?["reason_code"] as? String,
                            currentVersion: f["current_version"] as? Int ?? detail?["current_version"] as? Int)
        }
    }

    static func done(_ op: String, _ r: [String: Any]) -> WriteDone {
        var facts: [String: String] = [:]
        for (k, v) in r {
            if let s = v as? String { facts[k] = s }
            else if let n = v as? NSNumber, CFGetTypeID(n) != CFBooleanGetTypeID() { facts[k] = n.stringValue }
            else if let b = v as? Bool { facts[k] = b ? "true" : "false" }
        }
        return WriteDone(op: op, state: r["state"] as? String ?? r["decision"] as? String, version: r["version"] as? Int, facts: facts)
    }
}

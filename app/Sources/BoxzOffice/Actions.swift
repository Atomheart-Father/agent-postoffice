import Foundation
import OfficeClient

enum InboxTab: Equatable { case letters, requests }

extension DeskModel {
    /// 夹具页上的申请 / 绑定是假号：批、拒、下线一律不能写（写信和审核不涉及夹具）。
    func fixtureBlocksWrite(_ intent: Intent) -> Bool {
        guard isFixture else { return false }
        switch intent { case .decide, .unbind, .adminApproval: return true; default: return false }
    }

    var actions: [String: ActionState] {
        get { book.actions }
        set { book.actions = newValue }
    }

    func beginAction(_ key: String, _ intent: Intent, subject: String = "", text: String = "") {
        book.begin(key, intent, subject: subject, text: text, allowed: !fixtureBlocksWrite(intent),
                   deniedReason: tr("模拟数据，不能写", "Sample data: cannot write"), newKey: DeskModel.newActKey)
    }
    private static func newActKey() -> String { "boxz-" + UUID().uuidString.lowercased() }

    func cancelAction(_ key: String) { book.cancel(key) }
    func reeditAction(_ key: String) { book.reedit(key, newKey: DeskModel.newActKey) }

    /// 版本冲突之后：重新读取，再清掉这一笔，让这一行带着新版本重新出现——由人重新点，不自动重发。
    func reloadAfterConflict(_ key: String) {
        Task {
            await refresh()
            if case .review(let ref, _, _) = book.actions[key]?.intent, workDetails[ref]?.value == nil { return }
            book.actions[key] = nil
        }
    }

    /// 发送（或对"未知"重试：键、内容、retry_ref 都不变）。
    func confirmAction(_ key: String) {
        guard book.canSend(key), var a = book.actions[key] else { return }
        a.phase = .sending
        book.actions[key] = a
        let writes = self.writes, snapshot = a
        Task {
            let out = await Task.detached { () -> WriteOutcome in
                switch snapshot.intent {
                case .decide(let ref, let version, let d):
                    return writes.decide(requestRef: ref, decision: d, reason: snapshot.text, expectedVersion: version, actKey: snapshot.actKey)
                case .adminApproval(let enabled):
                    return writes.adminApproval(enabled: enabled, actKey: snapshot.actKey)
                case .archive(let id):
                    return writes.archive(letterID: id, actKey: snapshot.actKey)
                case .reply(let to, let replyTo):
                    return writes.letter(to: to, subject: snapshot.subject, body: snapshot.text, replyTo: replyTo,
                                         actKey: snapshot.actKey, retryRef: snapshot.retryRef, need: snapshot.outgoingNeed)
                case .compose(let to):
                    return writes.letter(to: to, subject: snapshot.subject, body: snapshot.text,
                                         actKey: snapshot.actKey, retryRef: snapshot.retryRef, need: snapshot.need)
                case .review(let w, let s, let d):
                    return writes.review(workRef: w, subRef: s, decision: d, reason: snapshot.text, actKey: snapshot.actKey)
                case .unbind(let id, let label):
                    return writes.unbind(bindingID: id, reason: tr("下线 \(label)", "Take \(label) offline"), actKey: snapshot.actKey)
                }
            }.value
            let again = book.apply(out, to: key)
            if case .unbind = snapshot.intent { if offlineFinished { await refresh() } }     // 批量：全部发完再重读一遍
            else if again { await refresh() }                                                // 界面以后端为准
        }
    }

    // MARK: 宿主页：下线（单个 / 一组）。老板自己的绑定在 `ContractSource.hosts(boss:)` 里已经去掉，传进来的都能下线。

    func askOffline(title: String, bindings: [HostBinding]) {
        guard !bindings.isEmpty else { return }
        offlineDraft = OfflineDraft(title: title, bindings: bindings)
    }

    private var offlinePhases: [ActionPhase?] { (offlineDraft?.bindings ?? []).map { book.actions["unbind:" + $0.bindingID]?.phase } }
    /// 确认过了（至少有一笔进了账本）。
    var offlineStarted: Bool { offlinePhases.contains { $0 != nil } }
    /// 每一笔都有了结果（没有还在编辑 / 发送中的）。
    var offlineFinished: Bool { offlineStarted && !offlinePhases.contains { $0 == nil || $0 == .sending || $0 == .editing } }
    var hasSendingOffline: Bool { offlinePhases.contains { $0 == .sending } }

    /// 确认后逐个发：每个绑定一个键、一个意图；结果各自三态显示。
    func confirmOffline() {
        guard let d = offlineDraft, !offlineStarted else { return }
        for b in d.bindings {
            let key = "unbind:" + b.bindingID
            beginAction(key, .unbind(bindingID: b.bindingID, label: b.actor))
            confirmAction(key)
        }
        if offlineFinished { Task { await refresh() } }       // 全部被挡住（夹具）：没有任何一笔在飞，直接收尾
    }

    func closeOffline() {
        for b in offlineDraft?.bindings ?? [] { cancelAction("unbind:" + b.bindingID) }
        offlineDraft = nil
    }
}

extension Intent {
    /// 成功后的一句话：照后端返回的事实说，不夸大（accept 中间版 Work 回到进行中，不是完成）。
    func doneText(_ d: WriteDone) -> String {
        switch self {
        case .adminApproval(let enabled):
            return enabled ? tr("管理员审批已开启", "Admin approval enabled") : tr("管理员审批已停用", "Admin approval disabled")
        case .decide:
            let v = d.version.map { tr(" · 版本 \($0)", " · version \($0)") } ?? ""
            switch d.state {
            case "done": return tr("已同意并执行", "Approved and executed") + v
            case "refused": return tr("已拒绝", "Refused") + v
            case "pending": return tr("已记下你的同意，申请仍待批（还有项没批完）", "Your approval is recorded; the request is still pending (items remain)") + v
            default: return tr("后端已受理", "Accepted by the office") + v + (d.state.map { " · \($0)" } ?? "")
            }
        case .reply, .compose:
            return tr("已发出", "Sent") + (d.facts["letter_id"].map { " · \($0)" } ?? "")
        case .archive:
            return tr("已归档", "Archived")
        case .unbind:
            switch d.state {
            case "done": return tr("已下线", "Offline")
            case "pending": return tr("已提交，等人批准", "Submitted, waiting for approval")
            default: return tr("后端已受理", "Accepted by the office") + (d.state.map { " · \($0)" } ?? "")
            }
        case .review(_, _, let dec):
            let v = d.version.map { tr(" · 版本 \($0)", " · version \($0)") } ?? ""
            if dec == .return { return tr("已退回，Work 回到进行中", "Returned; Work is Active again") + v }
            return (d.state == "Done" ? tr("已通过，Work 已完成", "Accepted; Work is Done") : tr("已通过这一份，Work 回到进行中", "This submission accepted; Work is Active again")) + v
        }
    }
}

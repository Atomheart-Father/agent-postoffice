import Foundation
import OfficeClient

enum InboxTab: Equatable { case letters, requests }

extension DeskModel {
    func fixtureBlocksWrite(_ intent: Intent) -> Bool { isFixture }

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
    func confirmAction(_ key: String) { Task { await performAction(key) } }

    private func performAction(_ key: String) async {
        guard !isFixture, book.canSend(key), var a = book.actions[key] else { return }
        guard let journal, let identity = journalIdentity, let actor = journalActor,
              whoami.value == actor, socket.identity() == identity else {
            let detail = tr("身份或未决记录不可用，请刷新", "Identity or unresolved records unavailable; refresh")
            a.phase = a.mayHaveCommitted ? .unknown(detail: detail) : .notConnected(detail: detail)
            book.actions[key] = a; return
        }
        if case .unbind(_, let label, _) = a.intent, !a.mayHaveCommitted { a.text = tr("下线 \(label)", "Take \(label) offline") }
        a.phase = .sending
        book.actions[key] = a
        let reads = self.reads, endpoint = self.socket
        let writes = OfficeWrites(socket: endpoint.bound(to: identity)), snapshot = a
        let result = await Task.detached { () -> Result<ActionJournal.Attempt, Error> in
            Result {
                try journal.attempt(key, action: snapshot) {
                    guard case .ok(let context) = reads.context(), context.actor == actor, endpoint.identity() == identity else {
                        return .notConnected(detail: tr("无法核验原身份", "Cannot verify original identity"))
                    }
                    switch snapshot.intent {
                    case .decide(let ref, let version, let d):
                        return writes.decide(requestRef: ref, decision: d, reason: snapshot.text, expectedVersion: version, actKey: snapshot.actKey)
                    case .adminApproval(let enabled):
                        return writes.adminApproval(enabled: enabled, actKey: snapshot.actKey)
                    case .archive(let id):
                        return writes.archive(letterID: id, actKey: snapshot.actKey)
                    case .archiveCompleted(let target):
                        return writes.archiveCompleted(target: target, actKey: snapshot.actKey)
                    case .positionDuties(let position, let expected):
                        return writes.setPositionDuties(position, duties: snapshot.text.components(separatedBy: .newlines),
                                                        expected: expected, actKey: snapshot.actKey)
                    case .reply(let to, let replyTo):
                        return writes.letter(to: to, subject: snapshot.subject, body: snapshot.text, replyTo: replyTo,
                                             actKey: snapshot.actKey, retryRef: snapshot.retryRef, need: snapshot.outgoingNeed)
                    case .compose(let to):
                        return writes.letter(to: to, subject: snapshot.subject, body: snapshot.text,
                                             actKey: snapshot.actKey, retryRef: snapshot.retryRef, need: snapshot.need)
                    case .review(let w, let s, let d):
                        return writes.review(workRef: w, subRef: s, decision: d, reason: snapshot.text, actKey: snapshot.actKey)
                    case .unbind(let id, _, let generation):
                        return writes.unbind(bindingID: id, reason: snapshot.text, actKey: snapshot.actKey, generation: generation)
                    }
                }
            }
        }.value
        guard journalIdentity == identity, journalActor == actor else { return }
        let again: Bool
        switch result {
        case .failure:
            a.phase = a.mayHaveCommitted ? .unknown(detail: tr("未决记录保存失败", "Cannot save unresolved action")) : .notConnected(detail: tr("未决记录保存失败", "Cannot save unresolved action"))
            book.adopt(key, a); journalError = tr("未决记录保存失败，未发送", "Cannot save unresolved action; not sent"); return
        case .success(.busy):
            if let existing = try? journal.load()[key] { book.adopt(key, existing); return }
            let detail = tr("另一窗口正在处理此操作", "Another window is handling this action")
            a.phase = a.mayHaveCommitted ? .unknown(detail: detail) : .notConnected(detail: detail)
            book.adopt(key, a); return
        case .success(.existing(let existing)):
            book.adopt(key, existing)
            journalError = tr("另一窗口已有未决操作，请核对原意图", "Another window has an unresolved action; review the original intent")
            return
        case .success(.completed(let out, let persisted)):
            again = book.apply(out, to: key)
            journalError = persisted ? nil : tr("未决记录更新失败，请检查本机文件", "Could not update unresolved actions; check local storage")
        }
        if case .unbind = snapshot.intent { if offlineFinished && !offlineRunning { await refresh() } }
        else if again { await refresh() }
    }

    // MARK: 宿主页：下线（单个 / 一组）。老板自己的绑定在 `ContractSource.hosts(boss:)` 里已经去掉，传进来的都能下线。

    func askOffline(title: String, bindings: [HostBinding]) {
        guard !bindings.isEmpty else { return }
        var seen = Set<String>()
        offlineDraft = OfflineDraft(title: title, bindings: bindings.filter { seen.insert($0.bindingID).inserted })
    }

    private var offlinePhases: [ActionPhase?] { (offlineDraft?.bindings ?? []).map { book.actions["unbind:" + $0.bindingID]?.phase } }
    /// 确认过了（至少有一笔进了账本）。
    var offlineStarted: Bool { offlinePhases.contains { $0 != nil } }
    /// 每一笔都有了结果（没有还在编辑 / 发送中的）。
    var offlineFinished: Bool { offlineStarted && !offlinePhases.contains { $0 == nil || $0 == .sending || $0 == .editing } }
    var hasSendingOffline: Bool { offlineRunning || offlinePhases.contains { $0 == .sending } }

    /// 确认后逐个发：每个绑定一个键、一个意图；结果各自三态显示。
    func confirmOffline() {
        guard let d = offlineDraft, !offlineStarted, !offlineRunning, d.bindings.allSatisfy({ $0.generation != nil }) else { return }
        offlineRunning = true
        for b in d.bindings {
            beginAction("unbind:" + b.bindingID, .unbind(bindingID: b.bindingID, label: b.actor, generation: b.generation))
        }
        Task {
            for b in d.bindings { await performAction("unbind:" + b.bindingID) }
            offlineRunning = false
            await refresh()
        }
    }

    func closeOffline() {
        guard !offlineRunning else { return }
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
        case .archiveCompleted:
            guard let count = d.facts["archived_count"].flatMap(Int.init) else { return tr("归档已完成", "Archiving completed") }
            return tr("已归档 \(count) 封", "Archived \(count) \(pl(count, "letter"))")
        case .positionDuties:
            return tr("职责已保存", "Duties saved")
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

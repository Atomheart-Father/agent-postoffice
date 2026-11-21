import SwiftUI
import OfficeClient

/// 决策入口的内联面板：编辑 → 发送中 → 完成 / 被拒 / 版本已变 / 未知 / 未连接 / 夹具不能写，各说各的。
/// 只在用户点了按钮之后才出现；没有模态框。
struct ActionBox: View {
    @ObservedObject var desk: DeskModel
    let key: String

    var body: some View {
        if let a = desk.actions[key] {
            VStack(alignment: .leading, spacing: 10) {
                if case .editing = a.phase { editor(a) }
                else {
                    PhaseLine(phase: a.phase, intent: a.intent)
                    HStack(spacing: 8) {
                        switch a.phase {
                        case .refused(let code, _):
                            if case .decide(_, _, .approve) = a.intent, code == "invalid_request" {
                                Text(tr("请申请人更正申请内容后重新提交。", "The applicant needs to correct and resubmit this request."))
                                    .font(Theme.Font.small).foregroundStyle(Theme.ink2)
                            } else if case .archive = a.intent {
                                EmptyView()
                            } else {
                                BoxButton(title: tr("改后重发", "Edit and resend")) { desk.reeditAction(key) }
                            }
                        case .versionChanged: BoxButton(title: tr("重新读取", "Reload")) { desk.reloadAfterConflict(key) }    // 重新读取后，在新版本上由你重新决定
                        case .unknown, .notConnected: BoxButton(title: tr("重试", "Retry")) { desk.confirmAction(key) }
                        default: EmptyView()
                        }
                        if a.phase != .sending { BoxButton(title: tr("关闭", "Close")) { desk.cancelAction(key) } }
                    }
                }
            }
            .padding(12)
            .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
            .padding(.top, 8)
        }
    }

    @ViewBuilder private func editor(_ a: ActionState) -> some View {
        Text(a.intent.title).font(Theme.Font.bodyBold)
        if let to = a.intent.recipient {
            Text(tr("收件人 \(to)", "To \(to)")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
            field(tr("主题", "Subject"), text: Binding(get: { desk.actions[key]?.subject ?? "" }, set: { desk.actions[key]?.subject = $0 }), lines: 1)
            let needHint = a.intent.needRequired
                ? tr("需要对方做什么（必填，可写 FYI）", "What you need from them (required, or FYI)")
                : tr("需要对方做什么（留空则仅告知）", "What you need from them (blank sends as FYI)")
            field(needHint, text: Binding(get: { desk.actions[key]?.need ?? "" }, set: { desk.actions[key]?.need = $0 }), lines: 1)
        }
        if a.intent.needsText {
            field(a.intent.textRequired ? tr("必填", "Required") : tr("原因（可不写）", "Reason (optional)"),
                  text: Binding(get: { desk.actions[key]?.text ?? "" }, set: { desk.actions[key]?.text = $0 }),
                  lines: 3)
        }
        HStack(spacing: 8) {
            BoxButton(title: tr("确认\(a.intent.title)", "Confirm: \(a.intent.title)"), primary: true, disabled: !desk.book.canSend(key)) { desk.confirmAction(key) }
            BoxButton(title: tr("取消", "Cancel")) { desk.cancelAction(key) }
        }
    }

    private func field(_ placeholder: String, text: Binding<String>, lines: Int) -> some View {
        TextField(placeholder, text: text, axis: .vertical)
            .textFieldStyle(.plain).font(Theme.Font.f14).lineLimit(lines...(lines + 3))
            .padding(8)
            .overlay(Rectangle().strokeBorder(Theme.wire, lineWidth: Theme.Line.hair))
    }
}

/// 一笔写的结果（非编辑态）的一行字。动作栏的内联面板和下线确认框共用。
struct PhaseLine: View {
    let phase: ActionPhase
    let intent: Intent
    var body: some View {
        switch phase {
        case .editing, .sending: line(tr("发送中…", "Sending…"), tone: Theme.ink3)
        case .done(let d): Text(intent.doneText(d)).font(Theme.Font.f14)
        case .refused(let code, let message): line(tr("被拒绝  \(code)  \(message)", "Refused  \(code)  \(message)"), tone: Theme.bad)
        case .versionChanged(let v):       // 别人先动过：这一笔没有发生。不自动重发。
            line(tr("版本已变", "Version changed") + (v.map { tr("（当前版本 \($0)）", "(current version \($0))") } ?? "") + tr("  这一笔没有发生", "  This one did not happen"), tone: Theme.bad)
        case .unknown(let d): line(tr("结果未知  \(d)", "Result unknown  \(d)"))
        case .notConnected(let d): line(tr("未连接  \(d)  没有发出", "Not connected  \(d)  Not sent"))
        case .disabled(let reason): line(reason)
        }
    }
    private func line(_ s: String, tone: Color = Theme.ink2) -> some View {
        Text(s).font(Theme.Font.f14).foregroundStyle(tone).fixedSize(horizontal: false, vertical: true)
    }
}

/// 抽屉顶部的两个标签：给你的信 / 待批权限。
struct InboxTabs: View {
    @ObservedObject var desk: DeskModel
    var body: some View {
        HStack(spacing: 8) {
            BoxButton(title: tr("给你的信", "Letters for you"), primary: desk.inboxTab == .letters) { desk.inboxTab = .letters }
            BoxButton(title: tr("待批权限", "Pending approvals"), primary: desk.inboxTab == .requests) { desk.inboxTab = .requests }
        }
    }
}

/// 待批权限：数据现在来自夹具（R1 request.list 没上线），所以顶上一个"模拟数据"。决定走真的 request.decide；夹具里的假申请不能写，按钮禁用。
struct RequestList: View {
    @ObservedObject var desk: DeskModel
    var body: some View {
        PanelBody(panel: desk.requests) { page in
            VStack(alignment: .leading, spacing: 0) {
                if desk.isFixture { Chip(text: tr("模拟数据", "Sample data"), style: .dashed).padding(.bottom, 12) }
                if page.items.isEmpty { Text(tr("0 条", "0 items")).font(Theme.Font.body).foregroundStyle(Theme.ink3) }
                ForEach(page.items) { RequestRow(desk: desk, request: $0) }
                if !page.complete { Text(tr("还有下一页，只读到 \(page.items.count) 条", "More pages exist; read \(page.items.count) only")).font(Theme.Font.small).foregroundStyle(Theme.ink2).padding(.top, 10) }
            }
        }
    }
}

/// 后端码 → 中文；认得的才译，不认得的原样显示。
enum Labels {
    static func requestKind(_ k: String) -> String {
        switch k { case "grant": tr("授权", "Grant"); case "holder": tr("岗位持有人", "Position holder"); case "project": tr("项目", "Project"); default: k }
    }
    /// 提交状态：认得的才译，不认得的原样显示。
    static func submissionState(_ s: String) -> String {
        switch s { case "effective": tr("生效", "Effective"); default: s }
    }
    /// 申请项码（后端：`project:create`、`grant:<范围>`、`holder:@岗位`）→ 一句话；认不得的原样。
    static func requestItem(_ code: String) -> String {
        let parts = code.split(separator: ":", maxSplits: 1).map(String.init)
        guard parts.count == 2 else { return requestKind(code) }
        switch parts[0] {
        case "project": return parts[1] == "create" ? tr("创建项目", "Create project") : code
        case "grant": return tr("授权 ", "Grant ") + parts[1]
        case "holder": return tr("岗位持有人 ", "Position holder ") + parts[1]
        default: return code
        }
    }
    static func quota(_ q: String) -> String {
        switch q { case "exhausted": tr("额度用完", "Quota exhausted"); default: q }
    }
}

/// 结果未知被收起来的那一笔：入口旁边留一个词，点同一个入口接着用同一个键重试。
struct HeldMark: View {
    @ObservedObject var desk: DeskModel
    let key: String
    var body: some View {
        if desk.book.isHeld(key) { Chip(text: tr("上一笔结果未知", "Previous result unknown"), style: .dashed) }
    }
}

private struct RequestRow: View {
    @ObservedObject var desk: DeskModel
    let request: PendingRequest

    var body: some View {
        let r = request
        let key = "decide:" + r.ref
        let canApprove = r.bossCanApprove == true           // nil = 后端没说：不画按钮，也不写"批不了"
        let blocked = desk.fixtureBlocksWrite(.decide(ref: r.ref, version: r.version, decision: .approve))
        let finished: Bool = { if case .done? = desk.actions[key]?.phase { return true }; return false }()
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 10) {
                Text(r.ref).font(Theme.Font.mono.weight(.semibold))
                Chip(text: Labels.requestKind(r.kind))
                Chip(text: r.state == "pending" ? tr("待批", "Pending") : r.state, style: r.state == "pending" && canApprove && !finished ? .red : .plain)
                Spacer(minLength: 0)
                Text(tr("版本 \(r.version)", "Version \(r.version)")).font(Theme.Font.small).foregroundStyle(Theme.ink3).monospacedDigit()
            }
            let position = r.position == r.target ? nil : r.position
            Text(desk.seatName(r.applicant) + (r.target.map { " → " + desk.seatName($0) } ?? "") + (position.map { "  " + $0 } ?? ""))
                .font(Theme.Font.bodyBold).lineLimit(2)
            if !r.reason.isEmpty { Text(r.reason).font(Theme.Font.f14).foregroundStyle(Theme.ink2).fixedSize(horizontal: false, vertical: true) }
            if !r.details.isEmpty { Text(r.details.joined(separator: " · ")).font(Theme.Font.small).foregroundStyle(Theme.ink2) }
            if let body = r.body {
                DisclosureGroup(tr("正文", "Body")) {
                    Text(body).font(Theme.Font.f14).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
            // 多项申请：已批的和还差的分开写；后端没给 approved = 不写"已批"，也不猜还差哪几项。
            let items = r.items ?? [], done = r.approved ?? []
            let waiting = items.filter { !done.contains($0) }
            if !items.isEmpty {
                if done.isEmpty { Text(items.map(Labels.requestItem).joined(separator: "、") + (r.bossCanApprove == false ? tr("（你批不了）", "(you cannot approve)") : "")).font(Theme.Font.small).foregroundStyle(Theme.ink3).lineLimit(2) }
                else {
                    Text(tr("已批 ", "Approved ") + done.map(Labels.requestItem).joined(separator: "、")).font(Theme.Font.small).foregroundStyle(Theme.ink3).lineLimit(2)
                    if !waiting.isEmpty { Text(tr("还差 ", "Waiting ") + waiting.map(Labels.requestItem).joined(separator: "、") + (r.bossCanApprove == false ? tr("（你批不了）", "(you cannot approve)") : "")).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink2).lineLimit(2) }
                }
            } else if !done.isEmpty {
                Text(tr("已批 ", "Approved ") + done.map(Labels.requestItem).joined(separator: "、")).font(Theme.Font.small).foregroundStyle(Theme.ink3).lineLimit(2)
            }
            if canApprove && desk.actions[key] == nil {
                HStack(spacing: 8) {
                    BoxButton(title: tr("同意", "Approve"), disabled: blocked) {
                        desk.beginAction(key, .decide(ref: r.ref, version: r.version, decision: .approve))
                    }
                    BoxButton(title: tr("拒绝", "Refuse"), disabled: blocked) {
                        desk.beginAction(key, .decide(ref: r.ref, version: r.version, decision: .refuse))
                    }
                    HeldMark(desk: desk, key: key)
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
                .padding(.top, 4)
            }
            ActionBox(desk: desk, key: key)
        }
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) { Rule() }
    }
}

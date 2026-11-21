import SwiftUI
import OfficeClient

/// 右侧抽屉（440 宽，1px ink 左线，无模态框）：待办 / 项目 / Work 详情 / 席位信箱；动作（回信、写信、审核）以内联面板出现。
struct DrawerView: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 16) {
                Text(title).font(Theme.Font.blockTitle).tracking(Theme.Track.blockTitle).lineLimit(1)
                if case .inbox = desk.drawer { InboxTabs(desk: desk) }
                Spacer(minLength: 0)
                BoxButton(title: tr("关闭", "Close")) { desk.drawer = nil }
            }
            .padding(.leading, Theme.drawerPadLeading).padding(.trailing, Theme.drawerPad).padding(.vertical, Theme.drawerPad)
            .frame(minHeight: 72)
            Rule(color: Theme.ink)
            ScrollView(showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) { content }
                    .padding(.horizontal, 24).padding(.top, 22).padding(.bottom, 40)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            if let footer { Rule(color: Theme.ink); Text(footer).font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.horizontal, 24).padding(.vertical, 14) }
        }
        .background(Theme.paper)
    }

    private var title: String {
        switch desk.drawer {
        case .inbox: tr("待办", "To do")
        case .project(let ref): desk.projects.value?.first { $0.ref == ref }?.name ?? ref
        case .work(let ref): ref
        case .seat(let addr): desk.seatName(addr)
        case nil: ""
        }
    }

    private var footer: String? {
        guard case .inbox = desk.drawer, desk.inboxTab == .letters, let box = desk.inbox.value else { return nil }
        let shown = box.letters.filter { !$0.isRequestNotice }       // 申请类的信只在"待批权限"里
        let more = !box.complete
        return tr("要回复 \(bounded(box.waiting, atLeast: more)) · 其他 \(bounded(shown.count - box.waiting, atLeast: more))",
                  "To reply \(bounded(box.waiting, atLeast: more)) · Other \(bounded(shown.count - box.waiting, atLeast: more))")
    }

    @ViewBuilder private var content: some View {
        switch desk.drawer {
        case .inbox where desk.inboxTab == .requests:
            RequestList(desk: desk)
        case .inbox:
            PanelBody(panel: desk.inbox) { box in
                let shown = box.letters.filter { !$0.isRequestNotice }
                if shown.isEmpty { Text(tr("0 封", "0 letters")).font(Theme.Font.body).foregroundStyle(Theme.ink3) }
                if !box.complete { Text(tr("只读到 \(box.letters.count) 封，后端共 \(box.count) 封", "Read \(box.letters.count) \(pl(box.letters.count, "letter")); the office has \(box.count)")).font(Theme.Font.small).foregroundStyle(Theme.ink2).padding(.bottom, 8) }
                ForEach(shown) { LetterLine(desk: desk, letter: $0, holders: desk.holders) }
            }
        case .project(let ref):
            PanelBody(panel: desk.projects) { ps in
                if let p = ps.first(where: { $0.ref == ref }) { ProjectDetailView(desk: desk, project: p) }
                else { Text(tr("未知  项目不在当前读到的列表里", "Unknown  project not in the list read")).font(Theme.Font.body).foregroundStyle(Theme.ink2) }
            }
        case .work(let ref):
            PanelBody(panel: desk.workDetails[ref] ?? .loading) { WorkDetailView(desk: desk, detail: $0) }
        case .seat(let addr):
            SeatDrawer(desk: desk, addr: addr).id(addr)
        case nil: EmptyView()
        }
    }
}

// MARK: - 抽屉里的小件

private struct Sec: View {
    let title: String
    var count: String? = nil
    var body: some View {
        VStack(spacing: 0) {
            Rule(color: Theme.ink)
            HStack { Text(title); Spacer(); if let count { Text(count).monospacedDigit() } }
                .font(Theme.Font.f14Bold).padding(.top, 8)
        }
        .padding(.top, 24).padding(.bottom, 10)
    }
}

private struct KV: View {
    let k: String
    let v: String
    var mono = false
    var body: some View {
        if !v.trimmingCharacters(in: .whitespaces).isEmpty {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text(k).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3).frame(width: 92, alignment: .leading)
                Text(v).font(mono ? Theme.Font.mono : Theme.Font.f14).textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.vertical, 3)
        }
    }
}

private func shortTime(_ s: String) -> String {
    // "2026-11-21 02:11:12" → "11-21 02:11"
    let parts = s.split(separator: " ")
    guard parts.count >= 2, parts[0].count == 10, parts[1].count >= 5 else { return s }
    return String(parts[0].dropFirst(5)) + " " + String(parts[1].prefix(5))
}

private struct LetterLine: View {
    @ObservedObject var desk: DeskModel
    let letter: LetterRow
    let holders: [String: [String]]
    var target: String? = nil
    var canReply = true
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 12) {
                PersonName(id: letter.from, holders: holders, context: .person)
                Spacer(minLength: 0)
                Text(shortTime(letter.createdAt)).font(Theme.Font.small.weight(.regular)).monospacedDigit().foregroundStyle(Theme.ink3)
            }
            Button { expanded.toggle() } label: {
                HStack(spacing: 6) {
                    Image(systemName: expanded ? "chevron.down" : "chevron.right").font(Theme.Font.small)
                    Text(letter.subject).font(Theme.Font.f14).lineLimit(1)
                }
                .foregroundStyle(Theme.ink2).frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }
            .buttonStyle(.plain).focusRing()
            HStack(spacing: 8) {
                if let kind = Self.kind(letter) { Chip(text: kind, style: letter.needsReply && canReply ? .red : .plain) }      // 红只给"等老板回"的信；别人信箱里的不红
                if letter.system { Chip(text: tr("系统", "System")) }
                if letter.letterClass != "notice" {
                    Text(letter.accepted.map { $0 ? tr("已受理", "Accepted") : tr("未受理", "Not accepted") } ?? tr("受理 未知", "Acceptance unknown")).font(Theme.Font.small).foregroundStyle(Theme.ink2)
                }
            }
            .padding(.top, 4)
            if expanded { LetterBody(reads: desk.reads, letterID: letter.id, target: target).padding(.top, 8) }
            let key = "reply:" + letter.id
            let archiveKey = "archive:" + letter.id
            if canReply {
                HStack(spacing: 8) {
                    if letter.needsReply && !letter.system && desk.actions[key] == nil {
                        BoxButton(title: tr("回信", "Reply")) {
                            desk.beginAction(key, .reply(to: letter.from, replyTo: letter.id), subject: "Re: " + letter.subject)
                        }
                        HeldMark(desk: desk, key: key)
                    }
                    if desk.actions[archiveKey] == nil {
                        BoxButton(title: tr("归档", "Archive")) {
                            desk.beginAction(archiveKey, .archive(letterID: letter.id))
                            desk.confirmAction(archiveKey)
                        }
                        HeldMark(desk: desk, key: archiveKey)
                    }
                }.padding(.top, 6)
            }
            ActionBox(desk: desk, key: key)
            ActionBox(desk: desk, key: archiveKey)
        }
        .padding(.vertical, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .bottom) { Rule() }
    }

    /// 只给认得的类；不认识的类不画标签（不默认成"告知"）。
    static func kind(_ l: LetterRow) -> String? {
        if l.needsReply { return tr("要回复", "To reply") }
        switch l.letterClass { case "notice": return tr("告知", "FYI"); case "receipt": return tr("回执", "Receipt"); default: return nil }
    }
}

private struct LetterBody: View {
    let reads: OfficeReads
    let letterID: String
    let target: String?
    @State private var page: Panel<LetterPage> = .loading
    @State private var cursors = [1]
    @State private var digest: String?
    @State private var request = 0

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            PanelBody(panel: page) { current in
                if cursors.count > 1 || current.nextCursor != nil {
                    HStack(spacing: 8) {
                        if cursors.count > 1 {
                            BoxButton(title: tr("上一页", "Previous page")) {
                                cursors.removeLast()
                                load()
                            }
                        }
                        if let next = current.nextCursor {
                            BoxButton(title: tr("下一页", "Next page")) {
                                cursors.append(next)
                                load()
                            }
                        }
                    }
                }
                Text(verbatim: current.text).font(Theme.Font.body).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if page.value == nil, case .done = page {
                BoxButton(title: tr("重新读取", "Read again"), action: load)
            }
        }
        .task(id: request) {
            let cursor = cursors.last
            let result = await Task.detached { reads.letterPage(letterID, target: target, cursor: cursor) }.value
            guard !Task.isCancelled else { return }
            if case .ok(let incoming) = result {
                if let digest, digest != incoming.digest {
                    cursors = [1]
                    self.digest = nil
                    page = .done(.unknown(detail: tr("正文已变化，请重新读取", "The letter changed. Read it again.")))
                    return
                }
                digest = incoming.digest
            }
            page = .done(result)
        }
    }

    private func load() {
        page = .loading
        request += 1
    }
}

/// 席位抽屉：这个地址的信箱（letter.read 带 target，老板可直读）+ 写信（letter.send）。
private struct SeatDrawer: View {
    @ObservedObject var desk: DeskModel
    let addr: String
    var body: some View {
        let key = "compose:" + addr
        // 抽屉头已经写了名字；这里只在地址和名字不一样时补一行地址，同一个名字不写第二遍。
        if addr != desk.seatName(addr) { Text(addr).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3).padding(.bottom, 8) }
        if desk.actions[key] == nil {
            HStack(spacing: 8) {
                BoxButton(title: tr("写信", "Write")) {
                    desk.beginAction(key, .compose(to: addr))
                }
                HeldMark(desk: desk, key: key)
            }
        }
        ActionBox(desk: desk, key: key)
        let panel = desk.seatMail[addr] ?? .loading
        Sec(title: tr("信箱", "Mailbox"), count: panel.value.map { b in "\(b.letters.count)" } ?? tr("未知", "Unknown"))
        PanelBody(panel: panel) { box in
            if box.letters.isEmpty { Text(tr("0 封", "0 letters")).font(Theme.Font.body).foregroundStyle(Theme.ink3) }
            if !box.complete { Text(tr("只读到 \(box.letters.count) 封，后端共 \(box.count) 封", "Read \(box.letters.count) \(pl(box.letters.count, "letter")); the office has \(box.count)")).font(Theme.Font.small).foregroundStyle(Theme.ink2).padding(.bottom, 8) }
            ForEach(box.letters) { LetterLine(desk: desk, letter: $0, holders: desk.holders, target: addr, canReply: false) }
        }
    }
}

private struct ProjectDetailView: View {
    @ObservedObject var desk: DeskModel
    let project: ProjectDetail

    var body: some View {
        Text(project.ref).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3)
        Text(project.name).font(Theme.Font.h3).tracking(-0.52).padding(.top, 4).padding(.bottom, 8).fixedSize(horizontal: false, vertical: true)
        group(tr("经理", "Managers"), project.managers)
        group(tr("岗位", "Positions"), project.positions)
        group(tr("参与人", "Participants"), project.participants)
        let works = desk.work.value?.items.filter { $0.projectRef == project.ref } ?? []
        Sec(title: "Work", count: desk.work.value == nil ? tr("未知", "Unknown") : "\(works.count)")
        ForEach(works) { w in
            Button { desk.openWork(w.ref) } label: {
                HStack(spacing: 10) {
                    WorkMark(state: w.state)
                    Text(w.ref).font(Theme.Font.mono)
                    Text(w.state.displayName).font(Theme.Font.small).foregroundStyle(Theme.ink2)
                    Spacer()
                }
                .padding(.vertical, 6).contentShape(Rectangle())
            }
            .buttonStyle(.plain).focusRing()
        }
    }

    @ViewBuilder private func group(_ title: String, _ rows: [Party]) -> some View {
        Sec(title: title, count: "\(rows.count)")
        ForEach(rows) { p in
            VStack(alignment: .leading, spacing: 4) {
                if p.to.hasPrefix("employee-") {     // 个人：标题是岗位名 / 项目经理 / 个人身份，完整 id 只在这里出现
                    PersonName(id: p.to, holders: desk.holders, context: title == tr("经理", "Managers") ? .manager : .person, full: true)
                } else {
                    Text(p.to).font(Theme.Font.bodyBold).lineLimit(2)
                }
                if !p.roles.isEmpty { HStack(spacing: 8) { ForEach(p.roles, id: \.self) { Chip(text: $0) } } }
            }
            .padding(.vertical, 6).frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct WorkDetailView: View {
    @ObservedObject var desk: DeskModel
    let detail: WorkDetail
    var body: some View {
        let w = detail.row
        Text(w.objective).font(Theme.Font.h3Small).tracking(-0.4).padding(.top, 4).padding(.bottom, 10)
            .lineLimit(6).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
        KV(k: tr("状态", "State"), v: "\(w.state.number) \(w.state.displayName)")
        KV(k: tr("承办", "Assignee"), v: w.assignee)
        KV(k: tr("审核", "Reviewer"), v: w.reviewer + (detail.reviewRounds.map { tr(" · \($0) 轮", " · \($0) \(pl($0, "round"))") } ?? ""))
        KV(k: tr("创建", "Created"), v: shortTime(detail.createdAt))
        if !detail.acceptance.trimmingCharacters(in: .whitespaces).isEmpty {
            Sec(title: tr("验收", "Acceptance"))
            Text(detail.acceptance).font(Theme.Font.f14).lineSpacing(3).textSelection(.enabled)
        }
        relationsSection
        provenance
        usageSection
        submission(tr("当前提交", "Current submission"), detail.current)
        review
        submission(tr("最终提交", "Final submission"), detail.final)
    }

    /// 接续：我指向谁（等待 / 依据 / …）+ 谁指向我（谁在等我）。后端没给 = 未知，不写"没有"。点编号打开那一件。
    @ViewBuilder private var relationsSection: some View {
        Sec(title: tr("接续", "Relations"))
        if detail.relations == nil && detail.reversed == nil { Text(tr("未知", "Unknown")).font(Theme.Font.f14).foregroundStyle(Theme.ink2) }
        else if (detail.relations ?? []).isEmpty && (detail.reversed ?? []).isEmpty && detail.relations != nil && detail.reversed != nil {
            Text(tr("无", "None")).font(Theme.Font.f14).foregroundStyle(Theme.ink3)
        }
        ForEach(detail.relations ?? [], id: \.self) { r in relationRow(Self.forwardLabel(r.kind), r.target) }
        ForEach(detail.reversed ?? [], id: \.self) { r in relationRow(Self.reverseLabel(r.kind), r.target) }
        if detail.reversed == nil { Text(tr("谁指向这件：未知", "Who points here: unknown")).font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.top, 4) }
    }
    private func relationRow(_ label: String, _ target: String) -> some View {
        Button { desk.openWork(target) } label: {
            HStack(alignment: .firstTextBaseline, spacing: 14) {
                Text(label).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3).frame(width: 92, alignment: .leading)
                Text(target).font(Theme.Font.mono)
                Spacer(minLength: 0)
            }
            .padding(.vertical, 3).contentShape(Rectangle())
        }
        .buttonStyle(.plain).focusRing()
    }
    static func forwardLabel(_ kind: String) -> String {
        switch kind {
        case "waits_for": tr("等待", "Waits for")
        case "belongs_to": tr("属于", "Belongs to")
        case "based_on": tr("依据", "Based on")
        case "modifies": tr("修改", "Modifies")
        case "references": tr("参考", "References")
        default: kind
        }
    }
    /// 反向：别的 Work 指向这件。
    static func reverseLabel(_ kind: String) -> String {
        switch kind {
        case "waits_for": tr("被等待", "Awaited by")
        case "belongs_to": tr("下属", "Has child")
        case "modifies": tr("被修改", "Modified by")
        case "based_on": tr("被依据", "Basis of")
        default: kind
        }
    }

    /// 这件活的用量：work.get 的用量卡（承办 / 审核各一行，下面按模型拆）。没给卡 = 未知；covered=false = 无记录。
    @ViewBuilder private var usageSection: some View {
        let (card, simulated) = desk.usageCard(detail)
        Sec(title: tr("用量", "Usage"))
        if simulated { Chip(text: tr("模拟数据", "Sample data"), style: .dashed).padding(.bottom, 6) }
        if card == nil { Text(tr("未知", "Unknown")).font(Theme.Font.f14).foregroundStyle(Theme.ink2) }
        else if card?.covered == false { Text(tr("无记录", "No records")).font(Theme.Font.f14).foregroundStyle(Theme.ink3) }
        else if let c = card {
            roleUsage(tr("承办", "Assignee"), c.assignee)
            roleUsage(tr("审核", "Reviewer"), c.reviewer)
            if let t = c.total { UsageLine(label: tr("合计", "Total"), cell: t) }
        }
    }
    @ViewBuilder private func roleUsage(_ label: String, _ r: RoleUsage?) -> some View {
        UsageLine(label: label, cell: r?.cell)
        if let ms = r?.byModel {
            ForEach(Array(ms.enumerated()), id: \.offset) { _, m in
                UsageLine(label: "", cell: m.cell, caption: m.model ?? tr("模型未知", "Model unknown"))
            }
        } else if r != nil {
            Text(tr("模型未知（后端没按模型拆）", "Model unknown (not split by model)")).font(Theme.Font.sub).foregroundStyle(Theme.ink3).padding(.leading, 106)
        }
    }

    /// 来历链：这件依据 / 修改了谁，那些又依据 / 修改了谁……（用页上已经读到的详情往上走；没读到的就停在那一件）。点编号打开。
    @ViewBuilder private var provenance: some View {
        let rows = chain(from: detail.row.ref)
        Sec(title: tr("来历链", "Provenance"), count: "\(rows.count)")
        if rows.isEmpty { Text(detail.relations == nil ? tr("未知", "Unknown") : tr("无来源", "No source")).font(Theme.Font.f14).foregroundStyle(Theme.ink3) }
        ForEach(Array(rows.enumerated()), id: \.offset) { _, r in
            Button { desk.openWork(r.ref) } label: {
                HStack(alignment: .firstTextBaseline, spacing: 10) {
                    Text(r.kind == .basedOn ? tr("依据", "Based on") : tr("修改", "Modifies")).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3)
                        .frame(width: 44, alignment: .leading)
                    Text(r.ref).font(Theme.Font.mono)
                    Text(r.who).font(Theme.Font.small).foregroundStyle(Theme.ink3).lineLimit(1)
                    Spacer(minLength: 0)
                }
                .padding(.leading, CGFloat(r.depth) * 16).padding(.vertical, 3).contentShape(Rectangle())
            }
            .buttonStyle(.plain).focusRing()
        }
    }
    private func chain(from ref: String) -> [(depth: Int, kind: WorkEdge.Kind, ref: String, who: String)] {
        var out: [(depth: Int, kind: WorkEdge.Kind, ref: String, who: String)] = []
        var seen: Set<String> = [ref]
        func walk(_ r: String, _ depth: Int) {
            guard let d = desk.workDetails[r]?.value else { return }
            for rel in d.relations ?? [] {
                guard let k = WorkEdge.Kind(rawValue: rel.kind), !seen.contains(rel.target) else { continue }
                seen.insert(rel.target)
                let src = desk.workDetails[rel.target]?.value
                out.append((depth, k, rel.target, src.map { $0.row.assignee } ?? ""))
                walk(rel.target, depth + 1)
            }
        }
        walk(ref, 0)
        return out
    }

    /// 审核入口：只有后端对当前身份说 available_actions 含 review、Work 在待审、且有当前提交时才出现。
    @ViewBuilder private var review: some View {
        if let sub = detail.reviewable {
            let key = "review:" + detail.row.ref
            if desk.actions[key] == nil {
                HStack(spacing: 8) {
                    BoxButton(title: tr("通过", "Accept")) {
                        desk.beginAction(key, .review(workRef: detail.row.ref, subRef: sub.ref, decision: .accept))
                    }
                    BoxButton(title: tr("退回", "Return")) {
                        desk.beginAction(key, .review(workRef: detail.row.ref, subRef: sub.ref, decision: .return))
                    }
                    HeldMark(desk: desk, key: key)
                }
                .padding(.top, 10)
            }
            ActionBox(desk: desk, key: key)
        }
    }

    @ViewBuilder private func submission(_ k: String, _ s: SubmissionSummary?) -> some View {
        Sec(title: k, count: s == nil ? tr("无", "None") : nil)
        if let s {
            HStack { Text(s.ref).font(Theme.Font.mono); Spacer(); Chip(text: Labels.submissionState(s.state)) }
            Text(shortTime(s.createdAt)).font(Theme.Font.small.weight(.regular)).monospacedDigit().foregroundStyle(Theme.ink3).padding(.top, 2)
            if !s.note.isEmpty { Text(s.note).font(Theme.Font.f14).lineSpacing(3).padding(.top, 6).textSelection(.enabled) }
        }
    }
}

/// 一个角色格的 token：合计 + 入 / 出 / 缓存；缺的分量写"未知"。nil = 这个角色没有记录。
struct UsageLine: View {
    let label: String
    let cell: UsageCell?
    var caption: String? = nil
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 14) {
            Text(label).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3).frame(width: 92, alignment: .leading)
            if let c = cell {
                VStack(alignment: .leading, spacing: 2) {
                    if let caption { Text(caption).font(Theme.Font.sub.weight(.semibold)).foregroundStyle(Theme.ink2) }
                    Text(c.inOut.map { TokenFmt.short($0) + " token" } ?? tr("未知", "Unknown")).font(Theme.Font.f14Bold).monospacedDigit()
                    Text(tr("入 \(f(c.tokensIn)) · 出 \(f(c.tokensOut)) · 缓存 \(f(c.tokensCache)) · 费用 \(Cost.text(c.cost))", "In \(f(c.tokensIn)) · Out \(f(c.tokensOut)) · Cache \(f(c.tokensCache)) · Cost \(Cost.text(c.cost))"))
                        .font(Theme.Font.sub).monospacedDigit().foregroundStyle(Theme.ink3)
                }
            } else { Text(tr("无记录", "No records")).font(Theme.Font.f14).foregroundStyle(Theme.ink3) }
        }
        .padding(.vertical, 3)
    }
    private func f(_ n: Int?) -> String { n.map(TokenFmt.short) ?? tr("未知", "Unknown") }
}

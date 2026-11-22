import SwiftUI
import OfficeClient

/// Studio：老板 → 项目（01 02 03）→ 经理 / 岗位。事实全部来自 office.lookup（项目明细）与 work.list，不自己推。
/// 版式取自设计稿 A-组织：68 标题、76 窄体编号、直角连线、红色主块只表示"有信在等你"。
struct OrgPage: View {
    @ObservedObject var desk: DeskModel
    private let colSpacing: CGFloat = 40
    static let colWidth: CGFloat = 310

    var body: some View {
        PageScaffold {
                root
                switch desk.projects {
                case .done(.ok(let projects)) where !projects.isEmpty:
                    // 老板灯往下的竖线接到第一列的方块
                    Color.clear.frame(height: 56)
                        .overlay(alignment: .topLeading) {
                            Rectangle().fill(Theme.wire).frame(width: Theme.Line.outline, height: 56 + 14).padding(.leading, 4.75)
                        }
                    let rows = stride(from: 0, to: projects.count, by: 4).map { Array(projects[$0..<min($0 + 4, projects.count)]) }
                    VStack(alignment: .leading, spacing: 56) {
                        ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                            HStack(alignment: .top, spacing: colSpacing) {
                                ForEach(Array(row.enumerated()), id: \.element.id) { i, p in
                                    ProjectColumn(desk: desk, project: p, number: number(of: p, in: projects), joinsRail: i > 0, spacing: colSpacing)
                                        .frame(width: Self.colWidth, alignment: .topLeading)
                                }
                                Spacer(minLength: 0)
                            }
                        }
                    }
                default:
                    PanelBody(panel: desk.projects) { _ in Text(tr("0 个项目", "0 projects")).font(Theme.Font.body).foregroundStyle(Theme.ink3) }
                        .padding(.top, 40).padding(.leading, 30)
                }
                UnassignedBlock(desk: desk).padding(.top, 48).padding(.leading, 30)
                ReadAt(desk: desk, what: tr("项目", "Projects"), fact: desk.projects.fact).padding(.top, 48).padding(.leading, 30)
        }
    }

    private func number(of p: ProjectDetail, in all: [ProjectDetail]) -> String {
        String(format: "%02d", (all.firstIndex(of: p) ?? 0) + 1)
    }

    // MARK: 顶部：老板 · 标题 · 统计 · 红块
    private var titleBlock: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Studio").font(Theme.Font.viewTitle).tracking(Theme.Track.viewTitle).padding(.vertical, -6).lineLimit(1).fixedSize()
            (Text(desk.whoami.text { $0 }).font(Theme.Font.rowTitle).foregroundStyle(Theme.ink)
             + Text(tr(" · 老板 · 全部项目", " · Boss · all projects")).font(Theme.Font.body.weight(.medium)).foregroundStyle(Theme.ink2))
                .lineLimit(1).fixedSize()
        }
        .padding(.leading, 30)
    }

    private var root: some View {
        ViewThatFits(in: .horizontal) {
            HStack(alignment: .bottom, spacing: 44) {
                titleBlock
                Spacer(minLength: 0)
                stats.padding(.bottom, 4)
                WaitingBlock(panel: desk.inbox) { desk.drawer = .inbox }
            }
            // 窗口窄时：标题一行，统计和红块换到下面，不让标题被挤成一字一行。
            VStack(alignment: .leading, spacing: 22) {
                titleBlock
                HStack(alignment: .bottom, spacing: 32) {
                    stats
                    Spacer(minLength: 0)
                    WaitingBlock(panel: desk.inbox) { desk.drawer = .inbox }
                }
                .padding(.leading, 30)
            }
        }
        .overlay(alignment: .topLeading) {
            GeometryReader { g in
                ZStack(alignment: .topLeading) {
                    Rectangle().fill(Theme.wire).frame(width: Theme.Line.outline, height: max(0, g.size.height - 33.5)).offset(x: 4.75, y: 33.5)
                    Lamp(kind: .human).offset(y: 28)
                }
            }
        }
    }

    @ViewBuilder private var stats: some View {
        HStack(alignment: .bottom, spacing: Theme.statGap) {
            if let ps = desk.projects.value {
                Stat(value: "\(ps.count)", label: tr("项目", "Projects"))
                Stat(value: "\(ps.reduce(0) { $0 + $1.positions.count })", label: tr("岗位", "Positions"))
            } else {
                Stat(value: tr("未知", "Unknown"), label: tr("项目", "Projects"))
            }
            if let w = desk.work.value {
                Stat(value: bounded(w.items.filter { $0.state == .active }.count, atLeast: w.hasMore), label: tr("进行", "Active"))
                Stat(value: bounded(w.items.filter { $0.state == .submitted }.count, atLeast: w.hasMore), label: tr("待审", "In review"))
            } else {
                Stat(value: tr("未知", "Unknown"), label: tr("工作", "Work"))
            }
            Stat(value: tr("未知", "Unknown"), label: tr("正在干活", "Working"), muted: true)      // 没有后端事实源（裁决 D3）；只在这里写一次
        }
    }
}

// MARK: - 项目列
private struct ProjectColumn: View {
    @ObservedObject var desk: DeskModel
    let project: ProjectDetail
    let number: String
    let joinsRail: Bool
    let spacing: CGFloat

    var body: some View {
        let hasRows = !project.managers.isEmpty || !project.positions.isEmpty
        VStack(alignment: .leading, spacing: 0) {
            Button { desk.drawer = .project(project.ref) } label: { head(hasRows: hasRows) }.buttonStyle(.plain).focusRing()
            // 按 Core 返回的授权地址和岗位地址平铺；只合并完全相同的地址。
            let seatsList = ProjectSeats.entries(project)
            let threads = desk.hosts.value?.threads ?? []
            ForEach(Array(seatsList.enumerated()), id: \.element.id) { i, s in
                SeatRow(desk: desk, entry: s, isLast: i == seatsList.count - 1,
                        threads: threads.filter { $0.carries(s.party.to) })
            }
        }
    }

    private func head(hasRows: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(number).font(Theme.Font.sectionNum).tracking(Theme.Track.sectionNum).padding(.vertical, -9).padding(.top, 34)   // 横轨走在数字上方，不压字
            Text(project.name.isEmpty ? project.ref : project.name).font(Theme.Font.blockTitle).tracking(-0.33)
                .lineLimit(2).padding(.top, 12)
            Text(tr("\(project.ref) · 经理 \(project.managers.count) · 岗位 \(project.positions.count)", "\(project.ref) · Managers \(project.managers.count) · Positions \(project.positions.count)"))
                .font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.top, 3)
        }
        .padding(.leading, 24).padding(.bottom, 14)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(alignment: .topLeading) {
            ZStack(alignment: .topLeading) {
                if joinsRail {   // 横轨：从左边一列的方块中心接过来
                    Rectangle().fill(Theme.wire).frame(width: OrgPage.colWidth + spacing, height: Theme.Line.outline)
                        .offset(x: -(OrgPage.colWidth + spacing) + 5.5, y: 18.75)
                }
                Square(filled: true).offset(y: 14)
                if hasRows {
                    GeometryReader { g in
                        Rectangle().fill(Theme.wire).frame(width: Theme.Line.outline, height: max(0, g.size.height - 25)).offset(x: 4.75, y: 25)
                    }
                }
            }
        }
        .contentShape(Rectangle())
    }
}

// MARK: - 席位行（灯 + 名 + 事实），自带连线；所有席位同一级
private struct SeatRow: View {
    @ObservedObject var desk: DeskModel
    let entry: SeatEntry
    let isLast: Bool
    let threads: [HostThread]

    var body: some View {
        let lampLeft: CGFloat = 24, lineX: CGFloat = 5.5
        let party = entry.party
        let mailbox = party.to
        let personalMailboxes = Set(threads.flatMap(\.bindings)
            .filter { $0.position == nil && $0.actor == party.holder && $0.mailbox != mailbox }.map(\.mailbox)).sorted()
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Button { desk.openSeat(party.to) } label: {
                    Text(party.to).font(Theme.Font.rowTitle).tracking(-0.17).lineLimit(1).help(party.to)
                }
                .buttonStyle(.plain).focusRing()
                if entry.isManager { Text(tr("经理", "Manager")).font(Theme.Font.small).foregroundStyle(Theme.ink3) }
                if party.isVacant { Text(tr("空缺", "Vacant")).font(Theme.Font.small).foregroundStyle(Theme.ink3) }
                ForEach(threads) { thread in
                    let host = hostName(thread.harness)
                    Button { desk.showOnHostPage(thread.id) } label: {
                        Text(host).font(Theme.Font.small).underline()
                    }
                    .buttonStyle(.plain).focusRing()
                    .help(tr("在宿主页查看 \(host) 会话", "View \(host) session on Hosts"))
                }
            }
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 12) {
                    if let active = party.activeWorkCount {
                        Text(tr("进行中 Work \(active)", "Active Work \(active)"))
                        if let submitted = party.submittedWorkCount, submitted > 0 { Text(tr("待审 \(submitted)", "In review \(submitted)")) }
                    } else { Text(tr("进行中 Work 未知", "Active Work unknown")) }
                }
                Text(deliveryText(desk.deliveryPending(mailbox)))
            }
            .font(Theme.Font.small).foregroundStyle(Theme.ink3)
            ForEach(personalMailboxes, id: \.self) { address in
                if let count = desk.deliveryPending(address), count > 0 {
                    Button { desk.openSeat(address) } label: {
                        Text(tr("个人待投递 \(count)", "Personal delivery pending: \(count)"))
                            .font(Theme.Font.small).foregroundStyle(Theme.ink3).underline()
                    }
                    .buttonStyle(.plain).focusRing()
                }
            }
        }
        .padding(.leading, lampLeft + 22).padding(.vertical, 9)
        .frame(maxWidth: .infinity, minHeight: 56, alignment: .leading)
        .overlay {
            GeometryReader { g in
                let mid = g.size.height / 2
                ZStack(alignment: .topLeading) {
                    Rectangle().fill(Theme.wire).frame(width: Theme.Line.outline, height: isLast ? mid : g.size.height).offset(x: lineX - 0.75)
                    Rectangle().fill(Theme.wire).frame(width: lampLeft - lineX, height: Theme.Line.outline).offset(x: lineX, y: mid - 0.75)
                    Lamp(kind: .unknown).offset(x: lampLeft, y: mid - Theme.marker / 2)
                        .help(tr("正在干活：未知（后端没有这个事实）", "Working: unknown (the office has no such fact)"))
                }
            }
            .allowsHitTesting(false)
        }
    }
}


/// 已登记宿主会话中未关联任何项目席位的成员。
private struct UnassignedBlock: View {
    @ObservedObject var desk: DeskModel
    var body: some View {
        let panel = desk.unassigned
        VStack(alignment: .leading, spacing: 0) {
            Button { desk.unassignedOpen.toggle() } label: {
                HStack(spacing: 12) {
                    Text(desk.unassignedOpen ? "−" : "+").font(Theme.Font.mono.weight(.bold)).frame(width: 14)
                    Text(tr("未分配", "Unassigned")).font(Theme.Font.rowTitle)
                    Text(panel.text { "\($0.count)" }).font(Theme.Font.count).foregroundStyle(Theme.ink2)
                    if desk.isFixture { Chip(text: tr("模拟数据", "Sample data"), style: .dashed) }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain).focusRing()
            if desk.unassignedOpen {
                PanelBody(panel: panel) { list in
                    VStack(alignment: .leading, spacing: 0) {
                        if list.isEmpty { Text("0").font(Theme.Font.body).foregroundStyle(Theme.ink3).padding(.top, 10) }
                        ForEach(list) { b in
                            HStack(spacing: 14) {
                                Lamp(kind: .unknown)
                                VStack(alignment: .leading, spacing: 2) {
                                    Text(b.bindingID).font(Theme.Font.mono).lineLimit(1)
                                    HStack(spacing: 10) {
                                        Text(hostName(b.harness)).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                                        Text(b.hostSessionRef ?? tr("会话 未知", "Session unknown")).font(Theme.Font.small).foregroundStyle(Theme.ink3).lineLimit(1)
                                    }
                                }
                                Spacer(minLength: 0)
                                BoxButton(title: tr("在宿主页", "On Hosts")) { desk.showOnHostPage(b.bindingID) }
                            }
                            .padding(.vertical, 10).frame(maxWidth: 560, alignment: .leading)
                            .contentShape(Rectangle()).onTapGesture { desk.openSeat(b.actor) }       // 和宿主页卡片同一个抽屉：看信、写信
                            .overlay(alignment: .bottom) { Rule() }
                        }
                    }
                    .padding(.top, 8)
                }
            }
        }
    }
}

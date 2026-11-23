import SwiftUI
import OfficeClient

/// Work：已完成 Work 的来历图。每个项目一页；只画已完成的，节点之间只有"依据"和"修改"两种线，从左到右 = 从来源到后续；
/// 没有任何关系的放在图下方。节点上写承办人、承办用量和审核轮次。
/// 数据：work.list（谁完成了）+ 每件已完成的 work.get(history)（关系、审核轮次、用量卡）。
struct WorkPage: View {
    @ObservedObject var desk: DeskModel
    @State private var reviewOpen = false

    var body: some View {
        PageScaffold {
            PageTitle(title: "Work", fixture: simulated) { stats }
            Text(tr("已完成 Work 分析", "Completed-Work analysis")).font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.top, 2)
            if reviewOpen { reviewList.padding(.top, 18) }
            if let tab = desk.currentWorkTab {
                ProjectTabs(desk: desk, current: tab).padding(.top, 26)
                PanelBody(panel: desk.completed) { list in
                    Text(tr("本页 \(list.items.count) / 共 \(list.total)", "This page \(list.items.count) / \(list.total) total")).font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.top, 12)
                    graph()
                    HStack {
                        if desk.completedCursor != nil { BoxButton(title: tr("回到首页", "First page")) { Task { await desk.loadCompleted(cursor: nil) } } }
                        if list.hasMore, let next = list.nextCursor { BoxButton(title: tr("下一页", "Next page")) { Task { await desk.loadCompleted(cursor: next) } } }
                    }.padding(.top, 12)
                }
                if case .done = desk.completed, desk.completed.fact != nil {
                    HStack {
                        BoxButton(title: tr("重试", "Retry")) { Task { await desk.loadCompleted(cursor: desk.completedCursor) } }
                        if desk.completedCursor != nil { BoxButton(title: tr("回到首页", "First page")) { Task { await desk.loadCompleted(cursor: nil) } } }
                    }
                }
            } else {
                PanelBody(panel: desk.projects) { _ in
                    Text(tr("0 个项目", "No projects")).font(Theme.Font.body).foregroundStyle(Theme.ink3).padding(.top, 40)
                }
            }
            ReadAt(desk: desk, what: "Work", fact: desk.completed.fact).padding(.top, 40)
        }
        .safeAreaInset(edge: .bottom, spacing: 0) { if desk.workPicked.count >= 2 { CompareBar(desk: desk, refs: desk.workPicked) { desk.workPicked = [] } } }
    }

    // MARK: 顶部：数 + 待你审核

    @ViewBuilder private var stats: some View {
        HStack(alignment: .bottom, spacing: Theme.statGap) {
            if let w = desk.completed.value {
                Stat(value: "\(w.total)", label: tr("本项目已完成", "Project completed"))
            } else {
                Stat(value: desk.completed.text { _ in "" }, label: tr("本项目已完成", "Project completed"))
            }
            Button { reviewOpen.toggle() } label: {
                Stat(value: desk.reviews.text { "\($0.total)" }, label: tr("待你审核", "Awaiting your review"))
                    .overlay(alignment: .bottom) { Rectangle().fill(Theme.ink).frame(height: Theme.Line.outline).offset(y: 6) }
            }
            .buttonStyle(.plain).focusRing()
        }
    }

    /// 待你审核的活：点一件打开抽屉，通过 / 退回在那里（原来的审核入口）。
    private var reviewList: some View {
        VStack(alignment: .leading, spacing: 0) {
            PanelBody(panel: desk.reviews) { page in
              VStack(alignment: .leading, spacing: 0) {
              if page.items.isEmpty { Text(tr("本页没有可审核的 Work", "No reviewable Work on this page")).foregroundStyle(Theme.ink3) }
              ForEach(page.items) { w in
                Button { desk.openWork(w.ref) } label: {
                    HStack(spacing: 12) {
                        WorkMark(state: w.state)
                        Text(w.ref).font(Theme.Font.mono)
                        Text(w.objective).font(Theme.Font.f14).lineLimit(1)
                        Spacer(minLength: 0)
                        Text(desk.seatName(w.assignee)).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                    }
                    .padding(.vertical, 9).contentShape(Rectangle())
                }
                .buttonStyle(.plain).focusRing()
                .overlay(alignment: .bottom) { Rule() }
              }
              HStack {
                  if desk.reviewCursor != nil { BoxButton(title: tr("回到首页", "First page")) { Task { await desk.loadReviews(cursor: nil) } } }
                  if let next = page.nextCursor, page.hasMore { BoxButton(title: tr("下一页", "Next page")) { Task { await desk.loadReviews(cursor: next) } } }
              }.padding(.top, 12)
              }
            }
            if case .done = desk.reviews, desk.reviews.fact != nil {
                HStack {
                    BoxButton(title: tr("重试", "Retry")) { Task { await desk.loadReviews(cursor: desk.reviewCursor) } }
                    if desk.reviewCursor != nil { BoxButton(title: tr("回到首页", "First page")) { Task { await desk.loadReviews(cursor: nil) } } }
                }.padding(.top, 12)
            }
        }
        .frame(maxWidth: 640, alignment: .leading)
    }

    // MARK: 图

    private func facts(_ rows: [WorkRow]) -> [NodeFacts] { rows.map { NodeFacts(row: $0, desk: desk) } }
    private var simulated: Bool { facts(desk.doneInTab).contains { $0.usage.simulated } }

    @ViewBuilder private func graph() -> some View {
        let rows = desk.doneInTab
        let all = facts(rows)
        let byRef = Dictionary(uniqueKeysWithValues: all.map { ($0.row.ref, $0) })
        let g = WorkGraph(refs: rows.map(\.ref), relations: Dictionary(uniqueKeysWithValues: all.compactMap { f in f.relations.map { (f.row.ref, $0) } }))
        let layout = GraphLayout(graph: g)
        let names = Self.modelNames(all)
        if rows.isEmpty {
            Text(tr("这个项目还没有已完成的 Work", "No completed Work in this project yet")).font(Theme.Font.body).foregroundStyle(Theme.ink3).padding(.top, 40)
        } else {
            if !names.isEmpty { modelFilter(names).padding(.top, 18) }
            if !g.layers.isEmpty {
                ScrollView(.horizontal) {
                    ZStack(alignment: .topLeading) {
                        Edges(layout: layout)
                        ForEach(rows) { r in
                            if let rect = layout.nodes[r.ref], let f = byRef[r.ref] {
                                node(f).offset(x: rect.minX, y: rect.minY)
                            }
                        }
                    }
                    .frame(width: layout.size.width, height: layout.size.height, alignment: .topLeading)
                    .padding(.vertical, 14).padding(.trailing, 14)
                }
                .padding(.top, 12)
                legend.padding(.top, 18)
            }
            if !g.isolated.isEmpty {
                Text(tr("无关联 Work", "Unlinked Work")).font(Theme.Font.sub).foregroundStyle(Theme.ink3).padding(.top, 20)
                FlowLayout(spacing: GraphLayout.rowGap) {
                    ForEach(g.isolated, id: \.self) { ref in
                        if let f = byRef[ref] { node(f) }
                    }
                }
                .padding(.top, 8)
            }
        }
    }

    private func node(_ f: NodeFacts) -> some View {
        GraphNode(desk: desk, facts: f, picked: desk.workPicked.contains(f.row.ref), dim: dim(f)) { toggle(f.row.ref) }
            .frame(width: GraphLayout.nodeW, height: GraphLayout.nodeH)
    }

    private func dim(_ f: NodeFacts) -> Bool { !desk.workModels.isEmpty && desk.workModels.isDisjoint(with: f.filterKeys) }
    private func toggle(_ ref: String) { if let i = desk.workPicked.firstIndex(of: ref) { desk.workPicked.remove(at: i) } else { desk.workPicked.append(ref) } }

    /// 这个项目里出现过的模型（"未知"排最后）。
    private static func modelNames(_ all: [NodeFacts]) -> [String] {
        let s = Set(all.flatMap(\.filterKeys))
        return s.filter { $0 != NodeFacts.unknown }.sorted() + (s.contains(NodeFacts.unknown) ? [NodeFacts.unknown] : [])
    }

    private func modelFilter(_ names: [String]) -> some View {
        FlowLayout(spacing: 8) {
            Text(tr("模型", "Model")).font(Theme.Font.small.weight(.semibold)).foregroundStyle(Theme.ink3).frame(minHeight: Theme.control)
            ForEach(names, id: \.self) { m in
                BoxButton(title: m, primary: desk.workModels.contains(m)) { if desk.workModels.contains(m) { desk.workModels.remove(m) } else { desk.workModels.insert(m) } }
            }
            if !desk.workModels.isEmpty { BoxButton(title: tr("清除", "Clear")) { desk.workModels = [] } }
        }
    }

    private var legend: some View {
        HStack(spacing: 26) {
            ForEach([WorkEdge.Kind.basedOn, .modifies], id: \.rawValue) { k in
                HStack(spacing: 9) {
                    Path { p in p.move(to: CGPoint(x: 0, y: 6)); p.addLine(to: CGPoint(x: 34, y: 6)) }
                        .stroke(Theme.ink2, style: StrokeStyle(lineWidth: Theme.Line.outline, dash: Edges.dash(k))).frame(width: 34, height: 12)
                    Path { p in p.move(to: CGPoint(x: 0, y: 6)); p.addLine(to: CGPoint(x: -9, y: 1)); p.addLine(to: CGPoint(x: -9, y: 11)); p.closeSubpath() }
                        .fill(Theme.ink2).frame(width: 0, height: 12).offset(x: -3)
                    Text(k == .basedOn ? tr("依据（箭头：来源 → 后续）", "Based on (source → follow-up)") : tr("修改", "Modifies")).font(Theme.Font.small).foregroundStyle(Theme.ink2)
                }
            }
        }
    }
}

// MARK: - 节点的事实（节点和比较栏共用）

@MainActor struct NodeFacts {
    static var unknown: String { tr("未知", "Unknown") }
    let row: WorkRow
    let panel: Panel<WorkDetail>
    let usage: (card: WorkUsageCard?, simulated: Bool)

    init(row: WorkRow, desk: DeskModel) {
        self.row = row
        panel = desk.workDetails[row.ref] ?? .loading
        usage = panel.value.map { desk.usageCard($0) } ?? (nil, false)
    }
    var loading: Bool { if case .loading = panel { return true }; return false }
    /// 我指向谁；没读到 = nil。
    var relations: [WorkRelation]? { panel.value?.relations }
    var rounds: Int? { panel.value?.reviewRounds }
    var spent: UsageCell? { usage.card.flatMap { $0.covered ? $0.assignee?.cell : nil } }
    /// 承办用的模型（没拆 / 没读到 = 空 = "未知"）。
    var assigneeModels: [String] { (usage.card?.assigneeModels ?? []).map { $0 ?? Self.unknown } }
    /// 筛选用：承办 + 审核涉及的全部模型；一个都没有 = {未知}。
    var filterKeys: Set<String> {
        let m = (usage.card?.models ?? []).map { $0 ?? Self.unknown }
        return m.isEmpty ? [Self.unknown] : Set(m)
    }
    var modelText: String { assigneeModels.isEmpty ? Self.unknown : assigneeModels.joined(separator: "、") }

    static func n(_ v: Int?) -> String { v.map(TokenFmt.short) ?? unknown }
    var inText: String { loading ? "…" : Self.n(spent?.tokensIn) }
    var outText: String { loading ? "…" : Self.n(spent?.tokensOut) }
    var cacheText: String { loading ? "…" : Self.n(spent?.tokensCache) }
    var roundsText: String { loading ? "…" : (rounds.map { tr("\($0) 轮", "\($0)") } ?? Self.unknown) }
}

// MARK: - 节点

private struct GraphNode: View {
    @ObservedObject var desk: DeskModel
    let facts: NodeFacts
    let picked: Bool
    let dim: Bool
    let toggle: () -> Void
    @State private var hover = false

    var body: some View {
        let w = facts.row
        let open = desk.drawer == .work(w.ref)
        ZStack(alignment: .topTrailing) {
            Button {
                if NSEvent.modifierFlags.contains(.command) { toggle() } else { desk.openWork(w.ref) }
            } label: {
                VStack(alignment: .leading, spacing: 4) {
                    Text(w.ref).font(Theme.Font.mark.weight(.semibold)).foregroundStyle(Theme.ink2)
                    Text(desk.seatName(w.assignee) + " · " + facts.modelText).font(Theme.Font.small)
                        .foregroundStyle(facts.assigneeModels.isEmpty ? Theme.ink3 : Theme.ink2).lineLimit(1)
                    HStack(spacing: 10) {                                                          // ③ 入 / 出 token（醒目）+ 审核轮次
                        Text(tr("入 ", "In ") + facts.inText + tr("  出 ", "  Out ") + facts.outText).font(.system(size: 14, weight: .bold, design: .monospaced)).lineLimit(1).minimumScaleFactor(0.7)
                        Spacer(minLength: 0)
                        Text(tr("审核 ", "Rounds ") + facts.roundsText).font(Theme.Font.sub).foregroundStyle(Theme.ink2).lineLimit(1)
                    }
                }
                .padding(.horizontal, 11).padding(.vertical, 9)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .background(hover ? Theme.paper2 : Theme.paper, ignoresSafeAreaEdges: [])
                .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
                .overlay { if open { Rectangle().strokeBorder(Theme.red, lineWidth: 3) } else if picked { Rectangle().strokeBorder(Theme.ink, lineWidth: 2.5) } }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain).onHover { hover = $0 }.focusRing()
            .accessibilityLabel(Text(w.ref))
            Button(action: toggle) {                                                              // 右上角：选中以便比较
                Square(filled: picked, tone: Theme.ink).padding(7).contentShape(Rectangle())
            }
            .buttonStyle(.plain).help(tr("选中，和别的节点并排比较", "Pick to compare side by side"))
        }
        .opacity(dim ? 0.26 : 1)
    }
}

// MARK: - 连线

private struct Edges: View {
    let layout: GraphLayout
    static func dash(_ k: WorkEdge.Kind) -> [CGFloat] { k == .basedOn ? [] : [6, 4] }
    var body: some View {
        ForEach(Array(layout.routes.enumerated()), id: \.offset) { _, r in
            Path { p in
                p.move(to: r.points[0]); for q in r.points.dropFirst() { p.addLine(to: q) }
            }
            .stroke(Theme.ink2, style: StrokeStyle(lineWidth: Theme.Line.hair, lineJoin: .miter, dash: Self.dash(r.edge.kind)))
            if let e = r.points.last {
                Path { p in p.move(to: e); p.addLine(to: CGPoint(x: e.x - 9, y: e.y - 4.5)); p.addLine(to: CGPoint(x: e.x - 9, y: e.y + 4.5)); p.closeSubpath() }
                    .fill(Theme.ink2)
            }
        }
    }
}

// MARK: - 并排比较

private struct CompareBar: View {
    @ObservedObject var desk: DeskModel
    let refs: [String]
    let clear: () -> Void
    private let labelW: CGFloat = 72, colW: CGFloat = 214

    var body: some View {
        let items = refs.compactMap { r in desk.doneInTab.first { $0.ref == r }.map { NodeFacts(row: $0, desk: desk) } }
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Theme.ink).frame(height: Theme.Line.hair)
            HStack(alignment: .top, spacing: 0) {
                VStack(alignment: .leading, spacing: 6) {
                    Text(tr("并排比较", "Compare")).font(Theme.Font.head)
                    BoxButton(title: tr("清除", "Clear"), action: clear)
                }
                .frame(width: 112, alignment: .leading)
                ScrollView(.horizontal) {
                    HStack(alignment: .top, spacing: 0) {
                        labels
                        ForEach(items, id: \.row.ref) { f in column(f) }
                    }
                }
            }
            .padding(.horizontal, Theme.pad).padding(.vertical, 14)
        }
        .background(Theme.paper, ignoresSafeAreaEdges: [])
    }

    private var labels: some View {
        VStack(alignment: .leading, spacing: 5) {
            ForEach([tr("工作", "Work"), tr("承办", "Assignee"), tr("模型", "Model"), tr("入", "In"), tr("出", "Out"), tr("审核", "Rounds"), tr("缓存", "Cache")], id: \.self) {
                Text($0).font(Theme.Font.sub).foregroundStyle(Theme.ink3).frame(height: $0 == tr("工作", "Work") ? 34 : 17, alignment: .topLeading)
            }
        }
        .frame(width: labelW, alignment: .leading)
    }

    private func column(_ f: NodeFacts) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            VStack(alignment: .leading, spacing: 0) {
                Text(f.row.ref).font(Theme.Font.mark.weight(.semibold)).foregroundStyle(Theme.ink3)
                Text(f.row.objective).font(Theme.Font.small.weight(.bold)).lineLimit(1)
            }.frame(height: 34, alignment: .topLeading)
            cell(desk.seatName(f.row.assignee)); cell(f.modelText); cell(f.inText, mono: true); cell(f.outText, mono: true)
            cell(f.roundsText); cell(f.cacheText, mono: true)
        }
        .frame(width: colW, alignment: .leading)
        .overlay(alignment: .leading) { Rectangle().fill(Theme.rule).frame(width: Theme.Line.hair) }
        .padding(.leading, 12)
    }
    private func cell(_ s: String, mono: Bool = false) -> some View {
        Text(s).font(mono ? .system(size: 12.5, weight: .semibold, design: .monospaced) : Theme.Font.small).lineLimit(1).frame(height: 17, alignment: .topLeading)
    }
}

// MARK: - 项目页签

private struct ProjectTabs: View {
    @ObservedObject var desk: DeskModel
    let current: String
    var body: some View {
        // 窗口窄了就换行，不裁切
        FlowLayout(spacing: 8) {
            ForEach(desk.workTabs, id: \.self) { ref in
                let name = desk.projects.value?.first { $0.ref == ref }?.name
                BoxButton(title: name?.isEmpty == false ? name! : ref, primary: ref == current) { desk.workTab = ref }
            }
        }
    }
}

/// 简单的换行排布：一行放不下就折到下一行。
struct FlowLayout: Layout {
    var spacing: CGFloat = 8
    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(width: proposal.width ?? .infinity, subviews).size
    }
    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        let a = arrange(width: bounds.width, subviews)
        for (i, f) in a.frames.enumerated() { subviews[i].place(at: CGPoint(x: bounds.minX + f.minX, y: bounds.minY + f.minY), proposal: ProposedViewSize(f.size)) }
    }
    private func arrange(width: CGFloat, _ subviews: Subviews) -> (frames: [CGRect], size: CGSize) {
        var x: CGFloat = 0, y: CGFloat = 0, rowH: CGFloat = 0, maxW: CGFloat = 0
        var frames: [CGRect] = []
        for v in subviews {
            let s = v.sizeThatFits(.unspecified)
            if x > 0, x + s.width > width { x = 0; y += rowH + spacing; rowH = 0 }
            frames.append(CGRect(x: x, y: y, width: s.width, height: s.height))
            x += s.width + spacing; rowH = max(rowH, s.height); maxW = max(maxW, x - spacing)
        }
        return (frames, CGSize(width: maxW, height: y + rowH))
    }
}

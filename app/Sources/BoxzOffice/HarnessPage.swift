import SwiftUI
import OfficeClient

/// Harness：每个宿主（OpenCode / Codex / Claude / Pi）下挂着哪些活动绑定。
/// 版式取自设计稿 B-宿主（标题 + 统计、宿主汇总框、每个宿主一节、卡片网格）。
/// 数据现在来自夹具（R2 release.status 没上线），所以页头标"模拟数据"。
/// "有活动绑定"只表示已绑定：在线 / 运行中后端没有这个事实，卡片一律画虚线（未知），不画实心（在线）。
/// 设计稿里的"全部下线 / 开关"是写操作，不在这一步里。
struct HarnessPage: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        ScrollViewReader { proxy in
            PageScaffold {
                PageTitle(title: "Harness", fixture: desk.isFixture) { stats }
                PanelBody(panel: desk.hosts) { page in
                    let groups = groupedByHost(page.bindings, harness: \.harness)
                    VStack(alignment: .leading, spacing: 0) {
                        summary(groups).padding(.top, 38)
                        ForEach(groups, id: \.name) { g in section(g) }
                    }
                }
                ReadAt(desk: desk).padding(.top, 40)
            }
            .onAppear { scroll(proxy) }
            .onChange(of: desk.hosts.value?.bindings.count) { _ in scroll(proxy) }
        }
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        guard let f = desk.focusBinding else { return }
        DispatchQueue.main.async { withAnimation { proxy.scrollTo(f, anchor: .center) } }
    }

    // MARK: 统计
    @ViewBuilder private var stats: some View {
        HStack(alignment: .bottom, spacing: Theme.statGap) {
            Stat(value: desk.hosts.text { "\($0.bindings.count)" }, label: tr("活动绑定", "Active bindings"))
            Stat(value: tr("未知", "Unknown"), label: tr("在线", "Online"), muted: true)        // 没有后端事实源；只在这里写一次
        }
    }

    // MARK: 分组

    private func summary(_ groups: [HostGroup<HostBinding>]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220, maximum: 280), spacing: 12, alignment: .topLeading)], alignment: .leading, spacing: 12) {
            ForEach(groups, id: \.name) { g in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        Text(g.name).font(Theme.Font.small.weight(.bold))
                        Text(tr("\(g.items.count) 绑定", "\(g.items.count) \(pl(g.items.count, "binding"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                    }
                    Text(g.items.map(\.actor).joined(separator: "、")).font(Theme.Font.sub).foregroundStyle(Theme.ink3).lineLimit(2)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
            }
        }
    }

    private func section(_ g: HostGroup<HostBinding>) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Theme.ink).frame(height: Theme.Line.hair)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(g.name).font(Theme.Font.sectionTitle).tracking(Theme.Track.blockTitle)
                Text(tr("\(g.items.count) 绑定", "\(g.items.count) \(pl(g.items.count, "binding"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                Spacer(minLength: 0)
                let blocked = g.items.first.map { desk.fixtureBlocksWrite(.unbind(bindingID: $0.bindingID, label: $0.actor)) } ?? true
                BoxButton(title: tr("全部下线", "Take all offline"), disabled: blocked) {
                    desk.askOffline(title: tr("全部下线 · ", "Take all offline · ") + g.name, bindings: g.items)
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
            }
            .padding(.top, 16).padding(.bottom, 18)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 230, maximum: 260), spacing: 14, alignment: .topLeading)], alignment: .leading, spacing: 14) {
                ForEach(g.items) { BindingCard(desk: desk, binding: $0) }
            }
        }
        .padding(.top, 36)
    }
}

/// 按宿主分组：宿主页和用量页共用（首次出现的顺序）。harness 缺 = "来源未知"。
struct HostGroup<T> { let name: String; let items: [T] }

func hostName(_ harness: String?) -> String {
    switch harness?.lowercased() {
    case "opencode": "OpenCode"
    case "codex": "Codex"
    case "claude": "Claude"
    case "pi": "Pi"
    case .some(let h) where !h.isEmpty: h
    default: tr("来源未知", "Unknown source")
    }
}

func groupedByHost<T>(_ all: [T], harness: (T) -> String?) -> [HostGroup<T>] {
    var order: [String] = [], map: [String: [T]] = [:]
    for x in all { let n = hostName(harness(x)); if map[n] == nil { order.append(n) }; map[n, default: []].append(x) }
    return order.map { HostGroup(name: $0, items: map[$0] ?? []) }
}

/// 一个绑定的卡片：虚线 = 在线与否未知。额度 null 写"额度 未知"。
private struct BindingCard: View {
    @ObservedObject var desk: DeskModel
    let binding: HostBinding
    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Square(filled: false, dashed: true, tone: Theme.ink3)
                Text(binding.bindingID).font(Theme.Font.sub).foregroundStyle(Theme.ink3).lineLimit(1)
            }
            Text(binding.actor).font(Theme.Font.rowTitle).tracking(-0.17).lineLimit(1).padding(.top, 14)
            Text(binding.hostSessionRef ?? tr("会话 未知", "Session unknown")).font(Theme.Font.mono).foregroundStyle(Theme.ink3).lineLimit(1).padding(.top, 3)
            Text(flightText(desk.seatMail[binding.actor])).font(Theme.Font.small.weight(.semibold)).padding(.top, 10)
            Chip(text: binding.quota.map { $0 == "exhausted" ? Labels.quota($0) : tr("额度 ", "Quota ") + Labels.quota($0) } ?? tr("额度 未知", "Quota unknown"), style: binding.quota == nil ? .dashed : .mid).padding(.top, 10)
            HStack(spacing: 8) {
                let blocked = desk.fixtureBlocksWrite(.unbind(bindingID: binding.bindingID, label: binding.actor))
                BoxButton(title: tr("下线", "Take offline"), disabled: blocked) {
                    desk.askOffline(title: tr("下线 · ", "Take offline · ") + binding.actor, bindings: [binding])
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
                HeldMark(desk: desk, key: "unbind:" + binding.bindingID)
            }
            .padding(.top, 12)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Rectangle().strokeBorder(desk.focusBinding == binding.bindingID ? Theme.ink : Theme.wire,
                                          style: StrokeStyle(lineWidth: desk.focusBinding == binding.bindingID ? 2 : Theme.Line.hair, dash: desk.focusBinding == binding.bindingID ? [] : [4, 3])))
        .contentShape(Rectangle())
        .onTapGesture { desk.openSeat(binding.actor) }
        .id(binding.bindingID)
    }
}

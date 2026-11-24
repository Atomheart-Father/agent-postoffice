import SwiftUI
import OfficeClient

/// 每个已登记的原生会话一张卡，在线状态未知。
struct HarnessPage: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        ScrollViewReader { proxy in
            PageScaffold {
                PageTitle(title: "Harness", fixture: desk.isFixture) { stats }
                PanelBody(panel: desk.hosts) { page in
                    let groups = groupedByHost(page.threads, harness: \.harness)
                    VStack(alignment: .leading, spacing: 0) {
                        summary(groups).padding(.top, 38)
                        ForEach(groups, id: \.name) { g in section(g) }
                    }
                }
                ReadAt(desk: desk).padding(.top, 40)
            }
            .onAppear { scroll(proxy) }
            .onChange(of: desk.hosts.value?.threads) { _ in scroll(proxy) }
        }
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        guard let f = desk.focusBinding,
              let thread = desk.hosts.value?.threads.first(where: { $0.bindings.contains { $0.bindingID == f } }) else { return }
        DispatchQueue.main.async { withAnimation { proxy.scrollTo(thread.id, anchor: .center) } }
    }

    // MARK: 统计
    @ViewBuilder private var stats: some View {
        HStack(alignment: .bottom, spacing: Theme.statGap) {
            Stat(value: desk.hosts.text { "\($0.threads.count)" }, label: tr("已绑定会话", "Bound sessions"))
            Stat(value: tr("未知", "Unknown"), label: tr("在线", "Online"), muted: true)        // 没有后端事实源；只在这里写一次
        }
    }

    // MARK: 分组

    private func summary(_ groups: [HostGroup<HostThread>]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 220, maximum: 280), spacing: 12, alignment: .topLeading)], alignment: .leading, spacing: 12) {
            ForEach(groups, id: \.name) { g in
                VStack(alignment: .leading, spacing: 8) {
                    HStack(spacing: 10) {
                        Text(g.name).font(Theme.Font.small.weight(.bold))
                        Text(tr("\(g.items.count) 会话", "\(g.items.count) \(pl(g.items.count, "session"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                    }
                    Text(g.items.map(\.title).joined(separator: "、")).font(Theme.Font.sub).foregroundStyle(Theme.ink3).lineLimit(2)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
            }
        }
    }

    private func section(_ g: HostGroup<HostThread>) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Theme.ink).frame(height: Theme.Line.hair)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(g.name).font(Theme.Font.sectionTitle).tracking(Theme.Track.blockTitle)
                Text(tr("\(g.items.count) 会话", "\(g.items.count) \(pl(g.items.count, "session"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                Spacer(minLength: 0)
                let blocked = g.items.first.map { desk.fixtureBlocksWrite(.unbind(bindingID: $0.id, label: $0.title)) } ?? true
                BoxButton(title: tr("全部下线", "Take all offline"), disabled: blocked) {
                    desk.askOffline(title: tr("全部下线 · ", "Take all offline · ") + g.name, bindings: g.items.flatMap(\.bindings))
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
            }
            .padding(.top, 16).padding(.bottom, 18)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 230, maximum: 260), spacing: 14, alignment: .topLeading)], alignment: .leading, spacing: 14) {
                ForEach(g.items) { BindingCard(desk: desk, thread: $0) }
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

/// 单个会话可有多个可独立收信的地址。
private struct BindingCard: View {
    @ObservedObject var desk: DeskModel
    let thread: HostThread

    var body: some View {
        let binding = thread.primary
        let title = thread.title
        let focused = thread.bindings.contains { $0.bindingID == desk.focusBinding }
        let addresses = Set(thread.bindings.map(\.mailbox)).sorted()
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 8) {
                Square(filled: false, dashed: true, tone: Theme.ink3)
                Text(title).font(Theme.Font.rowTitle).tracking(-0.17).lineLimit(1).help(title)
            }
            ForEach(thread.managedProjects(in: desk.projects.value ?? [])) { project in
                Text(projectManagerLabel(project))
                    .font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.top, 4)
            }

            ForEach(addresses, id: \.self) { address in
                Button { desk.openSeat(address) } label: {
                    let label = address.hasPrefix("@") ? address : tr("个人信箱", "Personal inbox")
                    let pending = deliveryText(desk.deliveryAutomatic(address), inFlight: desk.deliveryInFlight(address), unresolved: desk.deliveryUnresolved(address))
                    Text(addresses.count == 1 ? pending : label + " · " + pending)
                        .font(Theme.Font.small.weight(.semibold)).fixedSize(horizontal: false, vertical: true)
                }
                .buttonStyle(.plain).focusRing().padding(.top, 10)
            }
            Chip(text: binding.quota.map { $0 == "exhausted" ? Labels.quota($0) : tr("额度 ", "Quota ") + Labels.quota($0) } ?? tr("额度 未知", "Quota unknown"), style: binding.quota == nil ? .dashed : .mid).padding(.top, 10)
            HStack(spacing: 8) {
                let blocked = desk.fixtureBlocksWrite(.unbind(bindingID: binding.bindingID, label: title))
                BoxButton(title: tr("下线", "Take offline"), disabled: blocked) {
                    desk.askOffline(title: tr("下线 · ", "Take offline · ") + title, bindings: thread.bindings)
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
                HeldMark(desk: desk, key: "unbind:" + binding.bindingID)
            }
            .padding(.top, 12)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Rectangle().strokeBorder(focused ? Theme.ink : Theme.wire,
                                          style: StrokeStyle(lineWidth: focused ? 2 : Theme.Line.hair, dash: focused ? [] : [4, 3])))
        .contentShape(Rectangle())
        .onTapGesture { desk.openSeat(binding.mailbox) }
        .id(thread.id)
    }
}

import SwiftUI
import AppKit
import OfficeClient

/// 每个已登记的原生会话一张卡，按宿主分组。
struct HarnessPage: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        ScrollViewReader { proxy in
            PageScaffold {
                PageTitle(title: Page.host.title, fixture: desk.isFixture) { stats }
                PanelBody(panel: desk.hosts) { page in
                    let groups = groupedByHost(page.threads, harness: \.harness)
                    VStack(alignment: .leading, spacing: 28) {
                        if groups.isEmpty {
                            Text(tr("暂无会话", "No sessions")).font(Theme.Font.body).foregroundStyle(Theme.ink3)
                        }
                        ForEach(groups, id: \.name) { g in section(g) }
                    }
                    .padding(.top, 24)
                }
                ReadAt(desk: desk).padding(.top, 40)
            }
            .onAppear { scroll(proxy) }
            .onChange(of: desk.hosts.value?.threads) { scroll(proxy) }
        }
    }

    private func scroll(_ proxy: ScrollViewProxy) {
        guard let f = desk.focusBinding,
              let thread = desk.hosts.value?.threads.first(where: { $0.bindings.contains { $0.bindingID == f } }) else { return }
        DispatchQueue.main.async { withAnimation { proxy.scrollTo(thread.id, anchor: .center) } }
    }

    // MARK: 统计
    @ViewBuilder private var stats: some View {
        Stat(value: desk.hosts.text { "\($0.threads.count)" }, label: tr("会话", "Sessions"))
    }

    // MARK: 分组

    private func section(_ g: HostGroup<HostThread>) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Rule(color: Theme.ink)
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                Text(g.name).font(Theme.Font.sectionTitle).tracking(Theme.Track.blockTitle)
                Text(tr("\(g.items.count) 个会话", "\(g.items.count) \(pl(g.items.count, "session"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3)
                Spacer(minLength: 0)
                let blocked = g.items.first.map { desk.fixtureBlocksWrite(.unbind(bindingID: $0.id, label: $0.title)) } ?? true
                BoxButton(title: tr("全部下线", "Take all offline"), disabled: blocked) {
                    desk.askOffline(title: tr("全部下线 · ", "Take all offline · ") + g.name, bindings: g.items.flatMap(\.bindings))
                }
                .help(blocked ? tr("模拟数据，不能写", "Sample data: cannot write") : "")
            }
            .padding(.top, 12).padding(.bottom, 12)
            LazyVGrid(columns: [GridItem(.adaptive(minimum: 210, maximum: 310), spacing: 12, alignment: .topLeading)], alignment: .leading, spacing: 10) {
                ForEach(g.items) { BindingCard(desk: desk, thread: $0) }
            }
        }
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
        let focused = thread.bindings.contains { $0.bindingID == desk.focusBinding }
        let roles = thread.positionAddresses
        let personal = Set(thread.bindings.filter { $0.position == nil }.map(\.mailbox)).sorted()
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: 6) {
                if roles.isEmpty {
                    let label = tr("未分配", "Unassigned")
                    let suffix = thread.sessionRef.isEmpty ? "" : " · " + String(thread.title.suffix(6))
                    mailbox(binding.mailbox, label: label + suffix, secondary: true, sessionID: thread.title)
                } else {
                    ForEach(roles, id: \.self) { address in
                        mailbox(address, label: address)
                    }
                    ForEach(personal.filter { (desk.deliveryPending($0) ?? 0) > 0 }, id: \.self) { address in
                        mailbox(address, label: tr("个人信箱", "Personal inbox"), secondary: true)
                    }
                }
                if let quota = binding.quota, ["exhausted", "blocked", tr("已阻塞", "Blocked")].contains(quota) {
                    Text(Labels.quota("exhausted")).font(Theme.Font.small).foregroundStyle(Theme.bad)
                }
            }
            Spacer(minLength: 0)
            Menu {
                if !roles.isEmpty {
                    ForEach(personal, id: \.self) { address in
                        Button(personal.count == 1 ? tr("个人信箱", "Personal inbox") : address) { desk.openSeat(address) }
                    }
                }
                if !thread.sessionRef.isEmpty {
                    Button(tr("复制会话编号", "Copy session ID")) {
                        NSPasteboard.general.clearContents()
                        NSPasteboard.general.setString(thread.sessionRef, forType: .string)
                    }
                }
                Button(tr("下线", "Take offline"), role: .destructive) {
                    desk.askOffline(title: tr("下线 · ", "Take offline · ") + thread.title, bindings: thread.bindings)
                }
                .disabled(desk.fixtureBlocksWrite(.unbind(bindingID: binding.bindingID, label: thread.title)))
            } label: {
                Image(systemName: "ellipsis").accessibilityLabel(tr("会话操作", "Session actions"))
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            HeldMark(desk: desk, key: "unbind:" + binding.bindingID)
        }
        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
        .overlay(Rectangle().strokeBorder(focused ? Theme.ink : Theme.wire,
                                          lineWidth: focused ? 2 : Theme.Line.hair).allowsHitTesting(false))
        .id(thread.id)
    }

    private func mailbox(_ address: String, label: String, secondary: Bool = false, sessionID: String? = nil) -> some View {
        let delivery = deliveryText(desk.deliveryAutomatic(address), inFlight: desk.deliveryInFlight(address),
                                    unresolved: desk.deliveryUnresolved(address))
        let details = sessionID.map { $0 + "\n" + delivery } ?? delivery
        return Button { desk.openSeat(address) } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(label).font(secondary ? Theme.Font.small.weight(.semibold) : Theme.Font.rowTitle)
                    .lineLimit(1)
                Text(pendingDeliveryText(desk.deliveryPending(address)))
                    .font(Theme.Font.small).foregroundStyle(Theme.ink3)
            }
            .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
        }
        .buttonStyle(.plain).focusRing()
        .help(details)
    }
}

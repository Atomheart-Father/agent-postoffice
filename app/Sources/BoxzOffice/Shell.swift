import SwiftUI
import OfficeClient

/// 主窗口：顶栏 + 内容 + 右侧抽屉（设计稿的版式：没有侧栏）。
struct Shell: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        ZStack(alignment: .trailing) {
            Theme.paper
            VStack(spacing: 0) {
                TopBar(desk: desk)
                Group {
                    switch desk.page {
                    case .org: OrgPage(desk: desk)
                    case .work: WorkPage(desk: desk)
                    case .usage: UsagePage(desk: desk)
                    case .settings: SettingsPage(desk: desk)
                    case .host: HarnessPage(desk: desk)
                    }
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .top)
            }
            if desk.drawer != nil {
                Theme.scrim.onTapGesture { desk.drawer = nil }.transition(.opacity)
                HStack(spacing: 0) {
                    Rectangle().fill(Theme.ink).frame(width: Theme.Line.hair)
                    DrawerView(desk: desk).frame(width: Theme.drawerWidth)
                }
                .background(Theme.paper)
                .transition(.move(edge: .trailing))
            }
        }
        .overlay { if let d = desk.offlineDraft { OfflineConfirm(desk: desk, draft: d) } }
        .animation(.easeOut(duration: 0.16), value: desk.drawer)
        .foregroundStyle(Theme.ink)
        .id(desk.lang)
        .ignoresSafeArea()
    }
}

/// 顶栏：品牌 · 标签 · 右侧"老板"按钮（打开待办抽屉）。64 高，1px ink 压底。
struct TopBar: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        // 窗口窄了就收起次要的东西（副标题、"隔离写入"标记、刷新），标签永远一行、不折字
        ViewThatFits(in: .horizontal) {
            bar(subtitle: true, mark: true, refresh: true)
            bar(subtitle: false, mark: true, refresh: true)
            bar(subtitle: false, mark: false, refresh: true)
            bar(subtitle: false, mark: false, refresh: false)
        }
        .padding(.leading, Theme.pad + 44).padding(.trailing, Theme.pad)   // 左边让出红绿灯（隐藏标题栏）
        .frame(height: Theme.barHeight)
        .background(Theme.paper)
        .overlay(alignment: .bottom) { Rule(color: Theme.ink) }
    }

    private func bar(subtitle: Bool, mark: Bool, refresh: Bool) -> some View {
        HStack(spacing: 16) {
            HStack(alignment: .firstTextBaseline, spacing: 10) {
                Text("BOXZ OFFICE").font(Theme.Font.brand).tracking(Theme.Track.brand)
                if subtitle { Text(tr("值班台", "Duty desk")).font(Theme.Font.small).foregroundStyle(Theme.ink3) }
            }
            .fixedSize()
            HStack(spacing: 0) { ForEach(Page.allCases) { Tab(desk: desk, page: $0) } }.fixedSize()
            Spacer(minLength: 0)
            if mark && desk.writes.mode == .isolated { Chip(text: tr("隔离写入", "Isolated"), style: .mid).fixedSize() }       // 只在隔离 home（测试）上标
            if refresh {
                Button(tr("刷新", "Refresh")) { Task { await desk.refresh() } }
                    .buttonStyle(.plain).font(Theme.Font.tab).foregroundStyle(Theme.ink2).lineLimit(1).fixedSize()
                    .padding(.horizontal, 10).frame(minHeight: Theme.control).keyboardShortcut("r").focusRing()
            }
            BossButton(desk: desk).fixedSize()
        }
    }
}

private struct Tab: View {
    @ObservedObject var desk: DeskModel
    let page: Page
    @State private var hover = false
    var body: some View {
        let on = desk.page == page
        Button { desk.page = page } label: {
            Text(page.title).font(Theme.Font.tab).lineLimit(1).fixedSize()
                .padding(.horizontal, 12).frame(maxHeight: .infinity)
                .foregroundStyle(on || hover ? Theme.ink : Theme.ink3)
                .overlay(alignment: .bottom) {
                    if on { Rectangle().fill(Theme.ink).frame(height: 3).padding(.horizontal, 14).offset(y: 1) }
                }
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain).onHover { hover = $0 }.focusRing()
    }
}

/// 右上角"老板"按钮：灯 + 名字 + 待办 + 红色计数。计数读不到时写"未知"，不画成 0。
private struct BossButton: View {
    @ObservedObject var desk: DeskModel
    @State private var hover = false
    var body: some View {
        Button { desk.drawer = desk.drawer == .inbox ? nil : .inbox } label: {
            HStack(spacing: 10) {
                Lamp(kind: .human)
                VStack(alignment: .leading, spacing: 0) {
                    Text(desk.whoami.text { $0 }).font(Theme.Font.small.weight(.semibold)).lineLimit(1)
                    Text(tr("待办", "To do")).font(Theme.Font.sub).foregroundStyle(Theme.ink3)
                }
                if let box = desk.inbox.value, let requests = desk.requests.value {
                    let approvals = requests.items.filter { $0.bossCanApprove == true }.count
                    CountBlock(n: box.waiting + approvals, lowerBound: !box.complete || !requests.complete)
                } else { Text(tr("未知", "Unknown")).font(Theme.Font.sub) }
            }
            .padding(.leading, 12).padding(.trailing, 6).frame(height: Theme.control)
            .background(hover ? Theme.paper2 : .clear, ignoresSafeAreaEdges: [])
            .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
        }
        .buttonStyle(.plain).onHover { hover = $0 }.focusRing()
    }
}

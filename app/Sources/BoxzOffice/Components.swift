import SwiftUI
import AppKit
import OfficeClient

/// 一个读口的显示状态。"还没读"和"读了是零"是两件事，所以单独有 `.loading`。
enum Panel<T: Sendable>: Sendable {
    case loading
    case done(Outcome<T>)

    var value: T? { if case .done(.ok(let v)) = self { return v }; return nil }

    /// 成功就按 `ok` 写；没成就写一个词（读取中 / 被拒绝 / 未知 / 未连接）——全站"读不到不画成 0"的唯一出口。
    func text(_ ok: (T) -> String) -> String {
        switch self {
        case .loading: tr("读取中", "Loading")
        case .done(.ok(let v)): ok(v)
        case .done(.refused): tr("被拒绝", "Refused")
        case .done(.unknown): tr("未知", "Unknown")
        case .done(.notConnected): tr("未连接", "Not connected")
        }
    }
    /// 读口没成时的那个词；成功 = nil。`—` 只留给"读到了，没有"。
    var fact: String? { value == nil ? text { _ in "" } : nil }
}

// MARK: - 基础件（直角、1px 线、无阴影；状态永远配文字）

struct Rule: View {
    var color: Color = Theme.rule
    var body: some View { Rectangle().fill(color).frame(height: Theme.Line.hair) }
}

/// 11×11 方块：实心 = 有 / 空心 = 无 / 虚线空心 = 未知。
struct Square: View {
    var filled = true
    var dashed = false
    var tone: Color? = nil
    var body: some View {
        let color = tone ?? Theme.ink
        Rectangle().fill(filled ? color : .clear)
            .overlay(Rectangle().strokeBorder(color, style: StrokeStyle(lineWidth: Theme.Line.outline, dash: dashed ? [2, 2] : [])))
            .frame(width: Theme.marker, height: Theme.marker)
    }
}

/// 状态灯：老板 = 圆；未知 = 虚线空心（后端没有这个事实，不画成"空闲"或"离线"）。
struct Lamp: View {
    enum Kind { case human, unknown }
    var kind: Kind = .unknown
    var body: some View {
        switch kind {
        case .human: Circle().fill(Theme.ink).frame(width: Theme.marker, height: Theme.marker)
        case .unknown: Square(filled: false, dashed: true, tone: Theme.ink3)
        }
    }
}

/// 方角小标签（设计稿 .chip）：描边 / 红底（有信在等你）。
struct Chip: View {
    enum Style { case plain, mid, red, dashed }
    let text: String
    var style: Style = .plain
    var body: some View {
        Text(text).font(Theme.Font.badge).tracking(Theme.Track.badge)
            .padding(.horizontal, 7).padding(.vertical, 1)
            .foregroundStyle(fg)
            .background(style == .red ? Theme.red : .clear, ignoresSafeAreaEdges: [])
            .overlay(Rectangle().strokeBorder(border, style: StrokeStyle(lineWidth: Theme.Line.hair, dash: style == .dashed ? [3, 2] : [])))
            .fixedSize()
    }
    private var fg: Color {
        switch style { case .red: Theme.onRed; case .mid: Theme.ink; default: Theme.ink2 }
    }
    private var border: Color {
        switch style { case .red: .clear; case .mid: Theme.ink3; case .dashed: Theme.wire; default: Theme.rule }
    }
}

/// 计数块：红 = 有人在等你；描边 = 中性计数。
struct CountBlock: View {
    let n: Int
    var lowerBound = false          // 没读全：真数只会更大
    var body: some View {
        Text(bounded(n, atLeast: lowerBound)).font(Theme.Font.count)
            .foregroundStyle(n > 0 ? Theme.onRed : Theme.ink)
            .padding(.horizontal, 6).frame(minWidth: 24).frame(height: 24)
            .background(n > 0 ? Theme.red : .clear, ignoresSafeAreaEdges: [])
            .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: n > 0 ? 0 : Theme.Line.hair))
    }
}

func deliveryText(_ count: Int?) -> String {
    guard let count else { return tr("待投递 未知", "Delivery pending: unknown") }
    return tr("待投递 \(count)", "Delivery pending: \(count)")
}

/// 读口不是"读到了"时的单格显示：未知 / 未连接 / 被拒绝 各说各的，不画成 0。
struct FactText<T: Sendable>: View {
    let panel: Panel<T>
    var body: some View {
        if let w = panel.fact { Text(w).foregroundStyle(tone) }
    }
    private var tone: Color {
        switch panel { case .loading: Theme.ink3; case .done(.refused): Theme.bad; default: Theme.ink2 }
    }
}

/// 读口非成功时的整块显示：拒绝带后端那句话，未知/未连接各说各的。成功才走 `content`。
struct PanelBody<T: Sendable, Content: View>: View {
    let panel: Panel<T>
    @ViewBuilder let content: (T) -> Content
    var body: some View {
        switch panel {
        case .loading: Text(tr("读取中", "Loading")).font(Theme.Font.body).foregroundStyle(Theme.ink3)
        case .done(.ok(let v)): content(v)
        case .done(.refused(let code, let message)): Text("\(code)  \(message)").font(Theme.Font.body).foregroundStyle(Theme.bad)
        case .done(.unknown(let d)): Text(tr("未知  \(d)", "Unknown  \(d)")).font(Theme.Font.body).foregroundStyle(Theme.ink2)
        case .done(.notConnected(let d)): Text(tr("未连接  \(d)", "Not connected  \(d)")).font(Theme.Font.body).foregroundStyle(Theme.ink2)
        }
    }
}

/// 方框按钮（设计稿 .close-x / .btn）：44 高、1px ink 描边，悬停反色。
struct BoxButton: View {
    let title: String
    var primary = false
    var disabled = false
    let action: () -> Void
    @State private var hover = false
    var body: some View {
        let hover = hover && !disabled
        Button(action: action) {
            Text(title).font(Theme.Font.button).padding(.horizontal, 16).frame(minWidth: 44, minHeight: Theme.control)
                .foregroundStyle(primary || hover ? Theme.paper : Theme.ink)
                .background(primary || hover ? Theme.ink : .clear, ignoresSafeAreaEdges: [])
                .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
        }
        .buttonStyle(.plain).onHover { self.hover = $0 }.disabled(disabled).opacity(disabled ? 0.45 : 1).focusRing()
    }
}

/// 统计块：34px 数字 + 13px ink-3 说明；读不到时数字位置是文字"未知"，不画成 0。
struct Stat: View {
    let value: String
    let label: String
    var muted = false               // 没有事实源的"未知"：小一档、ink-3，不和真数字抢层级
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(value).font(muted ? Theme.Font.blockTitle : Theme.Font.stat).tracking(muted ? 0 : Theme.Track.stat)
                .foregroundStyle(muted ? Theme.ink3 : Theme.ink)
            Text(label).font(Theme.Font.small).foregroundStyle(Theme.ink3)
        }
        .fixedSize()
    }
}

/// 红色主块：有信在等你。零值变描边版。
struct WaitingBlock: View {
    let panel: Panel<Inbox>
    let action: () -> Void
    @State private var hover = false
    var body: some View {
        Button(action: action) {
            HStack(spacing: 14) {
                if let box = panel.value {
                    Text(bounded(box.waiting, atLeast: !box.complete)).font(Theme.Font.ctaNum).tracking(Theme.Track.stat)
                    Text(tr("封信\n在等你", "letters\nwaiting")).font(Theme.Font.button).lineSpacing(0)
                } else {
                    FactText(panel: panel).font(Theme.Font.ctaNum)
                    Text(tr("收件箱", "Inbox")).font(Theme.Font.button)
                }
            }
            .padding(.leading, 14).padding(.trailing, 20).padding(.vertical, 12).frame(minHeight: 64)
            .foregroundStyle(filled ? (hover ? Theme.paper : Theme.onRed) : (hover ? Theme.paper : Theme.ink))
            .background(filled ? (hover ? Theme.ink : Theme.red) : (hover ? Theme.ink : .clear), ignoresSafeAreaEdges: [])
            .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: filled ? 0 : Theme.Line.hair))
        }
        .buttonStyle(.plain).onHover { hover = $0 }.focusRing()
    }
    private var filled: Bool { (panel.value?.waiting ?? 0) > 0 }
}

/// Work 状态标记：01–04 就是后端四态；方框里的数字 + 旁边的文字，不靠颜色。
struct WorkMark: View {
    let state: WorkState
    var label = false
    var body: some View {
        HStack(spacing: 7) {
            Text(state.number).font(Theme.Font.mark).frame(width: 21, height: 17)
                .foregroundStyle(state == .done || state == .cancelled ? Theme.ink3 : Theme.ink)
                .overlay(Rectangle().strokeBorder(border, lineWidth: Theme.Line.hair))
                .opacity(state == .cancelled ? 0.55 : 1)
            if label { Text(state.displayName).font(Theme.Font.small).foregroundStyle(Theme.ink2) }
        }
    }
    private var border: Color { state == .submitted ? Theme.red : (state == .active ? Theme.ink : Theme.ink3) }
}

// MARK: - 页面外壳（五页共用）

/// 纵向滚动 + 统一边距 + 最大宽度。
struct PageScaffold<Content: View>: View {
    @ViewBuilder let content: Content
    var body: some View {
        ScrollView(.vertical, showsIndicators: false) {
            VStack(alignment: .leading, spacing: 0) { content }
                .padding(.horizontal, Theme.pad).padding(.top, Theme.blockTop).padding(.bottom, Theme.blockBottom)
                .frame(maxWidth: Theme.maxContent, alignment: .leading)
                .frame(maxWidth: .infinity)
        }
    }
}

/// 页标题行：大标题 + （夹具数据时）"模拟数据"标 + 右侧统计。
struct PageTitle<Stats: View>: View {
    let title: String
    var fixture = false
    @ViewBuilder let stats: Stats
    var body: some View {
        HStack(alignment: .bottom) {
            Text(title).font(Theme.Font.viewTitle).tracking(Theme.Track.viewTitle).padding(.vertical, -6)
            if fixture { Chip(text: tr("模拟数据", "Sample data"), style: .dashed).padding(.leading, 20).padding(.bottom, 14) }
            Spacer()
            stats.padding(.bottom, 4)
        }
    }
}

/// 页脚："读取于 hh:mm:ss"；前面可带"这一页的读口没成"的一个词。
struct ReadAt: View {
    @ObservedObject var desk: DeskModel
    var what: String? = nil
    var fact: String? = nil
    var body: some View {
        HStack(spacing: 12) {
            if let fact { Text(what ?? ""); Text(fact) }
            if let t = desk.refreshedAt { Text(tr("读取于 ", "Read at ") + t.formatted(date: .omitted, time: .standard)) }
        }
        .font(Theme.Font.small).foregroundStyle(Theme.ink3)
    }
}

/// 键盘焦点环（DESIGN_SYSTEM §3）：2px ink，外偏 2。系统默认的蓝色焦点环关掉，自己画。
/// 键盘焦点环只在用户真的用键盘（Tab / 方向键等）之后才画；鼠标点击或刚启动时的默认焦点不画。
@MainActor final class KeyNav: ObservableObject {
    static let shared = KeyNav()
    @Published var keyboard = false
    private init() {
        NSEvent.addLocalMonitorForEvents(matching: [.keyDown, .leftMouseDown, .rightMouseDown]) { [weak self] e in
            let kb = e.type == .keyDown
            MainActor.assumeIsolated { if self?.keyboard != kb { self?.keyboard = kb } }
            return e
        }
    }
}

struct FocusRing: ViewModifier {
    @FocusState private var focused: Bool
    @ObservedObject private var nav = KeyNav.shared
    func body(content: Content) -> some View {
        content.focusable().focusEffectDisabled().focused($focused)
            .overlay { if focused && nav.keyboard { Rectangle().strokeBorder(Theme.ink, lineWidth: 2).padding(-4).allowsHitTesting(false) } }
    }
}
extension View { func focusRing() -> some View { modifier(FocusRing()) } }

func projectManagerLabel(_ project: ProjectDetail) -> String {
    let name = project.name.isEmpty ? project.ref : project.name
    return name + tr(" · 经理", " · Manager")
}

/// 人的名字（M4）：标题 = 岗位名 / "项目经理" / "个人身份"，次级行 = 固定缩写 id；`full` 时（抽屉）再给完整 id，可选中复制。
struct PersonName: View {
    let id: String
    let holders: [String: [String]]
    var context: PersonLabel.Context = .person
    var tag: String? = nil          // 比如tr("经理", "Manager")
    var titleFont: Font = Theme.Font.bodyBold
    var full = false
    var body: some View {
        let l = PersonLabel.resolve(id, holders: holders, context: context)
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 9) {
                Text(l.title).font(titleFont).lineLimit(2)
                if let tag { Text(tag).font(Theme.Font.small).foregroundStyle(Theme.ink3) }
            }
            if let s = l.shortID, !full { Text(s).font(Theme.Font.mono).foregroundStyle(Theme.ink3).lineLimit(1) }   // 次级行，固定缩写，不被系统截断
            if full, let f = l.fullID { Text(f).font(Theme.Font.mono).foregroundStyle(Theme.ink3).textSelection(.enabled) }
        }
    }
}

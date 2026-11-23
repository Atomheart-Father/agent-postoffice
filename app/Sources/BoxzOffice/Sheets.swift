import SwiftUI
import OfficeClient

private struct ContentHeight: PreferenceKey {
    static let defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = max(value, nextValue()) }
}

/// 居中的确认框（不是抽屉）：1px ink 描边、无圆角无阴影，后面压一层 scrim。点 scrim 不关（确认类的框不能误关）。
struct ConfirmFrame<Content: View>: View {
    let title: String
    @ViewBuilder let content: Content
    @State private var contentH: CGFloat = 0
    var body: some View {
        ZStack {
            Theme.scrim
            VStack(alignment: .leading, spacing: 14) {
                Text(title).font(Theme.Font.blockTitle).tracking(Theme.Track.blockTitle)
                Rule(color: Theme.ink)
                // 内容不超过 420 就贴着内容高；超过才滚动（量一次内容高度）
                ScrollView(showsIndicators: false) {
                    VStack(alignment: .leading, spacing: 0) { content }.frame(maxWidth: .infinity, alignment: .leading)
                        .background(GeometryReader { g in Color.clear.preference(key: ContentHeight.self, value: g.size.height) })
                }
                .frame(height: min(contentH, 420))
                .onPreferenceChange(ContentHeight.self) { contentH = $0 }
            }
            .padding(24).frame(maxWidth: 560)
            .background(Theme.paper, ignoresSafeAreaEdges: [])
            .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
            .padding(32)
        }
    }
}

/// 宿主页"全部下线 / 下线"：先列出会下线的绑定；确认后逐个发 request.create(kind=unbind)，每一项三态逐条显示。
struct OfflineConfirm: View {
    @ObservedObject var desk: DeskModel
    let draft: OfflineDraft

    var body: some View {
        ConfirmFrame(title: draft.title) {
            Text(tr("会解除 \(draft.bindings.count) 个绑定", "Will remove \(draft.bindings.count) \(pl(draft.bindings.count, "binding"))")).font(Theme.Font.small).foregroundStyle(Theme.ink3).padding(.bottom, 6)
            if desk.offlineStarted { tally.padding(.bottom, 8) }
            ForEach(draft.bindings) { b in
                let key = "unbind:" + b.bindingID
                VStack(alignment: .leading, spacing: 4) {
                    HStack(spacing: 10) {
                        Text(desk.seatName(b.mailbox)).font(Theme.Font.bodyBold).lineLimit(1)
                        Text(b.bindingID).font(Theme.Font.mono).foregroundStyle(Theme.ink3).lineLimit(1)
                    }
                    if let a = desk.actions[key] { status(a, key) }
                    if desk.offlineFinished { Text(onlineStatus(b)).font(Theme.Font.small).foregroundStyle(Theme.ink3) }
                }
                .padding(.vertical, 10).frame(maxWidth: .infinity, alignment: .leading)
                .overlay(alignment: .bottom) { Rule() }
            }
            HStack(spacing: 8) {
                if !desk.offlineStarted {
                    BoxButton(title: tr("确认下线", "Confirm"), primary: true) { desk.confirmOffline() }
                    BoxButton(title: tr("取消", "Cancel")) { desk.closeOffline() }
                } else {
                    BoxButton(title: tr("关闭", "Close"), disabled: !desk.offlineFinished && desk.hasSendingOffline) { desk.closeOffline() }
                }
            }
            .padding(.top, 16)
        }
    }

    /// 重读后这个绑定还在活动列表里吗（以后端重读为准）。
    private func onlineStatus(_ b: HostBinding) -> String {
        desk.hosts.text { $0.bindings.contains { $0.bindingID == b.bindingID } ? tr("仍在线", "Still online") : tr("已下线", "Offline") }
    }

    /// 已下线 a / 被拒 b / 未知 c / 未发 d：每一项各占一类，不合成一个整体的成功或失败。
    private var tally: some View {
        var a = 0, b = 0, c = 0, d = 0
        for x in draft.bindings {
            switch desk.actions["unbind:" + x.bindingID]?.phase {
            case .done?: a += 1
            case .refused?, .versionChanged?: b += 1
            case .unknown?, .notConnected?: c += 1
            default: d += 1
            }
        }
        return Text(tr("已下线 \(a) · 被拒 \(b) · 未知 \(c) · 未发 \(d)", "Offline \(a) · Refused \(b) · Unknown \(c) · Not sent \(d)"))
            .font(Theme.Font.small.weight(.semibold)).monospacedDigit()
    }

    @ViewBuilder private func status(_ a: ActionState, _ key: String) -> some View {
        HStack(spacing: 10) {
            PhaseLine(phase: a.phase, intent: a.intent)
            switch a.phase {
            case .unknown, .notConnected: BoxButton(title: tr("重试", "Retry")) { desk.confirmAction(key) }
            default: EmptyView()
            }
        }
    }
}

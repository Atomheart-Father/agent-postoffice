import SwiftUI
import AppKit
import OfficeClient

/// 本机偏好（只存在本机 UserDefaults，不碰后端）。
enum Prefs {
    static let appearanceKey = "appearance"     // system | light | dark
    static let languageKey = "uiLanguage"       // zh | en

    @MainActor static func applyAppearance() {
        switch UserDefaults.standard.string(forKey: appearanceKey) {
        case "light": NSApp.appearance = NSAppearance(named: .aqua)
        case "dark": NSApp.appearance = NSAppearance(named: .darkAqua)
        default: NSApp.appearance = nil
        }
    }
}

/// Settings：管理员审批、界面语言和主题。
/// 版式取自设计稿 H-设置：每节一条 1px ink 顶线，节标题 28px，行 1px 线、最小 56 高。
struct SettingsPage: View {
    @ObservedObject var desk: DeskModel
    @AppStorage(Prefs.appearanceKey) private var appearance = "system"

    var body: some View {
        PageScaffold {
                PageTitle(title: "Settings") { EmptyView() }
                section(tr("管理员审批", "Admin approval")) {
                    row(tr("现任管理员", "Current admin"), sub: nil) { Text(adminHolderText).font(Theme.Font.body).foregroundStyle(Theme.ink2) }
                    row(tr("审批开关", "Approval switch"), sub: nil) {
                        HStack(spacing: 12) {
                            if desk.isFixture { Chip(text: tr("模拟数据", "Sample data"), style: .dashed) }
                            Segmented(options: [("on", tr("在岗", "On duty")), ("off", tr("停用", "Off"))],
                                      selection: Binding(get: { desk.adminApproval.value?.switchKey ?? "" }, set: { key in
                                          guard key != desk.adminApproval.value?.switchKey else { return }
                                          desk.beginAction("admin-approval", .adminApproval(enabled: key == "on"))
                                      }),
                                      disabled: approvalButtonsDisabled)
                        }
                    }
                    ActionBox(desk: desk, key: "admin-approval")
                    if desk.adminApproval.value?.hasApprover == false {
                        row(tr("管理员会话", "Admin session"), sub: nil) {
                            Text(tr("请先绑定管理员会话", "Assign an admin session first"))
                                .font(Theme.Font.body).foregroundStyle(Theme.ink2)
                        }
                    }
                }
                section(tr("界面", "Interface")) {
                    row(tr("语言", "Language"), sub: nil) {
                        Segmented(options: [("zh", "中文"), ("en", "English")],
                                  selection: Binding(get: { desk.lang.rawValue }, set: { desk.lang = Lang(rawValue: $0) ?? .zh }))
                    }
                    row(tr("主题", "Theme"), sub: nil) {
                        Segmented(options: [("system", tr("跟随系统", "System")), ("light", tr("浅色", "Light")), ("dark", tr("深色", "Dark"))], selection: $appearance)
                    }
                }
                .onChange(of: appearance) { _ in Prefs.applyAppearance() }
        }
    }

    private var approvalButtonsDisabled: Set<String> {
        if desk.isFixture || desk.adminApproval.value?.switchOn == nil || desk.actions["admin-approval"] != nil {
            return ["on", "off"]
        }
        return desk.adminApproval.value?.hasApprover == true ? [] : ["on"]
    }

    /// 现任管理员：lookup 读到的 holder 过 PersonLabel；岗位在但没人持有写"空缺"，读失败写"未知 / 没连上"。
    private var adminHolderText: String {
        switch desk.adminHolder {
        case .done(.ok(let h)):
            guard let h, !h.isEmpty else { return tr("空缺", "Vacant") }
            let l = PersonLabel.resolve(h, holders: desk.holders, context: .person)
            return l.shortID.map { l.title + " · " + $0 } ?? l.title
        default: return desk.adminHolder.text { _ in "" }
        }
    }

    private func section<C: View>(_ title: String, @ViewBuilder _ content: () -> C) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Rectangle().fill(Theme.ink).frame(height: Theme.Line.hair)
            Text(title).font(Theme.Font.sectionTitle).tracking(Theme.Track.blockTitle).padding(.top, 14)
            content()
        }
        .padding(.top, 42)
    }

    private func row<C: View>(_ label: String, sub: String?, @ViewBuilder _ trailing: () -> C) -> some View {
        HStack(spacing: 0) {
            Text(label).font(Theme.Font.bodyBold).frame(width: 198, alignment: .leading)
            if let sub { Text(sub).font(Theme.Font.f14).foregroundStyle(Theme.ink2) }
            Spacer(minLength: 16)
            trailing()
        }
        .frame(minHeight: 70)
        .overlay(alignment: .bottom) { Rule() }
    }
}

/// 分段按钮（设计稿 .seg）：44 高，选中的是 ink 实心。
private struct Segmented: View {
    let options: [(String, String)]
    @Binding var selection: String
    var disabled: Set<String> = []
    var body: some View {
        HStack(spacing: 6) {
            ForEach(options, id: \.0) { key, title in
                let on = selection == key
                Button { selection = key } label: {
                    Text(title).font(Theme.Font.button).padding(.horizontal, 16).frame(minWidth: 44, minHeight: Theme.control)
                        .foregroundStyle(on ? Theme.onKey : Theme.ink)
                        .background(on ? Theme.keyOn : .clear, ignoresSafeAreaEdges: [])
                        .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
                }
                .buttonStyle(.plain).focusRing().disabled(disabled.contains(key)).opacity(disabled.contains(key) ? 0.45 : 1)
            }
        }
    }
}

import SwiftUI
import OfficeClient

/// 按绑定查看 token 用量，逻辑地址用于识别当前线程。
struct UsagePage: View {
    @ObservedObject var desk: DeskModel

    var body: some View {
        PageScaffold {
            PageTitle(title: "Usage", fixture: desk.isFixture) { stats }
            PanelBody(panel: desk.threads) { page in
                VStack(alignment: .leading, spacing: 0) {
                    if page.threads.isEmpty { Text(tr("0 个线程", "0 threads")).font(Theme.Font.body).foregroundStyle(Theme.ink3).padding(.top, 40) }
                    else {
                        hostStrip(groupedByHost(page.threads, harness: \.harness)).padding(.top, 34)
                        ThreadTable(threads: Self.ranked(page.threads), hosts: desk.hosts.value?.threads ?? []).padding(.top, 30)
                    }
                }
            }
            ReadAt(desk: desk).padding(.top, 40)
        }
    }

    /// 只排序：入 + 出从多到少；"未知"排最后（不当 0）。缓存不进排序。
    static func ranked(_ all: [ThreadUsage]) -> [ThreadUsage] {
        all.enumerated().sorted { l, r in
            switch (l.element.cell.inOut, r.element.cell.inOut) {
            case (let a?, let b?): return a != b ? a > b : l.offset < r.offset
            case (_?, nil): return true
            case (nil, _?): return false
            default: return l.offset < r.offset
            }
        }.map(\.element)
    }

    /// 每个宿主一格：线程数 + token 合计。
    private func hostStrip(_ groups: [HostGroup<ThreadUsage>]) -> some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 200, maximum: 280), spacing: 12, alignment: .topLeading)], alignment: .leading, spacing: 12) {
            ForEach(groups, id: \.name) { g in
                VStack(alignment: .leading, spacing: 6) {
                    Text(g.name).font(Theme.Font.small.weight(.bold))
                    Text(Sum.inOut(g.items)).font(Theme.Font.rowTitle).monospacedDigit()
                    Text(Self.countText(g.items)).font(Theme.Font.sub).foregroundStyle(Theme.ink3)
                }
                .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                .overlay(Rectangle().strokeBorder(Theme.ink, lineWidth: Theme.Line.hair))
            }
        }
    }

    /// "n 个线程"只数带 binding_id 的行；没有绑定的用量记录单独写，不算线程。
    static func countText(_ items: [ThreadUsage]) -> String {
        let n = items.filter(\.isThread).count, u = items.count - n
        let t = tr("\(n) 个线程", "\(n) \(pl(n, "thread"))")
        return u == 0 ? t : (n == 0 ? tr("\(u) 条无绑定记录", "\(u) unbound \(pl(u, "record"))") : t + tr(" · \(u) 条无绑定记录", " · \(u) unbound"))
    }

    @ViewBuilder private var stats: some View {
        HStack(alignment: .bottom, spacing: Theme.statGap) {
            Stat(value: desk.threads.text { Sum.inOut($0.threads) }, label: tr("token（入+出）", "token (in+out)"))
            Stat(value: desk.threads.text { "\($0.threadCount)" }, label: tr("线程", "threads"))
        }
    }
}

// MARK: - 数字

enum TokenFmt {
    /// 1,234 → 1.2K；1,234,567 → 1.23M。
    static func short(_ n: Int) -> String {
        switch n {
        case ..<1_000: "\(n)"
        case ..<1_000_000: String(format: "%.1fK", Double(n) / 1_000)
        default: String(format: "%.2fM", Double(n) / 1_000_000)
        }
    }
}

/// 合计（入 + 出）：每一行都有数才是确切数；有一行说"未知"，合计就是"未知"——不自己算"≥ 已知部分"。
enum Sum {
    static func inOut(_ ts: [ThreadUsage]) -> String {
        if ts.isEmpty { return "—" }
        var sum = 0
        for t in ts { guard let x = t.cell.inOut else { return tr("未知", "Unknown") }; sum += x }
        return TokenFmt.short(sum)
    }
}

private struct ThreadTable: View {
    let threads: [ThreadUsage]
    let hosts: [HostThread]
    private static let wNum: CGFloat = 70, wHost: CGFloat = 84, wAddress: CGFloat = 140

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                head(tr("线程", "THREAD"), nil, .leading)
                head(tr("逻辑地址", "LOGICAL ADDRESS"), Self.wAddress, .leading)
                head(tr("宿主", "HOST"), Self.wHost, .leading)
                head(tr("入", "IN"), Self.wNum, .trailing)
                head(tr("出", "OUT"), Self.wNum, .trailing)
                head(tr("入+出", "IN+OUT"), Self.wNum, .trailing)
                head(tr("缓存", "CACHE"), Self.wNum, .trailing)
            }
            .padding(.horizontal, 10).frame(height: 34)
            .overlay(alignment: .bottom) { Rectangle().fill(Theme.ink).frame(height: Theme.Line.hair) }
            ForEach(threads) { t in
                HStack(spacing: 0) {
                    Text(t.bindingID == nil ? tr("来源未知", "Unknown source") + " · " + t.actor : t.actor)
                        .font(Theme.Font.body).lineLimit(1).help(t.actor)
                        .frame(maxWidth: .infinity, alignment: .leading)
                    let address = HostThread.currentAddress(for: t, in: hosts)
                    Text(address ?? "—").font(Theme.Font.body).lineLimit(1).help(address ?? "")
                        .frame(width: Self.wAddress, alignment: .leading)
                    Text(hostName(t.harness)).font(Theme.Font.small).foregroundStyle(Theme.ink2).lineLimit(1).frame(width: Self.wHost, alignment: .leading)
                    num(t.cell.tokensIn); num(t.cell.tokensOut)
                    VStack(alignment: .trailing, spacing: 2) {
                        Text(t.cell.inOut.map(TokenFmt.short) ?? tr("未知", "Unknown")).font(Theme.Font.bodyBold).monospacedDigit()
                            .foregroundStyle(t.cell.inOut == nil ? Theme.ink2 : Theme.ink)
                        if t.estimated { Text(tr("含估算", "incl. estimate")).font(Theme.Font.sub).foregroundStyle(Theme.ink3) }
                    }
                    .frame(width: Self.wNum, alignment: .trailing)
                    num(t.cell.tokensCache)

                }
                .padding(.horizontal, 10).frame(minHeight: 52)
                .overlay(alignment: .bottom) { Rule() }
            }
        }
    }

    private func head(_ s: String, _ w: CGFloat?, _ a: Alignment) -> some View {
        Text(s).font(Theme.Font.badge).tracking(Theme.Track.badge).foregroundStyle(Theme.ink3)
            .frame(width: w, alignment: a).frame(maxWidth: w == nil ? .infinity : nil, alignment: a)
    }
    private func num(_ n: Int?) -> some View {
        Text(n.map(TokenFmt.short) ?? tr("未知", "Unknown")).font(Theme.Font.body).monospacedDigit()
            .foregroundStyle(n == nil ? Theme.ink3 : Theme.ink2).frame(width: Self.wNum, alignment: .trailing)
    }
}

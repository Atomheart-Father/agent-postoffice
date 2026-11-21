import AppKit
import OfficeClient

/// 启动参数：整个 App 唯一读环境变量的地方，只读一次。给隔离测试 / 截图 / 实测读取次数用，OfficeClient 不碰环境。
///   BOXZ_OFFICE_HOME   指向别的 home（缺省 = 正式 ~/office11）
///   BOXZ_READ_LOG      每次读 op 追加一行到这个文件
///   BOXZ_FIXTURES      夹具目录（requests / hosts / threads / admin .json；缺哪个用内置的）
///   BOXZ_SNAPSHOT      <路径>[@宽x高]：首轮读完摆好视图后，把窗口自己存成 PNG（默认 1440x900）
///   BOXZ_PAGE          org | host | work | usage | settings；可带参数 work:<项目> / org:unassigned / host:<绑定>
///   BOXZ_DRAWER        inbox | requests | project:<ref> | work:<ref> | seat:<地址>
///   BOXZ_OPEN_ACTION   decide:<申请>:approve|refuse | reply:<信号> | compose:<地址> | review:<work>:accept|return | offline-host:<宿主名>
/// 外观 / 语言不在这里：走 UserDefaults 的命令行参数域，`--args -appearance dark -uiLanguage en`。
struct LaunchScript {
    let socket: OfficeSocket
    let fixtures: URL?
    private let env = Self.launchEnv()

    init() {
        let e = Self.launchEnv()
        let log = e["BOXZ_READ_LOG"].map { URL(fileURLWithPath: $0) }
        let install = Bundle.main.bundleURL.deletingLastPathComponent()
        let installedHome = FileManager.default.fileExists(atPath: install.appendingPathComponent("runtime/office/officed.py").path)
            ? install.path : nil
        socket = (e["BOXZ_OFFICE_HOME"] ?? e["POSTOFFICE_HOME"] ?? installedHome).map {
            OfficeSocket(home: URL(fileURLWithPath: $0), readLog: log)
        } ?? OfficeSocket(readLog: log)
        fixtures = e["BOXZ_FIXTURES"].map { URL(fileURLWithPath: $0, isDirectory: true) }
    }

    /// 空串当没设（脚本里常写 `--env BOXZ_DRAWER=""`）。
    private static func launchEnv() -> [String: String] { ProcessInfo.processInfo.environment.filter { !$0.value.isEmpty } }

    var capturing: Bool { env["BOXZ_SNAPSHOT"] != nil }

    /// 首轮读完后跑一次；没设任何取证参数就是 nil。
    var afterFirstRound: (@MainActor (DeskModel) async -> Void)? {
        guard env["BOXZ_SNAPSHOT"] != nil || env["BOXZ_PAGE"] != nil || env["BOXZ_DRAWER"] != nil || env["BOXZ_OPEN_ACTION"] != nil else { return nil }
        return { desk in
            let snap = env["BOXZ_SNAPSHOT"].map(Self.splitSnapshot)
            if let size = snap?.size, let w = NSApp.windows.first(where: { $0.isVisible }) { w.setContentSize(size) }
            if let spec = env["BOXZ_PAGE"] { Self.openPage(spec, desk) }
            if let d = env["BOXZ_DRAWER"] { Self.openDrawer(d, desk) }
            await desk.settle()
            if let a = env["BOXZ_OPEN_ACTION"] { Self.openAction(a, desk) }
            guard let snap else { return }
            try? await Task.sleep(nanoseconds: 400_000_000)        // 等 SwiftUI 把刚摆好的视图画出来
            Self.writePNG(to: snap.path)
        }
    }

    private static func splitSnapshot(_ s: String) -> (path: String, size: CGSize) {
        let parts = s.components(separatedBy: "@")
        let wh = parts.count == 2 ? parts[1].split(separator: "x").compactMap { Double($0) } : []
        return (parts[0], wh.count == 2 ? CGSize(width: wh[0], height: wh[1]) : CGSize(width: 1440, height: 900))
    }

    @MainActor private static func openPage(_ spec: String, _ desk: DeskModel) {
        let p = spec.split(separator: ":", maxSplits: 1).map(String.init)
        guard let page = Page(rawValue: p[0]) else { return }
        desk.page = page
        guard p.count == 2 else { return }
        switch page {
        case .work:       // work:<项目>[+model=<模型>][+pick=<ref>,<ref>]
            let parts = p[1].components(separatedBy: "+")
            desk.workTab = parts[0]
            for x in parts.dropFirst() {
                if x.hasPrefix("model=") { desk.workModels = [String(x.dropFirst(6))] }
                if x.hasPrefix("pick=") { desk.workPicked = x.dropFirst(5).split(separator: ",").map(String.init) }
            }
        case .org: desk.unassignedOpen = p[1] == "unassigned"
        case .host: desk.focusBinding = p[1]
        default: break
        }
    }

    @MainActor private static func openDrawer(_ spec: String, _ desk: DeskModel) {
        let p = spec.split(separator: ":", maxSplits: 1).map(String.init)
        switch (p[0], p.count == 2 ? p[1] : "") {
        case ("inbox", _): desk.inboxTab = .letters; desk.drawer = .inbox
        case ("requests", _): desk.inboxTab = .requests; desk.drawer = .inbox
        case ("project", let r): desk.drawer = .project(r)
        case ("work", let r): desk.openWork(r)
        case ("seat", let a): desk.openSeat(a)
        default: break
        }
    }

    /// 把动作框摆出来（不发送）；填好的文字只为截图好看。
    @MainActor private static func openAction(_ spec: String, _ desk: DeskModel) {
        let p = spec.split(separator: ":", omittingEmptySubsequences: false).map(String.init)
        guard p.count >= 2 else { return }
        switch p[0] {
        case "decide":
            guard p.count >= 3, let r = desk.requests.value?.items.first(where: { $0.ref == p[1] }) else { return }
            let refuse = p[2] == "refuse"
            desk.beginAction("decide:" + r.ref, .decide(ref: r.ref, version: r.version, decision: refuse ? .refuse : .approve),
                             text: refuse ? tr("这一项先不批", "Not approving this one for now") : "")
        case "reply":
            guard let l = desk.inbox.value?.letters.first(where: { $0.id == p[1] }) else { return }
            desk.beginAction("reply:" + l.id, .reply(to: l.from, replyTo: l.id), subject: "Re: " + l.subject, text: tr("收到，同意。", "Got it, approved."))
        case "compose":
            desk.beginAction("compose:" + p[1], .compose(to: p[1]), subject: tr("演示项目的进度", "Demo project progress"), text: tr("请告诉我现在的进度。", "Please tell me the current progress."))
        case "review":
            guard p.count >= 3, let sub = desk.workDetails[p[1]]?.value?.current?.ref else { return }
            let ret = p[2] == "return"
            desk.beginAction("review:" + p[1], .review(workRef: p[1], subRef: sub, decision: ret ? .return : .accept), text: ret ? tr("需要补一页说明", "Needs one more page of explanation") : "")
        case "offline-host":              // 走真路径：和宿主页"全部下线"同一份清单
            let items = groupedByHost(desk.hosts.value?.threads ?? [], harness: \.harness).first { $0.name.lowercased() == p[1].lowercased() }?.items.flatMap(\.bindings) ?? []
            desk.askOffline(title: tr("全部下线 · ", "Take all offline · ") + p[1], bindings: items)
        default: return
        }
    }

    /// 窗口自己的内容存成 PNG（不需要屏幕录制权限）。界面从不显示令牌，所以图里也不会有。
    @MainActor private static func writePNG(to path: String) {
        guard let view = NSApp.windows.first(where: { $0.isVisible })?.contentView,
              let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        try? rep.representation(using: .png, properties: [:])?.write(to: URL(fileURLWithPath: path))
    }
}

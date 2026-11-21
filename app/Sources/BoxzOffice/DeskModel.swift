import AppKit
import Foundation
import OfficeClient

enum Page: String, CaseIterable, Identifiable {
    case org, host, work, usage, settings
    var id: String { rawValue }
    /// 导航标签（随界面语言）。
    var title: String {
        switch self { case .org: tr("组织", "Studio"); case .host: tr("宿主", "Hosts"); case .work: tr("工作", "Work"); case .usage: tr("用量", "Usage"); case .settings: tr("设置", "Settings") }
    }
}

/// 右侧抽屉里放什么。
enum Drawer: Equatable {
    case inbox
    case project(String)      // project_ref
    case work(String)         // work_ref
    case seat(String)         // 席位地址（@岗位 / 个人身份）：看它的信箱、给它写信
}

/// 宿主页"全部下线 / 下线"的确认框：先列出会下线的绑定，确认后逐个发。进行到哪一步，从每个绑定的 phase 推出来。
struct OfflineDraft: Equatable {
    var title: String
    var bindings: [HostBinding]
}

/// 当前可见的东西需要读哪些：页面 + 抽屉。一轮刷新读它们的并集；切页 / 开抽屉只读新出现的那一份。
private struct Needs {
    var mail = Set<String>()        // 要读信箱的地址（letter.read target）
    var works = Set<String>()       // 要读详情的 Work（work.get）
    var admin = false               // 现任管理员（office.lookup）
    var usage = false
    var isEmpty: Bool { mail.isEmpty && works.isEmpty && !admin && !usage }
    mutating func formUnion(_ o: Needs) {
        mail.formUnion(o.mail); works.formUnion(o.works); admin = admin || o.admin; usage = usage || o.usage
    }
}

@MainActor
final class DeskModel: ObservableObject {
    @Published var page: Page = .org {
        didSet {
            guard page != oldValue else { return }
            if page != .host { focusBinding = nil }
            load(needs(page))
        }
    }
    /// Work 页当前项目页签（nil = 第一个项目）。
    @Published var workTab: String? {
        didSet { if page == .work { load(needs(Page.work)) } }
    }
    /// 界面语言（设置页切换；存本机）。
    @Published var lang: Lang {
        didSet { Lang.current = lang; UserDefaults.standard.set(lang.rawValue, forKey: Prefs.languageKey) }
    }
    @Published var drawer: Drawer? {
        didSet { if drawer != oldValue { load(needs(drawer)) } }
    }
    /// 宿主页"全部下线 / 下线"的确认框。
    @Published var offlineDraft: OfflineDraft?
    /// 每个席位信箱（letter.read 带 target）。只在组织页 / 宿主页 / 席位抽屉可见时读。
    @Published var seatMail: [String: Panel<Inbox>] = [:]
    @Published var threads: Panel<ThreadUsagePage> = .loading
    @Published var adminApproval: Panel<AdminApproval> = .loading
    @Published var adminHolder: Panel<String?> = .loading
    /// 宿主页要高亮 / 滚到的绑定（从组织页的"未分配"点过来）。
    @Published var focusBinding: String?
    /// Work 页：模型筛选（选中的模型，对应节点高亮，其余变淡）和勾选的节点（两个以上出并排比较栏）。
    @Published var workModels: Set<String> = []
    @Published var workPicked: [String] = []
    @Published var unassignedOpen = false
    @Published var whoami: Panel<String> = .loading
    @Published var projects: Panel<[ProjectDetail]> = .loading
    @Published var work: Panel<WorkList> = .loading
    @Published var inbox: Panel<Inbox> = .loading
    @Published var workDetails: [String: Panel<WorkDetail>] = [:]
    @Published var refreshedAt: Date?
    @Published var requests: Panel<RequestPage> = .loading
    @Published var hosts: Panel<HostPage> = .loading
    @Published var inboxTab: InboxTab = .letters
    private var inboxTabChosen = false
    /// 决策入口的进行中状态，key = "decide:HR-2" / "reply:<信号>" / "review:<work_ref>" / "unbind:<binding_id>"。
    @Published var book = ActionBook()

    // 刷新时算一次的派生事实（视图只读，不在每次渲染里重算）
    @Published private(set) var holders: [String: [String]] = [:]
    /// 席位名下的件数；归不到唯一席位 / 没读全都是 nil（未知）。
    @Published private(set) var seatCounts: SeatCounts?
    /// 没挂在任何项目花名册上的活动绑定：R2 窗口减花名册（不是后端给的列表）。任何一边没读到 = 未知。
    @Published private(set) var unassigned: Panel<[HostBinding]> = .loading

    let reads: OfficeReads
    let writes: OfficeWrites
    private let contract: ContractSource
    var isFixture: Bool { contract.isFixture }

    // MARK: 读取节奏
    // 只有一个节拍：前台时读一轮、睡 30 秒；App 不活跃 / 窗口被遮挡 / 最小化时不读、也不睡
    // （没有任何定时器在转）；回到前台立刻读一次，再恢复 30 秒。没有别的定时器。
    private var isForeground = true
    private var waiter: CheckedContinuation<Void, Never>?
    private var observers: [NSObjectProtocol] = []
    /// 截图时窗口可能不在前台，照样读一轮，否则截不到图。
    private let alwaysForeground: Bool
    /// 切页 / 开抽屉触发的读排成一条链（不并发重复读）；`settle()` 等它读完。
    private var loading: Task<Void, Never>?

    init(socket: OfficeSocket, contract: ContractSource, alwaysForeground: Bool = false) {
        reads = OfficeReads(socket: socket)
        writes = OfficeWrites(socket: socket)
        self.contract = contract
        self.alwaysForeground = alwaysForeground
        lang = Lang(rawValue: UserDefaults.standard.string(forKey: Prefs.languageKey) ?? "zh") ?? .zh
        Lang.current = lang
        let names: [Notification.Name] = [
            NSApplication.didBecomeActiveNotification, NSApplication.didResignActiveNotification,
            NSWindow.didChangeOcclusionStateNotification, NSWindow.didMiniaturizeNotification,
            NSWindow.didDeminiaturizeNotification, NSWindow.didBecomeKeyNotification,
        ]
        observers = names.map { name in
            NotificationCenter.default.addObserver(forName: name, object: nil, queue: .main) { [weak self] _ in
                MainActor.assumeIsolated { self?.foregroundMayHaveChanged() }
            }
        }
    }

    private func currentlyForeground() -> Bool {
        if alwaysForeground { return true }
        guard NSApp.isActive else { return false }
        guard let w = NSApp.windows.first(where: { $0.canBecomeMain }) else { return true }   // 窗口还没建出来
        return !w.isMiniaturized && w.occlusionState.contains(.visible)
    }

    private func foregroundMayHaveChanged() {
        let now = currentlyForeground()
        guard now != isForeground else { return }
        isForeground = now
        wake()          // 变前台：立刻读；变后台：把当前的 30 秒睡眠取消，转入无限等待
    }

    private func wake() { let w = waiter; waiter = nil; w?.resume() }

    /// 睡 seconds 秒（nil = 一直等到被叫醒）；被叫醒或睡醒就返回。
    private func sleepOrWake(_ seconds: UInt64?) async {
        let sleeper = seconds.map { s in
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: s * 1_000_000_000)
                if !Task.isCancelled { self.wake() }
            }
        }
        await withCheckedContinuation { waiter = $0 }
        sleeper?.cancel()
    }

    /// `afterFirstRound`：首轮读完后跑一次（启动脚本用）。
    func run(afterFirstRound: (@MainActor (DeskModel) async -> Void)? = nil) async {
        Prefs.applyAppearance()
        isForeground = currentlyForeground()
        var hook = afterFirstRound
        while !Task.isCancelled {
            if isForeground {
                await refresh()
                if let h = hook { hook = nil; await h(self) }
            }
            await sleepOrWake(isForeground ? 30 : nil)
        }
    }

    func refresh() async {
        let reads = self.reads
        let contract = self.contract
        async let w = Task.detached { reads.context() }.value
        async let p = Task.detached { reads.projectDetails() }.value
        async let k = Task.detached { reads.workList() }.value
        async let i = Task.detached { reads.inbox() }.value
        async let r = Task.detached { contract.requests() }.value
        let context = await w
        let who = context.map(\.actor)
        async let h = Task.detached { contract.hosts(boss: who) }.value
        whoami = .done(who)
        projects = .done(await p)
        work = .done(await k)
        inbox = .done(await i)
        requests = .done(await r)
        hosts = .done(await h)
        adminApproval = .done(contract.adminApproval(context: context))
        if !inboxTabChosen, let rs = requests.value {         // 待办抽屉默认落在有事要做的那一页
            inboxTabChosen = true
            if rs.items.contains(where: { $0.bossCanApprove == true }) { inboxTab = .requests }
        }
        refreshedAt = Date()
        deriveFacts()
        prune()
        var n = needs(page)
        n.formUnion(needs(drawer))
        await read(n)
    }

    private func deriveFacts() {
        holders = PersonLabel.holders(projects.value ?? [])
        seatCounts = projects.value.flatMap { ps in work.value.flatMap { SeatCounts(projects: ps, works: $0) } }
        if case .done(let h) = hosts, case .done(let p) = projects {
            unassigned = .done(Unassigned.derive(hosts: h, projects: p))
        } else { unassigned = .loading }
    }

    /// 缓存只留当前集合里还有的（项目 / Work / 席位没了，对应的信箱、详情、用量也丢掉）。
    private func prune() {
        if projects.value != nil {
            var addrs = seatAddresses
            addrs.formUnion((hosts.value?.bindings ?? []).map(\.actor))
            if case .seat(let a) = drawer { addrs.insert(a) }
            seatMail = seatMail.filter { addrs.contains($0.key) }
        }
        if let w = work.value {
            var refs = Set(w.items.map(\.ref))
            if case .work(let r) = drawer { refs.insert(r) }
            workDetails = workDetails.filter { refs.contains($0.key) }
        }
    }

    // MARK: 读什么

    private var seatAddresses: Set<String> { Set((projects.value ?? []).flatMap { ProjectSeats.entries($0).map(\.party.to) }) }

    /// 可见项目都能切换，项目详情未读到时仍保留 Work 已给出的项目。
    var workTabs: [String] {
        Set((projects.value ?? []).map(\.ref)).union((work.value?.items ?? []).map(\.projectRef)).sorted()
    }
    var currentWorkTab: String? {
        if let t = workTab, workTabs.contains(t) { return t }
        return workTabs.first
    }

    /// 当前页签里画在图上的 Work：已完成的。进行中 / 待审核 / 已取消的不画，也不读详情。
    var doneInTab: [WorkRow] { (work.value?.items ?? []).filter { $0.projectRef == currentWorkTab && $0.state == .done } }
    /// 等老板审核的 Work：待审核、审核人是老板（所有项目）。读不全时只是下限。
    var reviewQueue: [WorkRow] { work.value?.items.filter { $0.state == .submitted && $0.reviewer == whoami.value } ?? [] }
    /// 这件的用量卡：夹具有就用夹具的（模拟数据），否则用 work.get 自己带的。
    func usageCard(_ d: WorkDetail) -> (card: WorkUsageCard?, simulated: Bool) { contract.workUsage(ref: d.row.ref, real: d.usage) }

    /// 已完成的 Work 不会再变：详情按 (ref, version) 缓存，work.list 里版本没动就不再读。
    private func isCached(_ ref: String) -> Bool {
        guard case .done(.ok(let d))? = workDetails[ref], d.row.state == .done else { return false }
        return d.row.version == work.value?.items.first { $0.ref == ref }?.version
    }

    private func needs(_ page: Page) -> Needs {
        var n = Needs()
        switch page {
        case .org: n.mail = seatAddresses                                              // 每个席位一次 letter.read(target)
        case .host: n.mail = Set((hosts.value?.bindings ?? []).map(\.actor))    // 每个活动绑定的 actor 一次
        case .work: n.works = Set(doneInTab.map(\.ref))                                   // 只读当前页签里已完成的（图上只画它们）
        case .settings: n.admin = true
        case .usage: n.usage = true
        }
        return n
    }

    private func needs(_ drawer: Drawer?) -> Needs {
        var n = Needs()
        switch drawer {
        case .seat(let a): n.mail = [a]
        case .work(let ref):
            if !(page == .work && workDetails[ref]?.value != nil) { n.works = [ref] }       // Work 页上已经读过的不再读
        case .inbox, .project, nil: break
        }
        return n
    }

    private func load(_ n: Needs) {
        guard !n.isEmpty else { return }
        let prev = loading
        loading = Task { await prev?.value; await self.read(n) }
    }

    /// 等切页 / 开抽屉触发的读都落地（启动脚本截图前用）。
    func settle() async { await loading?.value }

    private func read(_ n: Needs) async {
        let reads = self.reads
        let contract = self.contract
        for k in n.mail where seatMail[k] == nil { seatMail[k] = .loading }
        for k in n.works where workDetails[k] == nil { workDetails[k] = .loading }
        await withTaskGroup(of: Void.self) { group in
            // 读到一个落一个；没读到之前显示"读取中"。
            for k in n.mail { group.addTask { let o = reads.inbox(target: k); await MainActor.run { self.seatMail[k] = .done(o) } } }
            for k in n.works where !isCached(k) { group.addTask { let o = reads.workDetail(k); await MainActor.run { self.workDetails[k] = .done(o) } } }
            if n.admin { group.addTask { let o = reads.adminHolder(); await MainActor.run { self.adminHolder = .done(o) } } }
            if n.usage { group.addTask { let o = contract.threadUsage(); await MainActor.run { self.threads = .done(o) } } }
        }
    }

    // MARK: 视图用的小方法

    func openWork(_ ref: String) { drawer = .work(ref) }
    func openSeat(_ addr: String) { drawer = .seat(addr) }

    /// 从组织页的"未分配"跳到宿主页对应的那张卡。
    func showOnHostPage(_ bindingID: String) {
        focusBinding = bindingID
        page = .host
    }

    /// 席位的名字：岗位名 / 个人身份；认不出就是地址本身。
    func seatName(_ addr: String) -> String {
        for p in projects.value ?? [] {
            if let party = (p.positions + p.managers).first(where: { $0.to == addr }), !party.name.isEmpty { return party.name }
        }
        return PersonLabel.resolve(addr, holders: holders, context: .person).title
    }
}

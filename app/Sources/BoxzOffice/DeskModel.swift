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
            if page == .work { queueCompleted() } else { load(needs(page)) }
        }
    }
    /// Work 页当前项目页签（nil = 第一个项目）。
    @Published var workTab: String? {
        didSet { if page == .work && effectiveWorkTab(oldValue) != currentWorkTab { queueCompleted() } }
    }
    /// 界面语言（设置页切换；存本机）。
    @Published var lang: Lang {
        didSet { Lang.current = lang; UserDefaults.standard.set(lang.rawValue, forKey: Prefs.languageKey) }
    }
    @Published var drawer: Drawer? {
        didSet {
            guard drawer != oldValue else { return }
            if case .project = drawer {
                queueProjectWork()
                return
            }
            if case .work(let ref) = drawer, page == .work,
               doneInTab.contains(where: { $0.ref == ref }), workDetails[ref]?.value != nil {
                return
            }
            load(needs(drawer))
        }
    }
    /// 宿主页"全部下线 / 下线"的确认框。
    @Published var offlineDraft: OfflineDraft?
    @Published var offlineRunning = false
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
    @Published var completed: Panel<WorkList> = .loading
    @Published private(set) var completedCursor: String?
    @Published var projectWork: Panel<WorkList> = .loading
    @Published private(set) var projectCursor: String?
    @Published var reviews: Panel<WorkList> = .loading
    @Published private(set) var reviewCursor: String?
    @Published var inbox: Panel<Inbox> = .loading
    @Published var workDetails: [String: Panel<WorkDetail>] = [:]
    @Published var refreshedAt: Date?
    @Published var requests: Panel<RequestPage> = .loading
    @Published private(set) var requestCursor: String?
    @Published var hosts: Panel<HostPage> = .loading
    @Published var inboxTab: InboxTab = .letters
    private var inboxTabChosen = false
    /// 决策入口的进行中状态，key = "decide:HR-2" / "reply:<信号>" / "review:<work_ref>" / "unbind:<binding_id>"。
    @Published var book = ActionBook()

    // 刷新时算一次的派生事实（视图只读，不在每次渲染里重算）
    @Published private(set) var holders: [String: [String]] = [:]
    /// 席位名下的件数；归不到唯一席位 / 没读全都是 nil（未知）。
    /// 没挂在任何项目花名册上的活动绑定：R2 窗口减花名册（不是后端给的列表）。任何一边没读到 = 未知。
    @Published private(set) var unassigned: Panel<[HostBinding]> = .loading

    let socket: OfficeSocket
    var journal: ActionJournal?
    var journalIdentity: String?
    var journalActor: String?
    @Published var journalError: String?
    let reads: OfficeReads
    let writes: OfficeWrites
    private let contract: ContractSource
    var isFixture: Bool { contract.isFixture }

    // MARK: 读取节奏
    // 只有一个节拍：前台时读一轮、睡 30 秒；App 不活跃 / 窗口被遮挡 / 最小化时不读、也不睡
    // （没有任何定时器在转）；回到前台立刻读一次，再恢复 30 秒。没有别的定时器。
    private var isForeground = true
    private var waiter: CheckedContinuation<Void, Never>?
    private var waitID = 0
    private var pollTask: Task<Void, Never>?
    private var subscribers = 0
    private var serial = 0
    private var generations: [String: Int] = [:]
    private func ticket(_ key: String) -> Int { serial += 1; generations[key] = serial; return serial }
    private func current(_ key: String, _ value: Int) -> Bool { generations[key] == value }
    private var observers: [NSObjectProtocol] = []
    /// 截图时窗口可能不在前台，照样读一轮，否则截不到图。
    private let alwaysForeground: Bool
    /// 切页 / 开抽屉触发的读排成一条链（不并发重复读）；`settle()` 等它读完。
    private var loading: Task<Void, Never>?

    init(socket: OfficeSocket, contract: ContractSource, alwaysForeground: Bool = false) {
        let transport = contract.isFixture ? OfficeSocket(home: socket.home, offline: true) : socket
        self.socket = transport
        reads = OfficeReads(socket: transport)
        writes = OfficeWrites(socket: transport)
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
        let windows = NSApp.windows.filter { $0.canBecomeMain }
        return windows.isEmpty || windows.contains { !$0.isMiniaturized && $0.occlusionState.contains(.visible) }
    }

    private func foregroundMayHaveChanged() {
        let now = currentlyForeground()
        guard now != isForeground else { return }
        isForeground = now
        wake()          // 变前台：立刻读；变后台：把当前的 30 秒睡眠取消，转入无限等待
    }

    private func wake(_ id: Int? = nil) {
        guard id == nil || id == waitID else { return }
        let w = waiter; waiter = nil; w?.resume()
    }

    /// 睡 seconds 秒（nil = 一直等到被叫醒）；被叫醒或睡醒就返回。
    private func sleepOrWake(_ seconds: UInt64?) async {
        waitID += 1
        let id = waitID
        let sleeper = seconds.map { s in
            Task { @MainActor in
                try? await Task.sleep(nanoseconds: s * 1_000_000_000)
                if !Task.isCancelled { self.wake(id) }
            }
        }
        await withTaskCancellationHandler {
            if !Task.isCancelled { await withCheckedContinuation { waiter = $0 } }
        } onCancel: { Task { @MainActor in self.wake(id) } }
        sleeper?.cancel()
    }

    /// `afterFirstRound`：首轮读完后跑一次（启动脚本用）。
    func run(afterFirstRound: (@MainActor (DeskModel) async -> Void)? = nil) async {
        subscribers += 1
        if pollTask == nil {
            pollTask = Task { @MainActor in
                Prefs.applyAppearance()
                isForeground = currentlyForeground()
                var hook = afterFirstRound
                while !Task.isCancelled {
                    if isForeground {
                        await refresh()
                        if let h = hook { hook = nil; await h(self) }
                    }
                    if !Task.isCancelled { await sleepOrWake(isForeground ? 30 : nil) }
                }
            }
        }
        defer {
            subscribers -= 1
            if subscribers == 0 { pollTask?.cancel(); pollTask = nil; wake() }
        }
        while !Task.isCancelled { try? await Task.sleep(nanoseconds: 3_600_000_000_000) }
    }

    func refresh() async {
        let generation = ticket("refresh")
        let requestGeneration = ticket("requests")
        let reviewGeneration = ticket("reviews")
        let reads = self.reads
        let contract = self.contract
        let identity = isFixture ? nil : socket.identity()
        async let w = Task.detached { reads.context() }.value
        async let p = Task.detached { reads.projectDetails() }.value
        async let k = Task.detached { reads.workList() }.value
        let cursor = reviewCursor
        async let q = Task.detached { reads.reviewPage(cursor: cursor) }.value
        async let i = Task.detached { reads.inbox() }.value
        let requestPage = requestCursor
        async let r = Task.detached { contract.requests(cursor: requestPage) }.value
        let context = await w
        let who = context.map(\.actor)
        async let h = Task.detached { contract.hosts(boss: who) }.value
        let (projectResult, workResult, reviewResult, inboxResult, requestResult, hostResult) = await (p, k, q, i, r, h)
        guard current("refresh", generation) else { return }
        if let identity, identity == socket.identity(), case .ok(let actor) = who {
            if journalIdentity != identity || journalActor != actor {
                let candidate = ActionJournal(home: socket.home, actor: actor, endpointIdentity: identity)
                do {
                    let restored = try candidate.load()
                    book = ActionBook(); book.restore(restored)
                    journal = candidate; journalIdentity = identity; journalActor = actor; journalError = nil
                } catch {
                    journal = nil; journalIdentity = nil; journalActor = nil
                    journalError = tr("未决操作无法读取，暂不能发送", "Cannot read unresolved actions; sending is unavailable")
                }
            } else if let journal {
                do { book.sync(try journal.load()) }
                catch { journalError = tr("未决操作无法读取，暂不能发送", "Cannot read unresolved actions; sending is unavailable"); self.journal = nil }
            }
        } else if !isFixture {
            journal = nil; journalIdentity = nil; journalActor = nil
        }
        whoami = .done(who)
        projects = .done(projectResult)
        work = .done(workResult)
        if current("reviews", reviewGeneration), reviewCursor == cursor { reviews = .done(reviewResult) }
        inbox = .done(inboxResult)
        if current("requests", requestGeneration), requestCursor == requestPage { requests = .done(requestResult) }
        hosts = .done(hostResult)
        if page == .work { await fetchCompleted(cursor: completedCursor) }
        if case .project = drawer { await fetchProjectWork(cursor: projectCursor) }
        guard current("refresh", generation) else { return }
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
        if case .done(let h) = hosts, case .done(let p) = projects {
            unassigned = .done(Unassigned.derive(hosts: h, projects: p))
        } else { unassigned = .loading }
    }

    /// 缓存只留当前集合里还有的（项目 / Work / 席位没了，对应的信箱、详情、用量也丢掉）。
    private func prune() {
        if case .seat(let address) = drawer {
            seatMail = seatMail.filter { $0.key == address }
        } else {
            seatMail.removeAll()
        }
        if work.value != nil {
            var refs = Set(doneInTab.map(\.ref))
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
    private func effectiveWorkTab(_ tab: String?) -> String? {
        if let t = tab, workTabs.contains(t) { return t }
        return workTabs.first
    }
    var currentWorkTab: String? { effectiveWorkTab(workTab) }

    /// 当前页签里画在图上的 Work：已完成的。进行中 / 待审核 / 已取消的不画，也不读详情。
    var doneInTab: [WorkRow] { completed.value?.items ?? [] }
    func loadRequests(cursor: String?) async {
        requestCursor = cursor
        requests = .loading
        let generation = ticket("requests"), contract = self.contract
        let result = await Task.detached { contract.requests(cursor: cursor) }.value
        if current("requests", generation), requestCursor == cursor { requests = .done(result) }
    }

    func loadReviews(cursor: String?) async {
        let generation = ticket("reviews")
        reviewCursor = cursor
        reviews = .loading
        let reads = self.reads
        let result = await Task.detached { reads.reviewPage(cursor: cursor) }.value
        if current("reviews", generation), reviewCursor == cursor { reviews = .done(result) }
    }
    /// 这件的用量卡：夹具有就用夹具的（模拟数据），否则用 work.get 自己带的。
    func usageCard(_ d: WorkDetail) -> (card: WorkUsageCard?, simulated: Bool) { contract.workUsage(ref: d.row.ref, real: d.usage) }

    private func fetchCompleted(cursor: String?) async {
        guard let project = currentWorkTab else { completed = .loading; return }
        completedCursor = cursor
        let generation = ticket("completed")
        let reads = self.reads
        let result = await Task.detached { reads.workList(projectRef: project, state: "Done", cursor: cursor, limit: 30) }.value
        if current("completed", generation) && currentWorkTab == project && completedCursor == cursor {
            completed = .done(result)
            if case .ok(let page) = result { workPicked = workPicked.filter { ref in page.items.contains { $0.ref == ref } } }
        }
    }

    func loadCompleted(cursor: String?) async {
        workPicked = []
        completed = .loading
        await fetchCompleted(cursor: cursor)
        var n = needs(page); n.formUnion(needs(drawer)); await read(n)
    }

    private func fetchProjectWork(cursor: String?) async {
        guard case .project(let project) = drawer else { return }
        projectCursor = cursor
        let generation = ticket("projectWork")
        let reads = self.reads
        let result = await Task.detached { reads.workList(projectRef: project, cursor: cursor) }.value
        if current("projectWork", generation) && drawer == .project(project) && projectCursor == cursor { projectWork = .done(result) }
    }

    func loadProjectWork(cursor: String?) async {
        projectWork = .loading
        await fetchProjectWork(cursor: cursor)
    }

    private func queueCompleted() {
        workPicked = []
        completedCursor = nil
        completed = .loading
        let project = currentWorkTab
        let prev = loading
        loading = Task {
            await prev?.value
            guard self.page == .work, self.currentWorkTab == project else { return }
            await self.loadCompleted(cursor: nil)
        }
    }

    private func queueProjectWork() {
        projectCursor = nil
        projectWork = .loading
        let prev = loading
        loading = Task { await prev?.value; await self.loadProjectWork(cursor: nil) }
    }

    private func needs(_ page: Page) -> Needs {
        var n = Needs()
        switch page {
        case .org, .host: break
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
        case .work(let ref): n.works = [ref]
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
        let mailTickets = Dictionary(uniqueKeysWithValues: n.mail.map { ($0, ticket("mail:" + $0)) })
        let workTickets = Dictionary(uniqueKeysWithValues: n.works.map { ($0, ticket("work:" + $0)) })
        let adminTicket = n.admin ? ticket("admin") : 0
        let usageTicket = n.usage ? ticket("usage") : 0
        for k in n.mail where seatMail[k] == nil { seatMail[k] = .loading }
        for k in n.works where workDetails[k] == nil { workDetails[k] = .loading }
        await withTaskGroup(of: Void.self) { group in
            // 读到一个落一个；没读到之前显示"读取中"。
            for k in n.mail { group.addTask { let o = reads.inbox(target: k); await MainActor.run { if self.current("mail:" + k, mailTickets[k]!) { self.seatMail[k] = .done(o) } } } }
            for k in n.works { group.addTask { let o = reads.workDetail(k); await MainActor.run { if self.current("work:" + k, workTickets[k]!) { self.workDetails[k] = .done(o) } } } }
            if n.admin { group.addTask { let o = reads.adminHolder(); await MainActor.run { if self.current("admin", adminTicket) { self.adminHolder = .done(o) } } } }
            if n.usage { group.addTask { let o = contract.threadUsage(); await MainActor.run { if self.current("usage", usageTicket) { self.threads = .done(o) } } } }
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

    func deliveryPending(_ mailbox: String) -> Int? {
        hosts.value?.bindings.first { $0.mailbox == mailbox }?.deliveryPending
    }

    func deliveryAutomatic(_ mailbox: String) -> Int? {
        hosts.value?.bindings.first { $0.mailbox == mailbox }?.deliveryAutomatic
    }

    func deliveryInFlight(_ mailbox: String) -> Int? {
        hosts.value?.bindings.first { $0.mailbox == mailbox }?.deliveryInFlight
    }

    func deliveryUnresolved(_ mailbox: String) -> UnresolvedDelivery? {
        hosts.value?.bindings.first { $0.mailbox == mailbox }?.deliveryUnresolved
    }

    /// 岗位使用逻辑地址，个人信箱沿用个人身份标签。
    func seatName(_ addr: String) -> String {
        if seatAddresses.contains("@" + addr) { return "@" + addr }
        return PersonLabel.resolve(addr, holders: holders, context: .person).title
    }
}

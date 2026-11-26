import Foundation

// MARK: - 后端返回与显式演示数据共用的视图模型

/// 一条待批申请。`approvable` = 调用方（老板）能批的项；空 = 这条老板批不了，只读。
public struct PendingRequest: Sendable, Equatable, Identifiable {
    public let ref: String
    public let version: Int
    public let state: String
    public let kind: String
    public let applicant: String
    public let target: String?
    public let position: String?
    public let reason: String
    public let details: [String]
    public let cancelWork: Bool?
    public let paramsText: String
    public let body: String?
    public let createdAt: String?
    /// nil = 后端没给（未知）；`[]` = 后端明确说没有。未知不画成"你批不了"。
    public let items: [String]?
    public let approvable: [String]?
    /// 已批的项（部分批准）与决定人（已转老板时）；后端没给 = nil（未知）。
    public let approved: [String]?
    public let decider: String?
    public var id: String { ref }

    /// 老板能批：`approvable` 非空才算。nil（未知）= 不画按钮，也不写"批不了"。
    public var bossCanApprove: Bool? { approvable.map { !$0.isEmpty } }

    /// 两种形状都收：数组（取元素）或字典 `{项名: 谁能批 | null}`（取键；值为 null = 老板批不了）。
    static func itemNames(_ v: Any?) -> [String]? {
        if let a = v as? [String] { return a }
        if let d = v as? [String: Any] { return d.keys.sorted() }
        return nil
    }
    static func approvableNames(_ v: Any?) -> [String]? {
        if let a = v as? [String] { return a }
        if let d = v as? [String: Any] { return d.filter { !($0.value is NSNull) }.keys.sorted() }
        return nil
    }

    /// 缺关键字段（编号 / 版本 / 状态 / 种类 / 申请人）= 读不懂，整条丢给调用方判"未知"，不猜默认值。
    public static func parse(_ r: [String: Any]) -> PendingRequest? {
        guard let ref = r["request_ref"] as? String, let version = r["version"] as? Int,
              let state = r["state"] as? String, let kind = r["kind"] as? String,
              let applicant = r["applicant"] as? String else { return nil }
        let params = r["params"] as? [String: Any] ?? [:]
        let summaryParams = params.filter { $0.key != "body" }
        let proposed = (params["create"] as? [String: Any])?["position"] as? [String: Any]
        let scope = params["scope"] as? [String] ?? (params["scope"] as? String).map { [$0] } ?? []
        let details = [params["name"], params["action"], params["grant_ref"], proposed?["name"], proposed?["project_ref"]]
            .compactMap { $0 as? String } + scope + (params["letter_ids"] as? [String] ?? [])
        return PendingRequest(ref: ref, version: version, state: state, kind: kind, applicant: applicant,
                              target: r["target"] as? String, position: r["position"] as? String, reason: r["reason"] as? String ?? "",
                              details: details, cancelWork: kind == "retire" ? (params["cancel_work"] as? Bool ?? false) : nil, paramsText: (try? JSONSerialization.data(withJSONObject: summaryParams, options: [.prettyPrinted, .sortedKeys, .fragmentsAllowed])).flatMap { String(data: $0, encoding: .utf8) } ?? "{}", body: params["body"] as? String, createdAt: r["created_at"] as? String,
                              items: itemNames(r["items"] ?? r["actionable_items"]),
                              approvable: approvableNames(r["actionable_items"] ?? r["approvable"] ?? (r["items"] is [String: Any] ? r["items"] : nil)),
                              approved: itemNames(r["approved"]), decider: r["decided_by"] as? String ?? r["decider_ref"] as? String ?? r["decider"] as? String)
    }
}

public struct RequestPage: Sendable, Equatable {
    public let items: [PendingRequest]
    public let nextCursor: String?
    /// 有下一页 = 读到的只是一部分，"共几条"只是下限。
    public var complete: Bool { nextCursor == nil }

    public static func parse(_ r: [String: Any]) -> RequestPage? {
        guard let rows = (r["requests"] ?? r["items"]) as? [[String: Any]] else { return nil }
        let items = rows.compactMap(PendingRequest.parse)
        guard items.count == rows.count else { return nil }
        return RequestPage(items: items, nextCursor: (r["next_cursor"] as? Int).map(String.init) ?? r["next_cursor"] as? String)
    }
}

public struct UnresolvedDelivery: Sendable, Equatable {
    public let count: Int
    public let letters: [String]
    public let hasMore: Bool
    static func parse(_ raw: Any?) -> UnresolvedDelivery? {
        guard let r = raw as? [String: Any], let count = r["count"] as? Int,
              let letters = r["letters"] as? [String], let more = r["has_more"] as? Bool else { return nil }
        return UnresolvedDelivery(count: count, letters: letters, hasMore: more)
    }
}

/// 一个活动绑定（release.status 的一项）。"有活动绑定"只表示已绑定；在线 / 运行中后端没有这个事实，永远是未知。
public struct HostBinding: Sendable, Equatable, Identifiable {
    public let bindingID: String
    public let actor: String
    public let hostSessionRef: String?
    public let harness: String?
    public let position: String?
    public var mailbox: String { position ?? actor }
    public let deliveryPending: Int?
    public var deliveryAutomatic: Int? = nil
    public var deliveryInFlight: Int? = nil
    public var deliveryUnresolved: UnresolvedDelivery? = nil
    /// nil = 来源未知（后端给 null）；有值 = 后端给的一句话 / 一个状态词，原样显示。
    public let quota: String?
    public var generation: Int? = nil
    public var id: String { bindingID }

    public static func parse(_ r: [String: Any]) -> HostBinding? {
        guard let bid = r["binding_id"] as? String, let actor = r["actor"] as? String else { return nil }
        let q: String?
        switch r["quota"] {
        case let s as String: q = s
        case let o as [String: Any]:
            q = (o["blocked"] as? Bool).map { $0 ? tr("已阻塞", "Blocked") : tr("未阻塞", "Not blocked") } ?? o["state"] as? String
        default: q = nil          // null / 缺 = 未知
        }
        return HostBinding(bindingID: bid, actor: actor, hostSessionRef: r["host_session_ref"] as? String,
                           harness: r["harness"] as? String, position: r["position"] as? String,
                           deliveryPending: r["delivery_pending"] as? Int,
                           deliveryAutomatic: r["delivery_automatic"] as? Int, deliveryInFlight: r["delivery_in_flight"] as? Int,
                           deliveryUnresolved: UnresolvedDelivery.parse(r["delivery_unresolved"]), quota: q, generation: r["generation"] as? Int)
    }
}

public struct HostPage: Sendable, Equatable {
    public let bindings: [HostBinding]
    public var threads: [HostThread] { HostThread.derive(bindings) }

    public static func parse(_ r: [String: Any]) -> HostPage? {
        guard let rows = (r["windows"] ?? r["bindings"]) as? [[String: Any]] else { return nil }
        let items = rows.compactMap(HostBinding.parse)
        guard items.count == rows.count else { return nil }
        return HostPage(bindings: items)
    }

    /// 去掉老板自己的绑定（actor == whoami）：它是人的身份通道，不是 agent 线程，宿主页和未分配里都不出现。
    func excluding(actor: String) -> HostPage { HostPage(bindings: bindings.filter { $0.actor != actor }) }
}

/// R4 usage.read(group_by="binding") 的一组：与 usage_sum 同一形状——`tokens:{in,out,cache}`，缺格 = null = "未知"。
/// 分组键 (binding_id, actor, harness, position)；binding_id 为空 = 来源未知（宿主没上报绑定），不算线程。
public struct ThreadUsage: Sendable, Equatable, Identifiable {
    public let bindingID: String?
    public let actor: String
    public let harness: String?
    public let position: String?
    public let events: Int?, estimatedEvents: Int?
    /// 三个 token 格，复用 Work 用量的 UsageCell。
    public let cell: UsageCell
    public var id: String { (bindingID ?? "unbound") + "|" + actor + "|" + (harness ?? "") + "|" + (position ?? "") }
    public var estimated: Bool { (estimatedEvents ?? 0) > 0 }
    /// 只有带 binding_id 的才是线程；没有的是"没有绑定的用量记录"。
    public var isThread: Bool { bindingID != nil }

    public static func parse(_ r: [String: Any]) -> ThreadUsage? {
        guard let actor = r["actor"] as? String else { return nil }
        let t = OfficeReads.tokens(r)
        return ThreadUsage(bindingID: r["binding_id"] as? String, actor: actor, harness: r["harness"] as? String,
                           position: r["position"] as? String, events: r["events"] as? Int, estimatedEvents: r["estimated_events"] as? Int,
                           cell: UsageCell(tokensIn: t.i, tokensOut: t.o, tokensCache: t.c))
    }
}

public struct ThreadUsagePage: Sendable, Equatable {
    public let threads: [ThreadUsage]
    public var threadCount: Int { threads.filter(\.isThread).count }
    public static func parse(_ r: [String: Any]) -> ThreadUsagePage? {
        guard let rows = r["groups"] as? [[String: Any]] else { return nil }
        let items = rows.compactMap(ThreadUsage.parse)
        guard items.count == rows.count else { return nil }
        return ThreadUsagePage(threads: items)
    }
}

/// 设置页"管理员审批"：开关的设置值（默认开）+ 当前有没有可审批的管理员。nil = 后端没说 = 未知。
public struct AdminApproval: Sendable, Equatable {
    public let switchOn: Bool?
    public let hasApprover: Bool?
    /// 分段按钮的选中项：值未知（nil）时两个都不选，不把"未知"画成"在岗"。
    public var switchKey: String { switchOn.map { $0 ? "on" : "off" } ?? "" }
}

public struct OfficeContext: Sendable {
    public let actor: String
    public let adminApproval: AdminApproval?
}

/// 运行时读受控端点。演示和截图显式注入夹具。
public struct ContractSource: Sendable {
    public var isFixture: Bool { loaded != nil }
    private let reads: OfficeReads
    private let loaded: (requests: Outcome<RequestPage>, hosts: Outcome<HostPage>, threads: Outcome<ThreadUsagePage>, admin: Outcome<AdminApproval>,
                            workUsage: Outcome<[String: WorkUsageCard]>)?

    public init(socket: OfficeSocket) {
        reads = OfficeReads(socket: socket)
        loaded = nil
    }

    public init(dir: URL? = nil) {
        reads = OfficeReads()
        func load<T: Sendable>(_ name: String, builtin: String, parse: ([String: Any]) -> T?) -> Outcome<T> {
            let data: Data
            if let url = dir?.appendingPathComponent(name), FileManager.default.fileExists(atPath: url.path) {
                guard let d = try? Data(contentsOf: url) else { return .notConnected(detail: tr("夹具文件读不到", "Fixture file unreadable")) }
                data = d
            } else { data = Data(builtin.utf8) }
            guard let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any], let v = parse(obj) else {
                return .unknown(detail: tr("夹具读不懂", "Fixture not understood"))
            }
            return .ok(v)
        }
        loaded = (load("requests.json", builtin: Self.builtinRequests, parse: RequestPage.parse),
                  load("hosts.json", builtin: Self.builtinHosts, parse: HostPage.parse),
                  load("threads.json", builtin: Self.builtinThreads, parse: ThreadUsagePage.parse),
                  load("admin.json", builtin: Self.builtinAdmin) { AdminApproval(switchOn: $0["switch_on"] as? Bool, hasApprover: $0["has_approver"] as? Bool) },
                  load("work_usage.json", builtin: "{}") { o in
                      let cards = o.compactMapValues { ($0 as? [String: Any]).flatMap(WorkUsageCard.parse) }
                      return cards.count == o.count ? cards : nil
                  })
    }

    public func requests(cursor: String? = nil) -> Outcome<RequestPage> {
        var args: [String: Any] = ["limit": 100]
        if let cursor, let offset = Int(cursor) { args["cursor"] = offset }
        return loaded?.requests ?? reads.call(.requests, args, parse: RequestPage.parse)
    }
    /// R2 release.status。**统一在这里去掉老板自己的绑定**；不知道谁是老板（whoami 没读到）= 整个名单跟着 whoami 的结果，不替它猜。
    public func hosts(boss: Outcome<String>) -> Outcome<HostPage> {
        boss.flatMap { b in (loaded?.hosts ?? reads.call(.hosts, parse: HostPage.parse)).map { $0.excluding(actor: b) } }
    }
    public func threadUsage() -> Outcome<ThreadUsagePage> {
        loaded?.threads ?? reads.call(.usage, ["group_by": "binding"], parse: ThreadUsagePage.parse)
    }
    /// R6 Work 用量卡按模型拆（`by_model`）。真接口没有时：`work_usage.json` 里有这件就用它（模拟数据，整张卡换成夹具的），
    /// 没有就用 work.get 自己带的卡（模型 = 未知）。真接口带 by_model 后，这个函数改成直接返回 `real`，`simulated` 恒为 false。
    public func workUsage(ref: String, real: WorkUsageCard?) -> (card: WorkUsageCard?, simulated: Bool) {
        if case .ok(let m)? = loaded?.workUsage, let c = m[ref] { return (c, true) }
        return (real, false)
    }
    /// R-管理员审批：office.context 向老板返回开关设置值和"当前有没有可审批的管理员"。
    public func adminApproval(context: Outcome<OfficeContext>? = nil) -> Outcome<AdminApproval> {
        loaded?.admin ?? (context ?? reads.context()).map { $0.adminApproval ?? AdminApproval(switchOn: nil, hasApprover: nil) }
    }

    static let builtinRequests = #"{"items":[]}"#
    static let builtinHosts = #"{"bindings":[]}"#
    static let builtinThreads = #"{"groups":[]}"#
    static let builtinAdmin = "{}"
}

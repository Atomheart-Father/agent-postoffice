import Foundation

/// 老板面板只用这几个公开只读 op，语义与 MCP 工具一一对应（`office_tool_spec.mjs`）：
/// office_whoami → office.context、office_lookup → office.lookup、office_letter_list/read → letter.read、
/// office_work_list → work.list、office_work_get → work.get（带 history，用量卡和审核轮次在里面）。
/// 不暴露 host.*、round.* 等内部 op。
enum ReadOp: String {
    case whoami = "office.context"
    case lookup = "office.lookup"
    case letter = "letter.read"
    case workList = "work.list"       // office_work_list
    case workGet = "work.get"         // office_work_get
    case requests = "request.list"
    case hosts = "release.status"
    case usage = "usage.read"
}

public struct LetterRow: Sendable, Equatable, Identifiable {
    public let id: String
    public let from: String
    public let subject: String
    public let createdAt: String
    public let state: String
    public let letterClass: String
    public let needsReply: Bool
    /// nil = 后端没报（未知），不是"没受理"。
    public let accepted: Bool?
    /// 后端 `terminal`：这封信要的动作是否已经了结。nil = 没报（未知）。
    public let terminal: Bool?
    public let system: Bool
    /// R5：服务端 meta.system 的种类（request.pending / request.to_user / …）；普通信 null。后端没返回这一列 = nil。
    public let systemKind: String?

    /// 一封信在"在送"口径下的三种读法：在送（terminal == false 且 accepted == false）/ 已了结或已受理 / 说不准
    /// （terminal 没报；或 terminal == false 而 accepted 没报）。说不准的不进确数，单独计。
    public enum Flight: Sendable, Equatable { case inFlight, settled, unsure }
    public var flight: Flight {
        guard let t = terminal else { return .unsure }
        if t { return .settled }
        guard let a = accepted else { return .unsure }
        return a ? .settled : .inFlight
    }
    /// 申请类的系统信（只在"待批权限"里处理，信件列表不重复显示）：后端给了 system_kind 就只认那两类；
    /// R5 上线前后端不给 system_kind（nil）：按结构判——系统发的、要回复的信（这里本来就回不了，要去"待批权限"处理）。
    /// 不按标题猜；批准 / 拒绝的回执（不要回复）照常留在信件页。
    public var isRequestNotice: Bool {
        switch systemKind {
        case "request.pending", "request.to_user": true
        case nil: system && needsReply
        default: false
        }
    }
}

/// 在送封数的读法：确数 / 确数之外还有几封说不准（含未知）/ 没读全时的下限 / 一封也数不出（未知）。
public enum InFlight: Equatable, Sendable { case exact(Int), withUnknown(Int, unsure: Int), atLeast(Int), unknown }

extension Inbox {
    /// 在等老板的信：要回复、且不是申请类系统信（后者在"待批权限"里）。读不全时只是下限（`complete` 为假）。
    public var waiting: Int { letters.filter { $0.needsReply && !$0.isRequestNotice }.count }
    public var inFlight: InFlight {
        var n = 0, unsure = 0
        for l in letters { switch l.flight { case .inFlight: n += 1; case .unsure: unsure += 1; case .settled: break } }
        if !complete { return n > 0 ? .atLeast(n) : .unknown }
        return unsure == 0 ? .exact(n) : .withUnknown(n, unsure: unsure)
    }
}

/// 项目明细（`office.lookup` 带 project_ref）。岗位与参与人读不全就留空，不猜。
public struct Party: Sendable, Equatable, Identifiable {
    public let to: String
    public let name: String
    public let holder: String?
    public let roles: [String]
    /// 只认后端明确返回的空岗位；没返回 holder 不等于空缺。
    public internal(set) var isVacant = false
    public var id: String { to + "/" + roles.joined(separator: ",") }
}

public struct ProjectDetail: Sendable, Equatable, Identifiable {
    public let ref: String
    public let name: String
    public let managers: [Party]
    public let positions: [Party]
    public let participants: [Party]
    public var id: String { ref }
}

/// Work 的四态（后端 Active / Submitted / Done / Cancelled）；认不得的原样留在 `.other`。协议字面量只出现在这里。
public enum WorkState: Sendable, Equatable {
    case active, submitted, done, cancelled, other(String)

    init(_ raw: String) {
        switch raw { case "Active": self = .active; case "Submitted": self = .submitted; case "Done": self = .done; case "Cancelled": self = .cancelled; default: self = .other(raw) }
    }
    /// 还在流转：进行中或待审核（席位件数只数这两种）。
    public var isLive: Bool { self == .active || self == .submitted }
    /// 待审核：这时才轮得到审核入口。
    public var canReview: Bool { self == .submitted }
    /// 状态方框里的编号。
    public var number: String {
        switch self { case .active: "01"; case .submitted: "02"; case .done: "03"; case .cancelled: "04"; case .other: "--" }
    }
    public var displayName: String {
        switch self {
        case .active: tr("进行中", "Active"); case .submitted: tr("待审核", "Submitted"); case .done: tr("已交付", "Done")
        case .cancelled: tr("已取消", "Cancelled"); case .other(let s): s
        }
    }
}

public struct WorkRow: Sendable, Equatable, Identifiable {
    public let ref: String
    public let projectRef: String
    public let objective: String
    public let state: WorkState
    public let assignee: String
    public let reviewer: String
    public let waiting: Bool
    public let version: Int
    public var id: String { ref }
}

public struct WorkList: Sendable, Equatable {
    public let total: Int
    public let hasMore: Bool
    public let items: [WorkRow]
}

public struct SubmissionSummary: Sendable, Equatable {
    public let ref: String
    public let state: String
    public let note: String
    public let createdAt: String
}

public struct WorkDetail: Sendable, Equatable {
    public let row: WorkRow
    public let acceptance: String
    public let reviewMode: String
    public let createdAt: String
    public let current: SubmissionSummary?
    public let final: SubmissionSummary?
    /// 后端 work.get 的 available_actions（对当前身份）。nil = 没报（未知），不是"没有动作"。
    public let availableActions: [String]?
    /// 这件 Work 指向谁（work.get 的 relations[]）。nil = 后端没给（未知）；[] = 没有。
    public let relations: [WorkRelation]?
    /// 谁指向我（work.get 的 relations.reversed：归属父 / 下游 / 修正者）。target = 对端那件 Work。nil = 没给（未知）。
    public let reversed: [WorkRelation]?
    /// 已作出的审核轮次数（work.get history.reviews.count）。nil = 没给（未知），不是 0 轮。
    public let reviewRounds: Int?
    /// 用量卡（work.get history.usage）。nil = 后端没给（未知）。
    public let usage: WorkUsageCard?

    /// 审核入口只在这时出现：后端对当前身份说 available_actions 含 review、Work 在待审、且有当前提交。
    public var reviewable: SubmissionSummary? {
        availableActions?.contains("review") == true && row.state.canReview ? current : nil
    }
}

/// 接续关系：`kind` ∈ based_on / modifies / waits_for / references / belongs_to（后端原样）。
public struct WorkRelation: Sendable, Equatable, Hashable {
    public let target: String
    public let kind: String
}

/// 计数的显示：没读全时真数只会更大，前面加 ≥（全站只在这里拼）。
public func bounded(_ n: Int, atLeast: Bool) -> String { (atLeast ? "≥" : "") + "\(n)" }

public struct Inbox: Sendable, Equatable {
    public let box: String
    public let count: Int
    public let letters: [LetterRow]
    /// 后端报的总数和这次读到的行数一致才算读全；不一致，"几封在等你"只是下限。
    public var complete: Bool { count == letters.count }
}

public struct LetterPage: Sendable {
    public let text: String
    public let digest: String
    public let nextCursor: Int?
}

public struct OfficeReads: Sendable {
    let socket: OfficeSocket
    public init(socket: OfficeSocket = OfficeSocket()) { self.socket = socket }

    /// 老板自己的 actor（office.context）。
    public func whoami() -> Outcome<String> {
        context().map(\.actor)
    }

    public func context() -> Outcome<OfficeContext> {
        call(.whoami) { r in
            guard let actor = r["actor"] as? String else { return nil }
            let approval = (r["admin_approval"] as? [String: Any]).map {
                AdminApproval(switchOn: $0["enabled"] as? Bool, hasApprover: $0["available"] as? Bool)
            }
            return OfficeContext(actor: actor, adminApproval: approval)
        }
    }

    /// @office.admin 的现任：office.lookup(query: "office.admin") 的 matches 里找 `to == "@office.admin"`。
    /// 外层 nil = 读不懂；内层 nil = 岗位在但没有人持有（后端 holder 为 null）。
    public func adminHolder() -> Outcome<String?> {
        call(.lookup, ["query": "office.admin"]) { r in
            guard let ms = r["matches"] as? [[String: Any]] else { return nil }
            guard let m = ms.first(where: { ($0["to"] as? String) == "@office.admin" }) else { return .some(nil) }
            return .some(m["holder"] as? String)
        }
    }

    /// 项目列表 + 每个项目的明细（岗位、参与人）。一个项目读不到就整页未知：不把缺的当成"没有岗位"。
    public func projectDetails() -> Outcome<[ProjectDetail]> {
        let refs: Outcome<[String]> = call(.lookup) { r in
            (r["projects"] as? [[String: Any]])?.compactMap { $0["project_ref"] as? String }
        }
        return refs.flatMap { refs in
            var out: [ProjectDetail] = []
            for ref in refs {
                let one: Outcome<ProjectDetail> = call(.lookup, ["project_ref": ref]) { r in
                    guard let p = (r["projects"] as? [[String: Any]])?.first, let ref = p["project_ref"] as? String,
                          let name = p["name"] as? String else { return nil }
                    return ProjectDetail(ref: ref, name: name, managers: Self.parties(p["managers"]),
                                         positions: Self.parties(p["positions"]),
                                         participants: Self.parties(p["work_participants"]))
                }
                guard case .ok(let d) = one else { return one.map { [$0] } }      // 失败原样带出去
                out.append(d)
            }
            return .ok(out)
        }
    }

    static func parties(_ raw: Any?) -> [Party] {
        (raw as? [[String: Any]] ?? []).map { p in
            Party(to: p["to"] as? String ?? "", name: p["name"] as? String ?? "",
                  holder: p["holder"] as? String, roles: p["roles"] as? [String] ?? [],
                  isVacant: p["kind"] as? String == "position" && p["holder"] is NSNull)
        }
    }

    /// 老板可读的全部 Work（role=all），一页 200；还有更多时 `hasMore` 为真，不装作读全了。
    public func workList() -> Outcome<WorkList> {
        call(.workList, ["role": "all", "limit": 200]) { r in
            guard let rows = r["items"] as? [[String: Any]] else { return nil }
            let items = rows.compactMap(Self.workRow)
            guard items.count == rows.count else { return nil }
            return WorkList(total: r["total"] as? Int ?? items.count, hasMore: r["has_more"] as? Bool ?? false, items: items)
        }
    }

    public static func tokens(_ g: [String: Any]) -> (i: Int?, o: Int?, c: Int?) {
        let t = g["tokens"] as? [String: Any] ?? [:]
        return (t["in"] as? Int, t["out"] as? Int, t["cache"] as? Int)
    }
    public static func number(_ raw: Any?) -> Double? { (raw as? NSNumber).map(\.doubleValue) }

    public func workDetail(_ ref: String) -> Outcome<WorkDetail> {
        call(.workGet, ["work_ref": ref, "history": true]) { r in
            guard let row = Self.workRow(r) else { return nil }
            return WorkDetail(row: row, acceptance: r["acceptance"] as? String ?? "",
                              reviewMode: r["review_mode"] as? String ?? "", createdAt: r["created_at"] as? String ?? "",
                              current: Self.submission(r["current_submission"]), final: Self.submission(r["final_submission"]),
                              availableActions: r["available_actions"] as? [String],
                              relations: Self.relations(r["relations"]), reversed: Self.relations(r["relations"], "reversed"),
                              reviewRounds: ((r["history"] as? [String: Any])?["reviews"] as? [String: Any])?["count"] as? Int,
                              usage: ((r["history"] as? [String: Any])?["usage"] as? [String: Any]).flatMap(WorkUsageCard.parse))
        }
    }

    /// work.get 的 relations 是 `{relations:[{work,relation,note}], belongs_to, reversed:[{work,relation}]}`；
    /// `key` 选一向："relations" = 我指向谁，"reversed" = 谁指向我。一行读不懂 = 整向未知。
    static func relations(_ raw: Any?, _ key: String = "relations") -> [WorkRelation]? {
        guard let o = raw as? [String: Any], let rows = o[key] as? [[String: Any]] else { return nil }
        let out = rows.compactMap { r -> WorkRelation? in
            guard let t = r["work"] as? String, let k = r["relation"] as? String else { return nil }
            return WorkRelation(target: t, kind: k)
        }
        return out.count == rows.count ? out : nil
    }

    static func workRow(_ w: [String: Any]) -> WorkRow? {
        guard let ref = w["work_ref"] as? String, let state = w["state"] as? String else { return nil }
        return WorkRow(ref: ref, projectRef: w["project_ref"] as? String ?? "", objective: w["objective"] as? String ?? "",
                       state: WorkState(state), assignee: w["assignee"] as? String ?? "", reviewer: w["reviewer"] as? String ?? "",
                       waiting: (w["waiting"] as? Bool) ?? (w["waiting"] != nil && !(w["waiting"] is NSNull)),
                       version: w["version"] as? Int ?? 0)
    }

    static func submission(_ raw: Any?) -> SubmissionSummary? {
        guard let s = raw as? [String: Any], let ref = s["sub_ref"] as? String else { return nil }
        return SubmissionSummary(ref: ref, state: s["state"] as? String ?? "", note: s["note"] as? String ?? "",
                                 createdAt: s["created_at"] as? String ?? "")
    }

    /// 读信箱。`target` 省略 = 自己的箱；给了就是直读那个地址的箱（老板可读，后端判权）。
    public func inbox(target: String? = nil) -> Outcome<Inbox> {
        call(.letter, target.map { ["target": $0] } ?? [:]) { r in
            guard let box = r["box"] as? String, let rows = r["letters"] as? [[String: Any]] else { return nil }
            let letters = rows.compactMap { l -> LetterRow? in
                // 缺 needs_reply / system 就是读不懂（整页未知），不默认成 false；accepted 缺 / null = 未知，保留 nil。
                guard let id = l["letter_id"] as? String, let needs = l["needs_reply"] as? Bool, let system = l["system"] as? Bool else { return nil }
                return LetterRow(id: id, from: l["from"] as? String ?? "", subject: l["subject"] as? String ?? "",
                                 createdAt: l["created_at"] as? String ?? "", state: l["state"] as? String ?? "",
                                 letterClass: l["letter_class"] as? String ?? "",
                                 needsReply: needs,
                                 accepted: l["accepted"] as? Bool,
                                 terminal: l["terminal"] as? Bool,
                                 system: system,
                                 systemKind: l["system_kind"] as? String)
            }
            // 有一封读不懂就是整页读不懂：宁可"未知"，也不把少了的行当成零。
            guard letters.count == rows.count else { return nil }
            return Inbox(box: box, count: r["count"] as? Int ?? letters.count, letters: letters)
        }
    }

    /// 单封正文定位到当前信箱。长信下一页直接用 Core 返回的游标。
    public func letterPage(_ id: String, target: String? = nil, cursor: Int? = nil) -> Outcome<LetterPage> {
        var params: [String: Any] = ["letter_id": id]
        params["target"] = target
        params["cursor"] = cursor
        return call(.letter, params) { r in
            guard let letter = r["letter"] as? [String: Any], letter["letter_id"] as? String == id,
                  let text = letter["body"] as? String, let digest = letter["body_sha256"] as? String else { return nil }
            guard let page = letter["body_page"] else { return LetterPage(text: text, digest: digest, nextCursor: nil) }
            guard let paging = page as? [String: Any], let more = paging["has_more"] as? Bool else { return nil }
            let next = paging["cursor"] as? Int
            if more, !(next.map { $0 > (cursor ?? 1) } ?? false) { return nil }
            return LetterPage(text: text, digest: digest, nextCursor: more ? next : nil)
        }
    }

    /// 三态在这里统一折：`ok:true` → 解析（读不懂 = 未知）；`ok:false` + `unknown` → 未知；其余 `ok:false` → 明确拒绝。
    func call<T: Sendable>(_ op: ReadOp, _ params: [String: Any] = [:], parse: ([String: Any]) -> T?) -> Outcome<T> {
        switch socket.exchange(params.merging(["op": op.rawValue]) { _, op in op }) {
        case .notConnected(let d): return .notConnected(detail: d)
        case .unknown(let d): return .unknown(detail: d)
        case .refused(let c, let m): return .refused(code: c, message: m)
        case .ok(let frame):
            if frame["ok"] as? Bool == true {
                guard let result = frame["result"] as? [String: Any], let value = parse(result) else {
                    return .unknown(detail: tr("答复缺少应有的字段", "Reply is missing expected fields"))
                }
                return .ok(value)
            }
            let code = frame["code"] as? String ?? ""
            if code == "unknown" { return .unknown(detail: frame["message"] as? String ?? tr("后端报告结果未知", "The office reports the result as unknown")) }
            guard !code.isEmpty else { return .unknown(detail: tr("答复既不是成功也不是拒绝", "Reply is neither success nor refusal")) }
            return .refused(code: code, message: frame["message"] as? String ?? "")
        }
    }
}

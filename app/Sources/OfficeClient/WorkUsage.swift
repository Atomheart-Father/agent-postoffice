import Foundation

/// 一个模型的用量（用量卡 `by_model` 的一项，契约 R6）。model 为空 = 宿主没报模型 = "未知"。
public struct ModelUsage: Sendable, Equatable {
    public let model: String?
    public let events: Int?
    public let cell: UsageCell
}

/// 承办或审核一方的用量：与后端 usage_sum 同形（`tokens{in,out,cache}`，缺 = null = 未知），多一个可选的 `by_model`。
/// byModel == nil = 后端没按模型拆（模型就是"未知"）。
public struct RoleUsage: Sendable, Equatable {
    public let cell: UsageCell
    public let byModel: [ModelUsage]?
}

/// work.get history.usage：本件的用量卡。`covered == false` = 一条记录都没有（未覆盖，不是用了 0）。
public struct WorkUsageCard: Sendable, Equatable {
    public let covered: Bool
    public let assignee: RoleUsage?
    public let reviewer: RoleUsage?
    public let total: UsageCell?

    /// 节点上的 token：覆盖了才有；没覆盖 / 没给卡 = nil = 全部"未知"。
    public var spent: UsageCell? { covered ? total : nil }

    /// 这件涉及的全部模型（承办、审核里出现过的，去重，保持出现顺序）；nil 的 model 记成 nil。没拆模型 = 空。
    public var models: [String?] {
        var out: [String?] = []
        for m in (assignee?.byModel ?? []) + (reviewer?.byModel ?? []) where !out.contains(where: { $0 == m.model }) { out.append(m.model) }
        return out
    }
    /// 承办用的模型（节点第二行）。
    public var assigneeModels: [String?] {
        var out: [String?] = []
        for m in assignee?.byModel ?? [] where !out.contains(where: { $0 == m.model }) { out.append(m.model) }
        return out
    }

    static func cell(_ o: [String: Any]) -> UsageCell {
        let t = OfficeReads.tokens(o)
        return UsageCell(tokensIn: t.i, tokensOut: t.o, tokensCache: t.c)
    }
    static func role(_ raw: Any?) -> RoleUsage? {
        guard let o = raw as? [String: Any] else { return nil }
        let byModel = (o["by_model"] as? [[String: Any]])?.map {
            ModelUsage(model: ($0["model"] as? String).flatMap { $0.isEmpty ? nil : $0 }, events: $0["events"] as? Int, cell: cell($0))
        }
        return RoleUsage(cell: cell(o), byModel: byModel)
    }
    public static func parse(_ r: [String: Any]) -> WorkUsageCard? {
        guard let covered = r["covered"] as? Bool else { return nil }
        return WorkUsageCard(covered: covered, assignee: role(r["assignee"]), reviewer: role(r["reviewer"]),
                             total: (r["total"] as? [String: Any]).map(cell))
    }
}

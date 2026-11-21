import Foundation

/// 个人 id（`employee-…`）怎么显示：只用同一次 lookup 里已有的事实，不编名字。
///   1. 这个 id 是某个岗位的 holder → 标题 = 岗位名（同一个人持多个岗位就都写）；
///   2. 查不到 → 按上下文写"项目经理"或"个人身份"；标题里永远没有 id；
///   3. id 固定缩写成 `employee-92d5…3a42` 放次级行（同一个 id 到处一样）；完整 id 只在抽屉里出现。
/// 不是个人 id 的（`postoffice`、`@project.builder` 这类地址 / 系统名）原样当标题，没有缩写行。
public struct PersonLabel: Sendable, Equatable {
    public enum Context: Sendable { case manager, person }

    public let title: String
    public let shortID: String?
    public let fullID: String?

    /// holder id → 他持有的岗位名；从同一批 lookup 数据反查。
    public static func holders(_ projects: [ProjectDetail]) -> [String: [String]] {
        var out: [String: [String]] = [:]
        for p in projects {
            for pos in p.positions {
                guard let h = pos.holder, !h.isEmpty else { continue }
                let name = pos.name.isEmpty ? pos.to : pos.name
                if !(out[h] ?? []).contains(name) { out[h, default: []].append(name) }
            }
        }
        return out
    }

    public static func resolve(_ id: String, holders: [String: [String]], context: Context) -> PersonLabel {
        guard id.hasPrefix("employee-") else { return PersonLabel(title: id, shortID: nil, fullID: nil) }
        let names = holders[id] ?? []
        let title = names.isEmpty ? (context == .manager ? tr("项目经理", "Project manager") : tr("个人身份", "Individual identity")) : names.joined(separator: "、")
        return PersonLabel(title: title, shortID: abbreviate(id), fullID: id)
    }

    /// `employee-` + 前 4 位 + `…` + 后 4 位。
    public static func abbreviate(_ id: String) -> String {
        let prefix = "employee-"
        guard id.hasPrefix(prefix) else { return id }
        let rest = id.dropFirst(prefix.count)
        guard rest.count > 8 else { return id }
        return prefix + rest.prefix(4) + "…" + rest.suffix(4)
    }
}

import Foundation

/// 一个席位名下的件数（进行 / 待审）。
public struct SeatCount: Sendable, Equatable { public let active: Int; public let submitted: Int }

/// 把 work.list 的件归到项目里的席位（经理 / 岗位）。
///
/// 后端的 `assignee` 是别名或岗位名（`project.builder`、`内容协作`），lookup 的 `to` 带前导 `@`（`@project.builder`），
/// 所以匹配键 = 去掉 `@` 的 `to`、`name`。**只在能唯一匹配时给数**：
///   - 只有岗位席位有件数；经理的个人 id 不会出现在 assignee 里，经理行不写数
///   - 某个进行 / 待审的件在它所在项目里匹配不到唯一席位 → 这个项目所有席位的数都不给（未知），不写 0；
///   - work.list 没读全（hasMore）→ 整个不给（init 返回 nil）。
public struct SeatCounts: Sendable, Equatable {
    let bySeat: [String: SeatCount]      // key = project|to；缺席 = 未知

    static func key(_ project: String, _ to: String) -> String { project + "|" + to }

    public func count(project: String, to: String) -> SeatCount? { bySeat[Self.key(project, to)] }

    public init?(projects: [ProjectDetail], works: WorkList) {
        guard !works.hasMore else { return nil }
        var tallies: [String: SeatCount] = [:]
        var badProjects = Set<String>()
        let byProject = Dictionary(uniqueKeysWithValues: projects.map { ($0.ref, $0) })

        for w in works.items where w.state.isLive {
            guard let seat = byProject[w.projectRef].flatMap({ Self.uniqueSeat(for: w.assignee, in: $0) }) else {
                badProjects.insert(w.projectRef)
                continue
            }
            let k = Self.key(w.projectRef, seat)
            let old = tallies[k] ?? SeatCount(active: 0, submitted: 0)
            tallies[k] = SeatCount(active: old.active + (w.state == .active ? 1 : 0),
                                   submitted: old.submitted + (w.state == .submitted ? 1 : 0))
        }
        // 没有活的席位 = 0（读全了、且这个项目没有匹配不上的件，才敢写 0）
        var all: [String: SeatCount] = [:]
        for p in projects where !badProjects.contains(p.ref) {
            for to in p.positions.map(\.to) { all[Self.key(p.ref, to)] = tallies[Self.key(p.ref, to)] ?? SeatCount(active: 0, submitted: 0) }
        }
        bySeat = all
    }

    /// 这个 assignee 在项目里唯一对应的席位（返回席位的 `to`）；没有或不止一个 = nil。
    static func uniqueSeat(for assignee: String, in p: ProjectDetail) -> String? {
        let name = assignee.hasPrefix("@") ? String(assignee.dropFirst()) : assignee
        guard !name.isEmpty else { return nil }
        var hits = Set<String>()
        for seat in p.positions {
            let alias = seat.to.hasPrefix("@") ? String(seat.to.dropFirst()) : seat.to
            if alias == name || (!seat.name.isEmpty && seat.name == name) { hits.insert(seat.to) }
        }
        return hits.count == 1 ? hits.first : nil
    }
}

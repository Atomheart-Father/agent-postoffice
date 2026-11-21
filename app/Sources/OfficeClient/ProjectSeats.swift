import Foundation

/// 组织页里一个项目要画的行。同一个人只画一行（N1）：
/// 经理可以用岗位地址或个人 id 表示；有对应岗位时把"经理"标签并入该行。没有对应岗位的经理单列，且不写件数
/// （后端 work.list 的 assignee 是别名 / 岗位名，永远归不到个人 id，写 0 是编的）。
public struct SeatEntry: Sendable, Equatable, Identifiable {
    public let party: Party
    public let isManager: Bool       // 这一行同时是经理
    public let countable: Bool       // 只有岗位行才有件数可写
    public var id: String { party.to }

    public func addresses(in threads: [HostThread]) -> [String] {
        if countable { return [party.to] }
        return Set(threads.filter { $0.carries(party.to) }.flatMap(\.positionAddresses)).sorted()
    }
}

public enum ProjectSeats {
    public static func entries(_ p: ProjectDetail) -> [SeatEntry] {
        let managerIDs = Set(p.managers.map(\.to))
        let positionIDs = Set(p.positions.map(\.to) + p.positions.compactMap(\.holder))
        let managerOnly = p.managers.filter { !positionIDs.contains($0.to) }
            .map { SeatEntry(party: $0, isManager: true, countable: false) }
        let positions = p.positions.map { position in
            let isManager = managerIDs.contains(position.to) || (position.holder.map(managerIDs.contains) ?? false)
            return SeatEntry(party: position, isManager: isManager, countable: true)
        }
        return managerOnly + positions.filter(\.isManager) + positions.filter { !$0.isManager }
    }
}

import Foundation

public struct SeatEntry: Sendable, Equatable, Identifiable {
    public let party: Party
    public let isManager: Bool
    public var id: String { party.to }
}

public enum ProjectSeats {
    public static func entries(_ p: ProjectDetail) -> [SeatEntry] {
        let managers = Set(p.managers.map(\.to))
        var seen = Set<String>()
        let rows = (p.positions + p.managers).compactMap { party -> SeatEntry? in
            guard !party.to.isEmpty, seen.insert(party.to).inserted else { return nil }
            return SeatEntry(party: party, isManager: managers.contains(party.to))
        }
        return rows.filter(\.isManager) + rows.filter { !$0.isManager }
    }
}

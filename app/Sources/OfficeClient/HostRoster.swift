import Foundation

public struct HostThread: Sendable, Equatable, Identifiable {
    public let harness: String
    public let sessionRef: String
    public let bindings: [HostBinding]
    public var primary: HostBinding { bindings[0] }
    public var id: String { primary.bindingID }

    /// release.status 返回的是绑定。同一宿主会话可同时持有个人邮箱和多个岗位。
    static func derive(_ bindings: [HostBinding]) -> [HostThread] {
        var groups: [String: [HostBinding]] = [:]
        var order: [String] = []
        for binding in bindings {
            guard let ref = binding.hostSessionRef, !ref.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            if groups[ref] == nil { order.append(ref) }
            groups[ref, default: []].append(binding)
        }
        let grouped = order.flatMap { ref -> [HostThread] in
            let rows = groups[ref, default: []]
            let known = rows.compactMap { row in supportedHost(row.harness).map { ($0, row) } }
            var hosts = Set(known.map { $0.0 })
            let singleHost = hosts.count == 1
            return known.compactMap { host, _ in
                guard hosts.remove(host) != nil else { return nil }
                let hosted = known.filter { $0.0 == host }.map { $0.1 }
                let primary = hosted.first(where: { $0.actor.hasPrefix("employee-") }) ?? hosted[0]
                // 缺宿主的岗位绑定只有在同一个原生会话能唯一归组时才随卡显示。
                let aliases = singleHost ? rows.filter { $0.harness == nil } : []
                return HostThread(harness: host, sessionRef: ref, bindings: [primary] + hosted.filter { $0 != primary } + aliases)
            }
        }
        let covered = Set(grouped.flatMap { $0.bindings.map(\.bindingID) })
        return grouped + bindings.filter { !covered.contains($0.bindingID) }.map {
            HostThread(harness: $0.harness ?? "", sessionRef: $0.hostSessionRef ?? "", bindings: [$0])
        }
    }

    private static func supportedHost(_ value: String?) -> String? {
        guard let name = value?.lowercased(), ["codex", "claude", "opencode", "pi"].contains(name) else { return nil }
        return name
    }

    public func carries(_ address: String) -> Bool {
        bindings.contains { $0.mailbox == address }
    }

    public func managedProjects(in projects: [ProjectDetail]) -> [ProjectDetail] {
        projects.filter { project in project.managers.contains { carries($0.to) } }
    }

    public var positionAddresses: [String] {
        Set(bindings.compactMap(\.position)).sorted()
    }

    public static func currentAddress(for usage: ThreadUsage, in threads: [HostThread]) -> String? {
        guard let id = usage.bindingID,
              let thread = threads.first(where: { $0.bindings.contains { $0.bindingID == id && $0.actor == usage.actor } }),
              !thread.positionAddresses.isEmpty else { return nil }
        return thread.positionAddresses.joined(separator: " · ")
    }

    public var title: String {
        if !positionAddresses.isEmpty { return positionAddresses.joined(separator: " · ") }
        if harness == "pi", sessionRef.hasPrefix("/") {
            let name = URL(fileURLWithPath: sessionRef).deletingPathExtension().lastPathComponent
            if let id = name.split(separator: "_").last, UUID(uuidString: String(id)) != nil {
                return String(id)
            }
        }
        return sessionRef.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            ? tr("会话未知", "Session unknown") + " · " + primary.bindingID : sessionRef
    }
}

public enum Unassigned {
    /// 缺项目明细时不能把读不到席位的线程算成未分配。
    public static func derive(hosts: Outcome<HostPage>, projects: Outcome<[ProjectDetail]>) -> Outcome<[HostBinding]> {
        hosts.flatMap { h in
            projects.map { ps in
                var roster = Set<String>()
                for p in ps {
                    roster.formUnion(p.managers.map(\.to)); roster.formUnion(p.participants.map(\.to))
                    roster.formUnion(p.positions.map(\.to)); roster.formUnion(p.positions.compactMap(\.holder))
                }
                return h.bindings.filter { !roster.contains($0.actor) && !roster.contains("@" + $0.actor) }
            }
        }
    }
}

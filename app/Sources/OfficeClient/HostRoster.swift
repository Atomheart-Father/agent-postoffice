import Foundation

public enum Unassigned {
    /// 没挂在任何项目花名册上的活动绑定：从 R2 窗口中去掉项目经理、岗位地址、岗位持有人和参与人。
    /// （`hosts` 已经去掉了老板自己的）。任何一边读不到（项目明细缺一个也算）= 整个名单"未知"，不把"没读到的人"算成未分配。
    public static func derive(hosts: Outcome<HostPage>, projects: Outcome<[ProjectDetail]>) -> Outcome<[HostBinding]> {
        hosts.flatMap { h in
            projects.map { ps in
                var roster = Set<String>()
                for p in ps {
                    roster.formUnion(p.managers.map(\.to)); roster.formUnion(p.participants.map(\.to))
                    roster.formUnion(p.positions.map(\.to)); roster.formUnion(p.positions.compactMap(\.holder))
                }
                // lookup 的岗位地址带 @，release.status 的绑定 actor 不带。
                return h.bindings.filter { !roster.contains($0.actor) && !roster.contains("@" + $0.actor) }
            }
        }
    }
}

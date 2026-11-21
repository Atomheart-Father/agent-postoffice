import Foundation

/// 来历关系的一条边：箭头从来源指向后续（A 依据 / 修改了 B ⇒ B → A）。只有这两种关系画。
public struct WorkEdge: Sendable, Equatable {
    public enum Kind: String, Sendable { case basedOn = "based_on", modifies }
    public let from: String      // 来源
    public let to: String        // 后续
    public let kind: Kind
}

/// 已完成 Work 的来历图：只算分层，不管怎么画。
/// - 列 = 从来源到后续的最长路径长度（没有来源的在最左列 0；列 = 各来源的最大列 + 1）。
/// - 没有任何边的（既无来源也无后续）单独放 `isolated`，画在最右。
/// - 等待 / 属于 / 参考不画；两头不都在 `refs` 里的边不画。
public struct WorkGraph: Sendable, Equatable {
    public let layers: [[String]]
    public let isolated: [String]
    public let edges: [WorkEdge]

    /// `relations`：每件 Work 指向谁（work.get 的 relations）；某件没读到 = 缺项，当作没有出边（它仍可能作为别人的来源出现）。
    public init(refs: [String], relations: [String: [WorkRelation]]) {
        let here = Set(refs)
        var edges: [WorkEdge] = []
        for w in refs.sorted(by: Self.order) {
            for r in relations[w] ?? [] where here.contains(r.target) && r.target != w {
                guard let k = WorkEdge.Kind(rawValue: r.kind) else { continue }
                let e = WorkEdge(from: r.target, to: w, kind: k)
                if !edges.contains(e) { edges.append(e) }
            }
        }
        let connected = Set(edges.flatMap { [$0.from, $0.to] })
        let sources = Dictionary(grouping: edges, by: \.to).mapValues { $0.map(\.from) }
        var depth: [String: Int] = [:]
        func layer(_ w: String, _ path: Set<String>) -> Int {
            if let d = depth[w] { return d }
            if path.contains(w) { return 0 }                                    // 环：后端不该有，真有了就不递归下去
            let d = (sources[w] ?? []).map { layer($0, path.union([w])) + 1 }.max() ?? 0
            depth[w] = d
            return d
        }
        let nodes = refs.filter(connected.contains).sorted(by: Self.order)
        var cols: [[String]] = []
        for w in nodes { let d = layer(w, []); while cols.count <= d { cols.append([]) }; cols[d].append(w) }
        // 同列内按来源的上下位置（平均序号）排，少交叉；第一列按编号
        var pos: [String: Double] = [:]
        for (i, c) in cols.enumerated() {
            var c = c
            if i > 0 {
                func key(_ w: String) -> Double {
                    let ps = (sources[w] ?? []).compactMap { pos[$0] }
                    return ps.isEmpty ? .greatestFiniteMagnitude : ps.reduce(0, +) / Double(ps.count)
                }
                c.sort { let a = key($0), b = key($1); return a != b ? a < b : Self.order($0, $1) }
            }
            for (j, w) in c.enumerated() { pos[w] = Double(j) }
            cols[i] = c
        }
        self.layers = cols
        self.isolated = refs.filter { !connected.contains($0) }.sorted(by: Self.order)
        self.edges = edges
    }

    /// 编号顺序：数字按数值比（W2 在 W10 前）。
    static func order(_ a: String, _ b: String) -> Bool { a.compare(b, options: .numeric) == .orderedAscending }
}

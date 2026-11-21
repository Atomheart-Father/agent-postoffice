import SwiftUI
import OfficeClient

/// 来历图的版面：每列一层（左 → 右），列与列之间的空隙里走直角线；跨列的边在中间各列留一个空槽穿过，线永远不压节点和字。
struct GraphLayout {
    static let nodeW: CGFloat = 216, nodeH: CGFloat = 72, rowGap: CGFloat = 12, passH: CGFloat = 14, headH: CGFloat = 26
    static let laneStep: CGFloat = 9, gapBase: CGFloat = 46

    struct Route { let edge: WorkEdge; let points: [CGPoint] }

    private(set) var nodes: [String: CGRect] = [:]
    private(set) var routes: [Route] = []
    private(set) var size: CGSize = .zero

    init(graph: WorkGraph) {
        let layerOf = Dictionary(uniqueKeysWithValues: graph.layers.enumerated().flatMap { i, c in c.map { ($0, i) } })
        let drawn = graph.edges.enumerated().filter { (layerOf[$0.element.to] ?? 0) > (layerOf[$0.element.from] ?? 0) }   // 同层 / 倒挂（环）不画
        let n = graph.layers.count

        // 每层的槽：真节点在上，被穿过的空槽在下（按边的先后）
        enum Slot: Hashable { case node(String), pass(edge: Int, layer: Int) }
        var slots: [[Slot]] = graph.layers.map { $0.map(Slot.node) }
        for (i, e) in drawn {
            let a = layerOf[e.from]!, b = layerOf[e.to]!
            if b - a > 1 { for l in (a + 1)..<b { slots[l].append(.pass(edge: i, layer: l)) } }
        }
        func slot(_ e: Int, _ edge: WorkEdge, _ l: Int) -> Slot {
            l == layerOf[edge.from]! ? .node(edge.from) : (l == layerOf[edge.to]! ? .node(edge.to) : .pass(edge: e, layer: l))
        }

        // 线道数决定每个空隙多宽：同一个来源出去的边共用一道
        var lanes: [Int: [Slot: Int]] = [:]
        for (i, e) in drawn {
            for l in layerOf[e.from]!..<layerOf[e.to]! { _ = i
                let s = slot(i, e, l)
                if lanes[l, default: [:]][s] == nil { lanes[l, default: [:]][s] = lanes[l, default: [:]].count }
            }
        }
        var x: CGFloat = 0, colX: [CGFloat] = []
        for l in 0..<max(n, 0) { colX.append(x); x += Self.nodeW + Self.gapBase + Self.laneStep * CGFloat(lanes[l]?.count ?? 0) }

        // 纵向：从上到下叠
        var rect: [Slot: CGRect] = [:]
        var maxY: CGFloat = 0
        for l in 0..<n {
            var y = Self.headH
            for s in slots[l] {
                let h: CGFloat
                if case .node = s { h = Self.nodeH } else { h = Self.passH }
                rect[s] = CGRect(x: colX[l], y: y, width: Self.nodeW, height: h)
                y += h + Self.rowGap
            }
            maxY = max(maxY, y)
        }
        for (s, r) in rect { if case .node(let ref) = s { nodes[ref] = r } }

        for (i, e) in drawn {
            var pts: [CGPoint] = []
            let a = layerOf[e.from]!, b = layerOf[e.to]!
            for l in a..<b {
                let from = rect[slot(i, e, l)]!, to = rect[slot(i, e, l + 1)]!
                let lane = CGFloat(lanes[l]?[slot(i, e, l)] ?? 0)
                let lx = from.maxX + 22 + lane * Self.laneStep
                pts += [CGPoint(x: from.maxX, y: from.midY), CGPoint(x: lx, y: from.midY), CGPoint(x: lx, y: to.midY), CGPoint(x: to.minX, y: to.midY)]
                if l + 1 < b { pts.append(CGPoint(x: to.maxX, y: to.midY)) }          // 穿过空槽
            }
            routes.append(Route(edge: e, points: pts))
        }

        let width = n > 0 ? colX[n - 1] + Self.nodeW : 0
        size = CGSize(width: width, height: max(maxY, Self.headH))
    }
}

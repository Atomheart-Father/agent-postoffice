import Foundation

/// 一个 Work × 角色格里的 token：三个分量各自可空，nil = 未知。
public struct UsageCell: Sendable, Equatable {
    public let tokensIn: Int?, tokensOut: Int?, tokensCache: Int?
    /// 后端 cost（单位后端没说，原样当数字显示）；nil = 没报 = "未知"。
    public let cost: Double?
    public init(tokensIn: Int?, tokensOut: Int?, tokensCache: Int?, cost: Double? = nil) {
        self.tokensIn = tokensIn; self.tokensOut = tokensOut; self.tokensCache = tokensCache; self.cost = cost
    }
    /// 用量排序 / 主数字用"入 + 出"（缓存读便宜，单列，不进排序；Q1 先按保守做法）。任一个未知 = 未知。
    public var inOut: Int? {
        guard let a = tokensIn, let b = tokensOut else { return nil }
        return a + b
    }
}

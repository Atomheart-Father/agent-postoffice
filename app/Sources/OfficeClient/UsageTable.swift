import Foundation

/// 一个 Work × 角色格里的 token：三个分量各自可空，nil = 未知。
public struct UsageCell: Sendable, Equatable {
    public let tokensIn: Int?, tokensOut: Int?, tokensCache: Int?
    public init(tokensIn: Int?, tokensOut: Int?, tokensCache: Int?) {
        self.tokensIn = tokensIn; self.tokensOut = tokensOut; self.tokensCache = tokensCache
    }
    /// 输入已包含缓存读；缓存列供查看其中的缓存读。任一项未知时合计未知。
    public var inOut: Int? {
        guard let a = tokensIn, let b = tokensOut else { return nil }
        return a + b
    }
}

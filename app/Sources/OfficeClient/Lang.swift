import Foundation

/// 界面语言：中文 / English。`tr(中文, English)` 在调用处按当前语言取一句；两套文案写在一起，改一处不会漏另一处。
public enum Lang: String, CaseIterable, Sendable {
    case zh, en
    /// 当前语言（主线程在设置页切换；其余线程只读）。
    nonisolated(unsafe) public static var current: Lang = .zh
}

public func tr(_ zh: String, _ en: String) -> String { Lang.current == .en ? en : zh }

/// 英文单复数：1 个用单数。
public func pl(_ n: Int, _ word: String) -> String { n == 1 ? word : word + "s" }

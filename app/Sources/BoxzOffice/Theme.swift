import SwiftUI
import AppKit

/// 设计 token 集中在这一个文件。数值逐项取自 B 的 docs/DESIGN_SYSTEM.md（work/b-docs，03deab5）：§1 颜色、§2 字号、§3 间距与线。
/// 字体：Instrument Sans（设计稿 woff2 → TTF，B 的 §2.2）打在 .app 里，由 Fonts.swift 注册；中文回退 PingFang SC。
enum Theme {
    // MARK: 颜色（亮 paper / 暗 night，跟随系统）
    static let paper = dyn(0xECEBE7, 0x131312)
    static let paper2 = dyn(0xE2E1DC, 0x1D1D1B)
    static let ink = dyn(0x141414, 0xEDECE7)
    static let ink2 = dyn(0x46453F, 0xBDBBB4)
    static let ink3 = dyn(0x686660, 0x8F8D86)         // 亮色下不放在 paper2 上（4.38:1，不够 4.5）；paper2 上用 ink2
    static let wire = dyn(0x9A9891, 0x56554F)
    static let rule = dyn(0x141414, 0xEDECE7, alpha: 0.14, darkAlpha: 0.13)
    static let red = dyn(0xD5381A, 0xF0502E)          // 唯一强调色：只表示"有信在等你 / 等你审"；亮色下只做底或描边，不做小字
    static let onRed = dyn(0xFFFFFF, 0x131312)
    static let bad = dyn(0xB3261E, 0xFF8A7A)
    static let keyOn = dyn(0x141414, 0xEDECE7)        // 黑色实心块：选中态、重音
    static let onKey = dyn(0xECEBE7, 0x131312)        // 实心块上的字
    static let scrim = dyn(0x141414, 0x000000, alpha: 0.42, darkAlpha: 0.6)

    // MARK: 字号（pt = px）。Instrument Sans 可变字体，宽度轴只在数字上用（B §2.1）
    enum Font {
        private static func f(_ size: CGFloat, _ weight: CGFloat = 400, _ width: CGFloat = 100) -> SwiftUI.Font {
            Fonts.instrument(size, weight: weight, width: width)
        }
        static let viewTitle = f(68, 700)
        static let sectionNum = f(76, 700, 75)
        static let blockTitle = f(22, 700)
        static let sectionTitle = f(28, 700)
        static let small = f(13, 500)
        static let tab = f(15, 600)
        static let rowTitle = f(17, 650)
        static let body = f(15)
        static let bodyBold = f(15, 700)
        static let button = f(15, 650)
        static let stat = f(34, 700, 85).monospacedDigit()
        static let ctaNum = f(40, 700, 85).monospacedDigit()
        static let brand = f(19, 700)
        static let f14 = f(14)
        static let f14Bold = f(14, 700)
        static let h3 = f(26, 700)
        static let h3Small = f(20, 700)
        static let head = f(16, 700)
        static let count = f(13, 700).monospacedDigit()
        static let sub = f(11.5, 500)
        static let badge = f(12.5, 600)
        static let mark = SwiftUI.Font.system(size: 11, weight: .bold, design: .monospaced)
        static let mono = SwiftUI.Font.system(size: 12.5, design: .monospaced)
    }

    /// 字距（pt，= 源稿 em 值 × 字号）
    enum Track {
        static let viewTitle: CGFloat = -2.38
        static let sectionNum: CGFloat = -3.04
        static let blockTitle: CGFloat = -0.44
        static let stat: CGFloat = -0.68
        static let brand: CGFloat = -0.19
        static let badge: CGFloat = 0.24
    }

    // MARK: 间距（B §3：App 里只出现这些数）
    static let pad: CGFloat = 32                      // page：页面左右留白
    static let maxContent: CGFloat = 1480
    static let barHeight: CGFloat = 64
    static let control: CGFloat = 44
    static let drawerWidth: CGFloat = 440
    static let drawerPadLeading: CGFloat = 24
    static let drawerPad: CGFloat = 16
    static let blockTop: CGFloat = 36
    static let blockBottom: CGFloat = 72
    static let statGap: CGFloat = 32
    static let marker: CGFloat = 11                   // 状态灯 / 状态方块

    // MARK: 线（1px = 1pt；圆角 0、无阴影）
    enum Line {
        static let hair: CGFloat = 1                  // 分隔、描边徽标
        static let outline: CGFloat = 1.5             // 锁定 / 空心描边、连线
        static let strong: CGFloat = 2.25             // 强关系连线
    }

    private static func dyn(_ light: UInt32, _ dark: UInt32, alpha: CGFloat = 1, darkAlpha: CGFloat? = nil) -> Color {
        Color(nsColor: NSColor(name: nil) { appearance in
            let isDark = appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
            let v = isDark ? dark : light
            return NSColor(red: CGFloat((v >> 16) & 0xFF) / 255, green: CGFloat((v >> 8) & 0xFF) / 255,
                           blue: CGFloat(v & 0xFF) / 255, alpha: isDark ? (darkAlpha ?? alpha) : alpha)
        })
    }
}

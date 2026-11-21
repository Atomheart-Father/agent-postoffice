import SwiftUI
import AppKit
import CoreText

/// Instrument Sans（可变，wdth 75–100 / wght 400–700，只有拉丁）+ 中文回退 PingFang SC。
/// 字体文件来自设计稿的 instrument.woff2（解压成 TTF，见 Resources/Fonts/README.md），打进 .app 的 Contents/Resources/Fonts。
/// 没找到文件就退回系统字体，界面照样能用。
enum Fonts {
    private static let wght = NSNumber(value: 2003265652)   // 'wght'
    private static let wdth = NSNumber(value: 2003072104)   // 'wdth'
    nonisolated(unsafe) private static var registered: Bool = {
        let name = "InstrumentSans-Variable.ttf"
        let candidates = [
            Bundle.main.resourceURL?.appendingPathComponent("Fonts/\(name)"),
            // swift run / 测试：从源码目录找
            URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
                .deletingLastPathComponent().appendingPathComponent("Resources/Fonts/\(name)"),
        ].compactMap { $0 }
        for url in candidates where FileManager.default.fileExists(atPath: url.path) {
            return CTFontManagerRegisterFontsForURL(url as CFURL, .process, nil)
        }
        return false
    }()

    static func ensureRegistered() { _ = registered }

    /// size/weight(400–700，可以是 650)/width(75–100)
    static func instrument(_ size: CGFloat, weight: CGFloat = 400, width: CGFloat = 100) -> SwiftUI.Font {
        guard registered else { return .system(size: size, weight: system(weight)) }
        let d = NSFontDescriptor(fontAttributes: [
            .family: "Instrument Sans",
            .variation: [wght: weight, wdth: width],
            .cascadeList: [NSFontDescriptor(fontAttributes: [.family: "PingFang SC", .face: cjkFace(weight)])],
        ])
        guard let f = NSFont(descriptor: d, size: size) else { return .system(size: size, weight: system(weight)) }
        return SwiftUI.Font(f as CTFont)
    }

    /// 中文走 PingFang SC，字重只有它自己的档位（和浏览器里一致）：<500 Regular、500 Medium、≥600 Semibold。
    private static func cjkFace(_ w: CGFloat) -> String { w >= 600 ? "Semibold" : w >= 500 ? "Medium" : "Regular" }

    private static func system(_ w: CGFloat) -> SwiftUI.Font.Weight {
        w >= 700 ? .bold : w >= 600 ? .semibold : w >= 500 ? .medium : .regular
    }
}

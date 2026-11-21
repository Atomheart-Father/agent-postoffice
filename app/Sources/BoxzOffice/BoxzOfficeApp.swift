import SwiftUI
import OfficeClient

@main
struct BoxzOfficeApp: App {
    @StateObject private var desk: DeskModel
    private let launch = LaunchScript()

    init() {
        Fonts.ensureRegistered()
        let l = LaunchScript()
        let source = l.fixtures.map { ContractSource(dir: $0) } ?? ContractSource(socket: l.socket)
        _desk = StateObject(wrappedValue: DeskModel(socket: l.socket, contract: source, alwaysForeground: l.capturing))
    }

    var body: some Scene {
        WindowGroup("BOXZ OFFICE") {
            Shell(desk: desk)
                .frame(minWidth: 900, minHeight: 560)
                .task { await desk.run(afterFirstRound: launch.afterFirstRound) }
        }
        .windowStyle(.hiddenTitleBar)
        .defaultSize(width: 1440, height: 900)
    }
}

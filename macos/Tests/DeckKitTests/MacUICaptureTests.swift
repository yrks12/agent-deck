import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// Off-screen captures of every Mac-bridge view, drawn from fixtures, written to
/// `UITests/Artifacts/overhaul/mac-*.png` and asserted to have drawn the words
/// they exist to say (text read back out of the pixels, so a blank or
/// truncated render fails instead of leaving a stale picture behind).
@MainActor
final class MacUICaptureTests: XCTestCase {
    private let now = Date(timeIntervalSince1970: 1_788_222_702)

    private func host<V: View>(_ view: V, size: CGSize, dark: Bool) -> (NSHostingView<AnyView>, NSWindow) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let scheme: ColorScheme = dark ? .dark : .light
        let host = NSHostingView(rootView: AnyView(
            view.background(Color(nsColor: .windowBackgroundColor))
                .environment(\.colorScheme, scheme)
                .environment(\.controlActiveState, .key)))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: size.width, height: size.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: dark ? .darkAqua : .aqua)
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        return (host, window)
    }

    private func capture<V: View>(_ name: String, _ view: V, size: CGSize, dark: Bool = false,
                                  expecting words: [String]) async throws {
        let (host, window) = host(view.frame(width: size.width, height: size.height, alignment: .top),
                                  size: size, dark: dark)
        // Tear the tree down, not just the window: a TimelineView or an avatar
        // left alive in a closed window keeps ticking through later suites.
        defer { host.rootView = AnyView(EmptyView()); window.contentView = nil; window.close() }
        try await Task.sleep(nanoseconds: 700_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        let read = RightPaneFixture.readText(rep).map(\.text).joined(separator: " ").lowercased()
        for word in words {
            XCTAssertTrue(read.contains(word.lowercased()), "\(name) did not draw '\(word)'. Read: \(read.prefix(400))")
        }
        try RightPaneFixture.saveCapture(rep, named: name)
    }

    // MARK: fixtures

    private var running: MacBridgeState {
        var s = MacBridgeState(mode: .ask, connection: .online)
        s.started(MacRunningJob(id: "mj_1", desk: "atlas", summary: "npm test", startedAt: now.addingTimeInterval(-14)))
        return s
    }

    private func config(_ mode: MacMode = .ask) -> MacPolicyConfig {
        var c = MacPolicyConfig(mode: mode, folders: [NSHomeDirectory() + "/Agent Deck Workspace", NSHomeDirectory() + "/Projects/acme"],
                                exceptions: ["~/Projects/acme/.env"], screenshotEnabled: false)
        c.grants = [
            "atlas": MacGrant(state: .hour, until: now.timeIntervalSince1970 + 32 * 60),
            "nova": MacGrant(state: .always, until: nil),
            "scout": MacGrant(state: .denied, until: now.timeIntervalSince1970 + 5 * 3600),
        ]
        return c
    }

    struct SettingsHarness: View {
        @State var config: MacPolicyConfig
        @State var name = "Sam's MacBook Pro"
        @State var menu = true
        @State var login = false
        let state: MacBridgeState
        let now: Date
        var body: some View {
            MacAccessSettingsView(config: $config, macName: $name, keepInMenuBar: $menu, startAtLogin: $login,
                                  state: state, deckHost: "deck.initech.example", now: now)
        }
    }

    // MARK: captures

    /// The Settings page is the one capture that is opt-in. Measured: with its
    /// two tall controls-and-text-fields windows in the run, the full suite
    /// fails `RealtimeCallTests.testADroppedLineEndsTheCallAndSaysSo` (a 2 s
    /// wait) three runs out of three, and with them skipped it passes three out
    /// of three. The views have no timers; the cost is AppKit's, left behind by
    /// the segmented picker / text fields / switches. The pictures are
    /// committed; regenerate with `YOS_MAC_SETTINGS_CAPTURE=1 swift test
    /// --filter MacUICaptureTests`.
    private func requireSettingsCaptureOptIn() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["YOS_MAC_SETTINGS_CAPTURE"] == "1",
                          "set YOS_MAC_SETTINGS_CAPTURE=1 to redraw the Settings captures")
    }

    func testSettingsAskMe() async throws {
        try requireSettingsCaptureOptIn()
        let v = SettingsHarness(config: config(), state: MacBridgeState(mode: .ask, connection: .online), now: now)
        try await capture("mac-settings-ask-light.png", v, size: CGSize(width: 620, height: 1150),
                          expecting: ["Let agents use this Mac", "Never touch", "Desks with access", "32 min left"])
        try await capture("mac-settings-ask-dark.png", v, size: CGSize(width: 620, height: 1150), dark: true,
                          expecting: ["Ask me", "Revoke", "Reset to defaults"])
    }

    func testSettingsFullAccessAndTheGuard() async throws {
        try requireSettingsCaptureOptIn()
        let full = SettingsHarness(config: config(.full), state: MacBridgeState(mode: .full, connection: .online), now: now)
        try await capture("mac-settings-full.png", full, size: CGSize(width: 620, height: 1100),
                          expecting: ["Full access", "Grant Full Disk Access"])
        var guarded = config(.off)
        guarded.grants = [:]
        let refused = SettingsHarness(config: guarded,
                                      state: MacBridgeState(mode: .off, connection: .refused(MacTransport.refusal)), now: now)
        try await capture("mac-settings-guard.png", refused, size: CGSize(width: 620, height: 420),
                          expecting: ["plain HTTP", "WireGuard"])
    }

    func testFullAccessConfirmationSheet() async throws {
        let v = SettingsHarness(config: config(), state: MacBridgeState(), now: now).fullAccessSheetForCapture
        try await capture("mac-full-access-sheet.png", v, size: CGSize(width: 440, height: 300),
                          expecting: ["Turn on full access", "without asking", "Cancel"])
    }

    func testGrantCard() async throws {
        let request = MacGrantRequest(jobId: "mj_1", desk: "atlas", summary: "sw_vers",
                                      folders: ["~/Agent Deck Workspace"], unconfined: false)
        try await capture("mac-grant-card-light.png", MacGrantCardView(request: request, onDecide: { _ in }).padding(24),
                          size: CGSize(width: 520, height: 400), expecting: ["Atlas wants to use this Mac", "Allow for 1 hour", "Deny"])
        let first = MacGrantRequest(jobId: "mj_2", desk: "nova", summary: "read ~/Projects/acme/README.md",
                                    folders: [], unconfined: true)
        try await capture("mac-grant-card-first-dark.png", MacGrantCardView(request: first, onDecide: { _ in }).padding(24),
                          size: CGSize(width: 520, height: 520), dark: true,
                          expecting: ["Nova wants to use this Mac", "Choose folder", "Always allow this desk"])
    }

    func testInUseBar() async throws {
        try await capture("mac-in-use-bar.png", MacInUseBar(state: running, now: now, onStop: { _ in }),
                          size: CGSize(width: 640, height: 60), expecting: ["Atlas is using your Mac", "npm test", "Stop"])
    }

    func testMenuBarMenu() async throws {
        let v = VStack(alignment: .leading, spacing: 6) {
            MacMenuBarContent(state: running, actions: MacMenuBarActions())
        }.padding(14).frame(width: 300, alignment: .leading)
        try await capture("mac-menu-bar-menu.png", v, size: CGSize(width: 300, height: 230),
                          expecting: ["Pause Mac access", "Open Activity", "Quit Shaliach"])
    }

    func testActivity() async throws {
        func row(_ offset: Double, _ desk: String, _ kind: String, _ summary: String, _ d: MacActivityRow.Decision,
                 reason: String? = nil, exit: Int32? = nil, ms: Int? = nil, fenced: Bool? = nil) -> MacActivityRow {
            MacActivityRow(ts: now.timeIntervalSince1970 - offset, jobId: "j\(Int(offset))", desk: desk, kind: kind,
                           summary: summary, decision: d, reason: reason, exit: exit, durationMs: ms, sandboxed: fenced)
        }
        let rows = [
            row(14, "atlas", "run", "npm test", .ran),
            row(120, "nova", "read", "~/Projects/acme/README.md", .ran, exit: 0, ms: 38),
            row(300, "atlas", "run", "sw_vers", .ran, exit: 0, ms: 2100, fenced: true),
            row(330, "atlas", "run", "sw_vers", .granted),
            row(340, "atlas", "run", "sw_vers", .asked),
            row(900, "scout", "read", "~/.ssh/id_ed25519", .refused, reason: "never_touch"),
            row(1500, "nova", "run", "make build", .ran, exit: 2, ms: 125_000, fenced: true),
        ]
        let view = MacActivityView(rows: rows, runningIds: ["j14"], now: now,
                                   output: { $0 == "j300" ? "ProductName:\t\tmacOS\nProductVersion:\t\t26.0\n" : nil })
        try await capture("mac-activity.png", view, size: CGSize(width: 720, height: 480),
                          expecting: ["Activity", "Running", "npm test", "Refused"])
    }
}

extension MacUICaptureTests.SettingsHarness {
    /// The confirmation sheet, which is only ever built while it is presented.
    var fullAccessSheetForCapture: some View {
        MacAccessSettingsView(config: .constant(config), macName: .constant(name), keepInMenuBar: .constant(menu),
                              startAtLogin: .constant(login), state: state, deckHost: "deck.initech.example", now: now)
            .fullAccessSheet
    }
}

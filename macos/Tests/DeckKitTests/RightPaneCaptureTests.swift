import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// D-1 captures for the right pane: the same 1000x962 window height as the
/// reference shots, the pane at its ideal 300pt, drawn from fixtures. Written
/// to `UITests/Artifacts/overhaul/d3-*.png` on every run, and asserted
/// non-blank so a broken renderer cannot leave stale pictures behind.
@MainActor
final class RightPaneCaptureTests: XCTestCase {

    private func capture(
        _ name: String, pane: SettingsPanelBody, width: CGFloat = 300, settle: TimeInterval = 1.2
    ) async throws {
        let size = CGSize(width: width, height: 962)
        let (host, window) = RightPaneFixture.host(pane.frame(width: width, height: 962), size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: UInt64(settle * 1_000_000_000))
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        XCTAssertGreaterThan(RightPaneFixture.readText(rep).count, 2, "\(name) drew almost nothing")
        try RightPaneFixture.saveCapture(rep, named: name)
    }

    func testCaptureAttentionScreenAndRoutines() async throws {
        try await capture("d3-right-pane-attention.png", pane: RightPaneFixture.pane(
            attention: [try RightPaneFixture.handoff()],
            routines: try RightPaneFixture.routines(),
            screens: RightPaneFixture.StubScreens()))
    }

    func testCaptureQuietPane() async throws {
        try await capture("d3-right-pane-quiet.png", pane: RightPaneFixture.pane(
            routines: try RightPaneFixture.routines(),
            screens: RightPaneFixture.StubScreens()))
    }

    func testCaptureThumbnailHover() async throws {
        let shot = AgentScreenFrame(jpeg: RightPaneFixture.frameJPEG(), serverAge: 0.2,
                                    display: ":99", receivedAt: Date())
        let view = VStack(alignment: .leading, spacing: 16) {
            AgentScreenPanelBody(
                presentation: AgentScreenPresentation(desk: "Cos", state: .live(shot)),
                isHovering: true, workspace: nil, onOpen: {})
        }.padding(12).frame(width: 300, height: 260, alignment: .top)
        let (host, window) = RightPaneFixture.host(view, size: CGSize(width: 300, height: 260))
        defer { window.close() }
        try RightPaneFixture.saveCapture(
            try XCTUnwrap(RightPaneFixture.render(host)), named: "d3-screen-thumb-hover.png")
    }
}

extension RightPaneCaptureTests {
    /// The gear's popover, drawn on its own: it is only ever built while open.
    func testCaptureTheProfileBehindTheGear() async throws {
        let form = ProfileSettingsForm(
            agent: RightPaneFixture.agent(), settingsError: nil,
            delivery: .current, store: RightPaneFixture.store())
        let (host, window) = RightPaneFixture.host(form, size: CGSize(width: 340, height: 480))
        defer { window.close() }
        try await Task.sleep(nanoseconds: 400_000_000)
        try RightPaneFixture.saveCapture(
            try XCTUnwrap(RightPaneFixture.render(host)), named: "d3-profile-behind-gear.png")
    }

    /// A permission ask has three answers, so it is a column and not a row.
    func testCaptureAPermissionAsk() async throws {
        let approvals = try DeckCoding.decoder.decode(ApprovalsPage.self, from: Data("""
        {"approvals":[{"id":"apr_1","ts":1756000000.0,"agent":"cos","asked_by":"cos",
          "desk_known":true,"tool":"Bash","subject":"gh pr merge 15",
          "cwd":"/Users/y/Projects/acme","cwd_short":"~/Projects/acme","status":"pending","options":[
           {"reply":"once","available":true,"rule":null,"summary":"Allow once - just this time"},
           {"reply":"always","available":true,"summary":"Always allow - `gh pr*` in ~/Projects/acme, never ask again",
            "rule":{"id":"r","kind":"always_allow","tool":"Bash","pattern":"gh pr*","cwd":"/Users/y/Projects/acme","note":"n"}},
           {"reply":"never","available":true,"summary":"Never - refuse `gh pr*` in ~/Projects/acme from now on",
            "rule":{"id":"r","kind":"deny","tool":"Bash","pattern":"gh pr*","cwd":"/Users/y/Projects/acme","note":"n"}}]}]}
        """.utf8))
        let item = AttentionItem.make(approval: try XCTUnwrap(approvals.approvals.first),
                                      agents: ["cos": RightPaneFixture.agent()])
        try await capture("d3-right-pane-permission.png", pane: RightPaneFixture.pane(
            attention: [item], routines: try RightPaneFixture.routines(),
            screens: RightPaneFixture.StubScreens()))
    }
}

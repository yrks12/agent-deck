import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"This looks off" — the owner's screenshot of the Mac take-over.**
///
/// The title read "creen" (cut off on the left), the terminal column ran off
/// the right edge, the agent's screen sat small inside a grey slab, and a
/// "type a line" field with Return/Tab/Escape buttons made it read as a debug
/// tool. The phone's remote control is the model: the screen fills the window
/// on black, the controls float over it, the terminal is a drawer he opens.
///
/// Measured here at the two sizes he meets — a 900x600 sheet (the smallest
/// window) and 1440x900 — off the rendered pixels and the size the view asks
/// for, both of which are looked at by a person too
/// (`UITests/Artifacts/takeover/`).
@MainActor
final class TheTakeOverLooksLikeThePhoneTests: XCTestCase {

    // MARK: the layout math

    /// The terminal drawer never takes the picture's room: it is at most 42%
    /// of the stage and never wider than the old fixed column.
    func testTheTerminalDrawerLeavesThePictureMostOfTheStage() {
        for width in [640.0, 900, 1120, 1440] {
            let drawer = Takeover.Stage.drawerWidth(stageWidth: width)
            XCTAssertLessThanOrEqual(drawer, width * 0.42 + 0.5, "at \(width)")
            XCTAssertLessThanOrEqual(drawer, Takeover.Stage.terminalWidth)
            XCTAssertGreaterThanOrEqual(width - drawer, width * 0.58 - 0.5)
        }
        XCTAssertGreaterThanOrEqual(Takeover.Stage.drawerWidth(stageWidth: 1440), 300,
                                    "on a big window the terminal is still a usable width")
    }

    /// The stage's floor fits a 900x600 sheet with room to spare — the old
    /// 860pt floor plus a 340pt terminal is what clipped both edges.
    func testTheStagesFloorFitsTheSmallestSheet() {
        XCTAssertLessThanOrEqual(Takeover.Stage.minWidth, 700)
        XCTAssertLessThanOrEqual(Takeover.Stage.minHeight, 460)
    }

    // MARK: the rendered sheet

    private func stage(terminal: Bool) -> TakeoverStageView {
        TakeoverStageView(
            request: TakeoverRequest(desk: "atlas", displayName: "Atlas",
                                     workspace: terminal ? "/home/agent/work" : "",
                                     focus: terminal ? .terminal : .screen),
            screens: RightPaneFixture.StubScreens(),
            shells: terminal ? FakeShellClient() : nil,
            onClose: {})
    }

    private func render(_ view: TakeoverStageView, _ size: CGSize, name: String) async throws
        -> (rep: NSBitmapImageRep, asked: CGSize) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let asked = floor(view)
        let host = NSHostingView(rootView: AnyView(view.frame(width: size.width, height: size.height)
            .environment(\.colorScheme, .dark)))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: size.width, height: size.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host
        window.orderBack(nil)
        defer { host.rootView = AnyView(EmptyView()); window.contentView = nil; window.close() }
        try await Task.sleep(nanoseconds: 1_200_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        let dir = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("UITests/Artifacts/takeover")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try RightPaneFixture.png(rep)?.write(to: dir.appendingPathComponent(name))
        return (rep, asked)
    }

    /// How much of the sheet the agent's screen is drawn across. The stub
    /// frame (`RightPaneFixture.frameJPEG`) is 640x400 with an 80pt blue disc
    /// in the middle, so the drawn picture is eight discs wide and five tall —
    /// measured off the disc, which nothing else on the sheet is coloured.
    private func pictureShare(_ rep: NSBitmapImageRep) -> (width: Double, height: Double) {
        var minX = Int.max, maxX = -1, minY = Int.max, maxY = -1
        for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
            for x in stride(from: 0, to: rep.pixelsWide, by: 2) {
                guard let c = rep.colorAt(x: x, y: y)?.usingColorSpace(.deviceRGB),
                      c.blueComponent > 0.75, c.redComponent < 0.45, c.greenComponent < 0.75 else { continue }
                minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
            }
        }
        guard maxX >= 0 else { return (0, 0) }
        let disc = Double(maxX - minX)
        return (disc * 8 / Double(rep.pixelsWide), disc * 5 / Double(rep.pixelsHigh))
    }

    /// The least room the view will lay out in. More than the sheet is what
    /// AppKit centres and clips at both edges — "creen".
    private func floor(_ view: TakeoverStageView) -> CGSize {
        NSHostingController(rootView: view).sizeThatFits(in: CGSize(width: 1, height: 1))
    }

    func testAtTheSmallestSheetNothingIsClippedAndTheScreenFillsIt() async throws {
        let size = CGSize(width: 900, height: 600)
        let (rep, asked) = try await render(stage(terminal: false), size, name: "takeover-900x600.png")
        XCTAssertLessThanOrEqual(asked.width, size.width + 0.5, "the take-over asks for more width than the sheet has")
        XCTAssertLessThanOrEqual(asked.height, size.height + 0.5, "the take-over asks for more height than the sheet has")
        let share = pictureShare(rep)
        XCTAssertTrue(share.width >= 0.9 || share.height >= 0.78,
                      "the agent's screen gets \(share) of a 900x600 sheet — a picture in a letterbox")
    }

    func testAtALargeSheetTheScreenFillsItAndTheTerminalIsADrawer() async throws {
        let size = CGSize(width: 1440, height: 900)
        let (rep, asked) = try await render(stage(terminal: false), size, name: "takeover-1440x900.png")
        XCTAssertLessThanOrEqual(asked.width, size.width + 0.5)
        let share = pictureShare(rep)
        XCTAssertTrue(share.width >= 0.9 || share.height >= 0.8,
                      "the agent's screen gets \(share) of a 1440x900 sheet")

        let (withDrawer, askedWithDrawer) = try await render(stage(terminal: true), CGSize(width: 900, height: 600),
                                                             name: "takeover-900x600-terminal.png")
        XCTAssertLessThanOrEqual(askedWithDrawer.width, 900.5, "the terminal pushes the sheet past its window")
        XCTAssertGreaterThan(pictureShare(withDrawer).width, 0.5, "the drawer took the picture's room")
    }

    /// Hidden until he asks: a take-over opened on the screen shows no terminal.
    func testTheTerminalIsHiddenWhenHeOpenedTheScreen() {
        let request = TakeoverRequest(desk: "atlas", displayName: "Atlas", workspace: "/home/agent/work",
                                      focus: .screen)
        XCTAssertFalse(request.showsTerminal)
    }
}

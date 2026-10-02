import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The right pane is what he looks at to see what is stuck on him - not a
/// profile form.**
///
/// The reference product's pane is a stack: an orange "Needs your attention"
/// card with two buttons, the desk's live screen with an Open on hover, a
/// Routines list with a "+", and the profile behind a gear. Ours opened on
/// Name / Title / Description / Save changes.
///
/// These read the pixels the pane really draws (Vision text recognition over a
/// rendered bitmap), because SwiftUI text has no NSView for a test to find and
/// a source grep would pass over a pane that is drawn wrong.
@MainActor
final class RightPaneTests: XCTestCase {

    private let paneSize = CGSize(width: 300, height: 900)

    private func words(
        _ pane: SettingsPanelBody, settle: TimeInterval = 0.3
    ) async throws -> [(text: String, box: CGRect)] {
        let (host, window) = RightPaneFixture.host(
            pane.frame(width: paneSize.width, height: paneSize.height), size: paneSize)
        defer { window.close() }
        try await Task.sleep(nanoseconds: UInt64(settle * 1_000_000_000))
        return RightPaneFixture.readText(try XCTUnwrap(RightPaneFixture.render(host)))
    }

    private func find(_ text: String, in found: [(text: String, box: CGRect)]) -> CGRect? {
        // The topmost match: a button's label, not the footnote repeating it.
        found.filter { $0.text.localizedCaseInsensitiveContains(text) }
            .min { $0.box.minY < $1.box.minY }?.box
    }

    private func fullPane() throws -> SettingsPanelBody {
        RightPaneFixture.pane(
            attention: [try RightPaneFixture.handoff()],
            routines: try RightPaneFixture.routines(),
            screens: RightPaneFixture.StubScreens())
    }

    // MARK: 1 - the profile form is not the pane

    func testTheProfileFormIsNotWhatThePaneOpensOn() async throws {
        let seen = try await words(try fullPane())
        for form in ["Save changes", "Description", "Title"] {
            XCTAssertNil(
                find(form, in: seen),
                "\"\(form)\" is drawn on the right pane. That is the profile form - "
                + "it belongs behind the gear, and the pane is for what is waiting on him.")
        }
    }

    // MARK: 2 - attention, screen, routines are all on screen at once

    func testAttentionScreenAndRoutinesAreAllInViewTogether() async throws {
        let seen = try await words(try fullPane())
        let attention = try XCTUnwrap(find("Needs your attention", in: seen), "no attention card")
        let screen = try XCTUnwrap(find("screen", in: seen), "no screen caption")
        let routines = try XCTUnwrap(find("Routines", in: seen),
                                     "Routines is not on screen in a 900pt pane")
        XCTAssertLessThan(attention.minY, screen.minY, "the card comes first")
        XCTAssertLessThan(screen.minY, routines.minY, "routines come under the screen")
        XCTAssertNotNil(find("Weekdays at 8:00 AM", in: seen), "a routine's schedule is not listed")
        XCTAssertNotNil(find("Weekdays at 9:00 AM", in: seen), "the second routine is not listed")
    }

    // MARK: 3 - two buttons, side by side, the quiet one first

    func testTheCardOffersTwoButtonsInOneRowWithSkipFirst() async throws {
        let seen = try await words(try fullPane())
        let skip = try XCTUnwrap(find("Skip this step", in: seen), "no skip button")
        let carry = try XCTUnwrap(find("done, continue", in: seen), "no continue button")
        XCTAssertEqual(
            skip.midY, carry.midY, accuracy: 12,
            "the two answers are stacked, not a row: skip at y=\(Int(skip.midY)), "
            + "continue at y=\(Int(carry.midY))")
        XCTAssertLessThan(skip.minX, carry.minX, "the quiet answer goes first, the ordinary one last")
    }

    // MARK: 4 - the picture's own Open, on hover

    private func screenWords(hovering: Bool) async throws -> [(text: String, box: CGRect)] {
        let shot = AgentScreenFrame(jpeg: RightPaneFixture.frameJPEG(), serverAge: 0.2,
                                    display: ":99", receivedAt: Date())
        let panel = AgentScreenPanelBody(
            presentation: AgentScreenPresentation(desk: "Cos", state: .live(shot)),
            isHovering: hovering, workspace: nil, onOpen: {})
            .equatable().padding(12)
        let (host, window) = RightPaneFixture.host(panel, size: CGSize(width: 300, height: 260))
        defer { window.close() }
        return RightPaneFixture.readText(try XCTUnwrap(RightPaneFixture.render(host)))
    }

    func testHoveringThePictureDrawsOpenOnIt() async throws {
        let cold = try await screenWords(hovering: false)
        let warm = try await screenWords(hovering: true)
        XCTAssertNil(cold.first { $0.text == "Open" }, "Open is drawn with nobody over the picture")
        XCTAssertNotNil(warm.first { $0.text.hasSuffix("Open") },
                        "the pointer is on the picture and it does not say Open")
    }

    // MARK: 5 - gear and collapse exist, and say what they do

    func testTheGearAndTheCollapseAreNamedControls() throws {
        let panel = try Self.source("Sources/DeckUI/SettingsPanelView.swift")
        XCTAssertTrue(panel.contains("Profile and settings"),
                      "no gear: the profile has nowhere to live once it leaves the pane")
        XCTAssertTrue(panel.contains("Hide the side panel"), "the pane cannot be collapsed from itself")
        let root = try Self.source("Sources/DeckUI/DeckRootView.swift")
        XCTAssertTrue(root.contains("collapse:"),
                      "the root does not hand the pane a way to close the inspector")
    }

    private static func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }
}

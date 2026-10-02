import XCTest
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The app said "The deck rejected this token" and he could not see it.**
///
/// He lost most of a night, and I lost most of a night, to a sidebar that drew
/// nothing. Root cause, MEASURED: the deck's token had diverged from the one in
/// his Keychain, every request answered `401`, and `roster` sat in `.failed`.
/// The app was **right** — it had the correct diagnosis on screen the whole
/// time. He only ever saw it after resizing the window smaller.
///
/// So the defect worth pinning is not the token. It is that **a failure state
/// is only useful at the size he actually runs the app at.** I read stack
/// traces, split-view autosave and layout counters for hours while the answer
/// was rendered in plain English in a window I never measured.
///
/// This asserts the good signal at both extremes: whatever the sidebar is
/// saying, it is REALISED — a real view with a real size — from the narrowest
/// column the split view permits up to a full-height display. A message that
/// exists but never lands on screen is the same nothing as no message at all,
/// and this repo has now shipped that twice.
///
/// Calibration is the whole trick, and it is asserted first: a macOS view tree
/// only realises what it is about to show, so `.hidden()` collapses the same
/// content to zero. If the probe cannot tell those apart it is measuring
/// nothing, and every assertion under it would be a mirror.
@MainActor
final class ARejectedTokenIsUnmissableTests: XCTestCase {

    /// The sizes that matter: the smallest column `Theme.inspectorWidth` and
    /// the split view allow, the width his window actually had, and a tall
    /// display where a centred message can fall outside a short clip.
    private static let columnWidths: [CGFloat] = [200, 260, 308, 420]
    private static let windowHeights: [CGFloat] = [560, 949, 1990]

    private func realisedSubviews<V: View>(
        _ view: V, width: CGFloat, height: CGFloat
    ) -> Int {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view.frame(width: width, height: height))
        host.frame = NSRect(x: 0, y: 0, width: width, height: height)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        _ = host.fittingSize
        let count = Self.descendants(of: host)
        window.orderOut(nil)
        window.contentView = nil
        return count
    }

    private static func descendants(of view: NSView) -> Int {
        view.subviews.reduce(view.subviews.count) { $0 + descendants(of: $1) }
    }

    /// **Calibration, first.** The probe must be able to tell a drawn message
    /// from a hidden one, or nothing below it means anything.
    func testTheProbeCanTellADrawnMessageFromAHiddenOne() {
        let shown = realisedSubviews(
            FailureView(failure: FailurePresentation.make(.unauthorized), onRetry: {}),
            width: 308, height: 949)
        let hidden = realisedSubviews(
            FailureView(failure: FailurePresentation.make(.unauthorized), onRetry: {})
                .hidden(),
            width: 308, height: 949)

        XCTAssertGreaterThan(
            shown, hidden,
            "the probe counted \(shown) realised views for a message that is on "
            + "screen and \(hidden) for the same message hidden — it cannot see "
            + "the difference, so every assertion below would pass on a blank "
            + "sidebar")
    }

    /// THE test. At every width and height, the rejected-token message is a
    /// real view on screen.
    func testTheRejectedTokenMessageIsRealisedAtEveryWindowSizeHeUses() {
        var blank: [String] = []
        for width in Self.columnWidths {
            for height in Self.windowHeights {
                let realised = realisedSubviews(
                    FailureView(failure: FailurePresentation.make(.unauthorized),
                                onRetry: {}),
                    width: width, height: height)
                if realised == 0 { blank.append("\(Int(width))x\(Int(height))") }
            }
        }
        XCTAssertEqual(
            blank, [],
            "at these window sizes the deck-rejected-this-token message drew "
            + "nothing at all, so the app looks broken instead of telling him "
            + "his token is wrong: \(blank.joined(separator: ", "))")
    }

    /// And it must SAY the cause. A generic "can't reach the deck" for a 401
    /// sends him to check the network and the box, neither of which is wrong —
    /// which is exactly the hour I spent on stack traces.
    func testItNamesTheTokenRatherThanBlamingTheConnection() {
        let presented = FailurePresentation.make(.unauthorized)
        let words = (presented.heading + " " + presented.detail).lowercased()
        XCTAssertTrue(
            words.contains("token"),
            "a 401 is presented as \(presented.heading.debugDescription) / "
            + "\(presented.detail.debugDescription), which does not mention the "
            + "token — so the one thing he can fix is the one thing it does not "
            + "name")
    }

    /// **What this probe can and cannot see, measured, so nobody trusts it
    /// further than it goes.**
    ///
    /// It counts AppKit-backed descendants. The failure screens realise **2**
    /// at 900x900 because they carry a button; a text-only
    /// `ContentUnavailableView(title:systemImage:description:)` realises **0**
    /// at the same size. That zero is the probe, not the product — SwiftUI
    /// draws that text without an AppKit view for the harness to count.
    ///
    /// I nearly shipped the opposite conclusion. The first version of the class
    /// sweep below read that zero as "the empty-roster message is invisible",
    /// and I had already changed `SidebarView` to fix a bug that was not there.
    /// The measurement above is what stopped it, and it is kept as a test so
    /// the next person does not repeat the hour.
    ///
    /// **The admitted gap:** nothing here can prove a *text-only* state is
    /// legible at his window size. That needs an instrument that reads rendered
    /// ink, and `TheRosterIsOnScreenTests` records why the two attempts at one
    /// failed — an `opacity(0)` sidebar still rendered 204 distinct colours, so
    /// backdrop chrome swamps the signal.
    func testThisProbeSeesControlsAndNotText() {
        let withAButton = realisedSubviews(
            FailureView(failure: FailurePresentation.make(.unauthorized), onRetry: {}),
            width: 900, height: 900)
        let textOnly = realisedSubviews(
            ContentUnavailableView(
                "No agents yet", systemImage: "person.2.slash",
                description: Text("This deck has no desks on its roster.")),
            width: 900, height: 900)

        XCTAssertGreaterThan(
            withAButton, 0,
            "the probe sees nothing even for a screen with a button on it, so "
            + "every assertion in this file is worthless")
        XCTAssertEqual(
            textOnly, 0,
            "a text-only ContentUnavailableView now realises \(textOnly) "
            + "AppKit views. If that is real, this probe has grown teeth it did "
            + "not have and the class sweep below should be widened to cover "
            + "text-only states")
    }

    /// The class, as far as this instrument honestly reaches: every failure the
    /// sidebar can show him carries a way out, and that control is on screen at
    /// the sizes he uses. An unreachable deck and a rejected token are the same
    /// silence if neither offers him a button.
    func testEveryFailureTheSidebarCanShowOffersAWayOutOnScreen() {
        var blank: [String] = []
        for (name, error) in [("unauthorized", DeckError.unauthorized),
                              ("transport", .transport("no route")),
                              ("missing token", .missingToken)] {
            for width in Self.columnWidths {
                let realised = realisedSubviews(
                    FailureView(failure: FailurePresentation.make(error), onRetry: {}),
                    width: width, height: 560)
                if realised == 0 { blank.append("\(name) at \(Int(width))pt") }
            }
        }
        XCTAssertEqual(
            blank, [],
            "these failures drew no control at all, so the app looks broken "
            + "instead of telling him what to fix: \(blank.joined(separator: ", "))")
    }
}

import XCTest

/// **He opened the app and could not reach a single agent.**
///
/// His words: *"open it i still have nothing"* — twice, an evening apart.
///
/// MEASURED, from `defaults read <bundle id>` on his Mac, verbatim:
///
///     "NSSplitView Subview Frames … SidebarNavigationSplitView" = (
///         "0.000000, 0.000000, 308.000000, 1990.000000, YES, NO",
///         "0.000000, 0.000000,  900.000000, 1990.000000, NO,  NO"
///     );
///
/// The trailing `YES` on the first subview is AppKit's **collapsed** flag. The
/// roster column was saved collapsed, so every launch restored it collapsed,
/// and a collapsed roster in this product means **no desk is reachable at all**
/// — which is indistinguishable from a dead app. He lost an evening to it and
/// nothing in the product could bring the column back: `DeckRootView` declared
/// `NavigationSplitView { } detail: { }` with **no `columnVisibility` binding**,
/// so AppKit's autosave was the only thing that owned it, the only control was
/// the system's own toggle, and there was no code path that said "show the
/// roster" ever again.
///
/// The app has to own that. Restoring a window's size and divider position is
/// a courtesy; restoring a state in which the product cannot be used is not.
///
/// **Why this test reads source instead of driving a split view, stated
/// plainly.** It was tried first. Hosted headlessly, SwiftUI's split view
/// *refuses* a programmatic collapse: `setPosition(0, ofDividerAt: 0)` reports
/// `isSubviewCollapsed == true` and then re-lays the column out at 308pt with
/// every row intact. So a behavioural test passes identically before and after
/// the fix — a mirror, and this repo has been bitten by those. What can be
/// asserted honestly is that the app **declares ownership** of the column and
/// opens it, which is the whole of the fix. Same shape, and the same reasoning,
/// as `ScreenGateTests` parsing sources rather than launching an app.
///
/// The gap that remains, named rather than left silent: nothing here proves the
/// column is on screen for *him*. That needs a reading from the running app on
/// his display, and `TheRosterIsOnScreenTests` records why no headless probe in
/// this repo can take it.
final class TheRosterColumnComesBackTests: XCTestCase {

    private var source: String {
        get throws {
            let url = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()   // Tests/DeckKitTests
                .deletingLastPathComponent()   // Tests
                .deletingLastPathComponent()   // repo root
                .appendingPathComponent("Sources/DeckUI/DeckRootView.swift")
            return try String(contentsOf: url, encoding: .utf8)
        }
    }

    /// Without this every assertion below is vacuously true.
    func testTheRootViewIsWhereThisCheckLooksForIt() throws {
        XCTAssertTrue(
            try source.contains("NavigationSplitView"),
            "DeckRootView no longer declares the split view this check is about")
    }

    /// THE test. The app must own whether the roster column is visible.
    func testTheAppOwnsWhetherTheRosterColumnIsVisible() throws {
        XCTAssertTrue(
            try source.contains("columnVisibility"),
            "DeckRootView passes no columnVisibility binding, so AppKit's "
            + "autosaved state is the only thing that decides whether the "
            + "roster is on screen — and his was saved collapsed. Nothing in "
            + "the product can bring it back, so the app opens with no desk "
            + "reachable and no way to reach one.")
    }

    /// And it must open **shown**. A binding that merely exists, or one seeded
    /// from whatever was saved last time, satisfies the assertion above and
    /// restores the same unusable window — the shape of a check that ran and
    /// proved nothing.
    func testItOpensWithTheRosterShownRatherThanWhateverWasSavedLastTime() throws {
        let body = try source
        guard let line = body.split(separator: "\n").first(where: {
            $0.contains("columnVisibility") && $0.contains("=")
        }) else {
            return XCTFail(
                "no line declares the columnVisibility state, so nothing sets "
                + "its starting value")
        }
        XCTAssertTrue(
            line.contains(".all"),
            "the roster column's visibility starts at something other than "
            + ".all, so a window saved with it collapsed opens collapsed "
            + "again: \(line.trimmingCharacters(in: .whitespaces))")
        XCTAssertFalse(
            line.contains("SceneStorage") || line.contains("AppStorage"),
            "the starting value is persisted, which is the defect: the state "
            + "that has to be recoverable is being made sticky instead. "
            + "\(line.trimmingCharacters(in: .whitespaces))")
    }

    /// The reason travels with the line, or it reads like a stray `@State` and
    /// is removed in a tidy-up. The symptom returns days later as "the app
    /// opens empty", which nobody connects back to a deleted binding.
    func testTheReasonIsWrittenDownBesideIt() throws {
        let body = try source
        guard let where_ = body.range(of: "columnVisibility") else {
            return XCTFail("covered by the assertion above")
        }
        let before = String(body[body.startIndex..<where_.lowerBound].suffix(900)).lowercased()
        XCTAssertTrue(
            before.contains("collaps"),
            "the binding is there with no comment saying it exists because a "
            + "collapsed roster leaves him unable to reach any agent")
    }
}

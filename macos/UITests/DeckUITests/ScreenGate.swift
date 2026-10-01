import XCTest

/// **The owner's screen is his unless he has said otherwise.**
///
/// XCUITest does not run in a window off to the side. It launches the real
/// `Agent Deck.app`, brings it to the front, and drives it with synthetic
/// clicks and keystrokes for the length of the run — so for those minutes the
/// Mac belongs to the test and not to the person sitting at it. That happened
/// to him mid-work, and his instruction was to run these only when he has said
/// he does not need the screen.
///
/// So: **default deny.** `swift test` never takes the screen — it does not
/// build this target at all. `xcodebuild ... test` without the switch skips
/// every screen-taking case and reports a clean, honest run.
///
/// Turn them on for one run, when the screen really is free:
///
/// ```console
/// $ xcodegen generate --spec macos/App/project.yml
/// $ YOS_SCREEN_IS_FREE=1 xcodebuild -project macos/App/AgentDeck.xcodeproj \
///     -scheme AgentDeck -destination 'platform=macOS' test
/// ```
///
/// **Skip, not fail.** A failure here would make a correct run look broken,
/// and the reliable consequence of that is people running it anyway to get a
/// green — which is how the screen gets taken again. A skip says the truth:
/// this was not run, and why.
///
/// `Tests/DeckKitTests/ScreenGateTests.swift` parses this file and every
/// XCTestCase beside it, so a new UI test added without the gate fails the
/// ordinary `swift test` run rather than being discovered on his screen.
extension XCTestCase {

    /// Call this before launching an `XCUIApplication`. Throws a skip — not a
    /// failure — when the screen has not been declared free.
    func requireTheScreenIsFree(
        file: StaticString = #filePath, line: UInt = #line
    ) throws {
        let free = ProcessInfo.processInfo.environment["YOS_SCREEN_IS_FREE"] == "1"
        try XCTSkipUnless(free, """
            Skipped: this test takes over the screen and YOS_SCREEN_IS_FREE is \
            not set to 1. It launches the real app, brings it to the front and \
            types into it, so it is only run when the owner has said he does \
            not need the Mac. Re-run with YOS_SCREEN_IS_FREE=1 when it is free.
            """, file: file, line: line)
    }
}

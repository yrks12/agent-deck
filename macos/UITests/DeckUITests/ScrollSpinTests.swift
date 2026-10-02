import XCTest

/// **Superseded, and kept only for the scroll path.**
///
/// This was written on the belief that the fault only existed while somebody
/// was scrolling — an idle 100-second watch had read `0.0%` on a process that
/// pegged the moment it was touched. That belief was wrong about the cause of
/// the quiet: the runs here set `DECK_FIXTURE=1`, and the fixture's longest
/// message is a single line, so this scrolls a pane that never spins. The
/// numbers it produced were of the wrong app.
///
/// **The real reproduction needs no scrolling and no UI test.** Against the
/// owner's own deck the app takes a whole core by itself within 15 seconds
/// with nobody touching it, so the measurement is a shell script:
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh http://10.99.0.1:7789
/// ```
///
/// That is what named the driver. Four runs, 40s to settle then three readings
/// six seconds apart: baseline 99.1 / 100.5 / 100.0, and the build with the
/// `Label`s drawn as plain `.top` rows 0.1 / 0.3 / 0.0, held at 0.0-1.8% for
/// 110 seconds with **zero** `FallbackAlignment` / `setFont` frames in the
/// sample where every earlier sample had 54-63. `Label` is gone from
/// `Sources/DeckUI` and `testNoViewLetsSwiftUIResolveAnAlignmentForIt` is what
/// stops one coming back.
///
/// **And the `Label` verdict above did not hold.** The fixed build still pegged
/// at t=45s; the driver was run 3, the window chrome, and it is fixed in
/// `ThreadHeader`. All three `DeckExperiment` switches are deleted — the app
/// needs no flag to be usable now. See `WindowChromeTests`.
///
/// ## THIS HAS NOT BEEN RUN
///
/// He is at that Mac and the screen is his. It is gated like everything else
/// here — see `ScreenGate.swift` — and `ScreenGateTests`, which *does* run
/// under an ordinary `swift test`, checks that this file asks permission
/// before it launches anything.
///
/// ## What it is still good for
///
/// A scroll-driven cost is a different question from an idle one, and this is
/// the only thing in the repo that asks it. To make it worth running, point it
/// at a deck with a real transcript by adding `DECK_URL` to the scheme's
/// environment — a fixture reading proves nothing. **Fixture by default on
/// purpose**: this types and clicks, and it must not be able to answer an
/// approval on a live deck.
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 xcodebuild -project UITests/DeckUITests.xcodeproj \
///     -scheme DeckUITests -destination 'platform=macOS,arch=arm64' \
///     -only-testing:DeckUITests/ScrollSpinTests test
/// ```
///
/// `XCTCPUMetric(application:)` attributes CPU to the **app process**, not to
/// the test runner, so the number in the report is the thing he complained
/// about. A healthy scroll should be a few percent of one core.
final class ScrollSpinTests: XCTestCase {

    override func setUpWithError() throws {
        // Default deny: this launches the real app and takes the screen.
        try requireTheScreenIsFree()
        continueAfterFailure = false
    }

    /// Flip these to isolate a chain. Left empty so run 1 is the honest
    /// baseline — the build he is actually holding.
    private var experiment: [String: String] { [:] }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        for (key, value) in experiment { app.launchEnvironment[key] = value }
        app.launch()
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))
        XCTAssertTrue(
            app.outlines.cells.element(boundBy: 1).waitForExistence(timeout: 30),
            "the roster never drew, so nothing below is scrolling a conversation")
        app.outlines.cells.element(boundBy: 1).click()
        return app
    }

    /// **The measurement.** Scrolls the conversation the way he does and
    /// attributes the CPU to the app.
    func testScrollingTheConversationDoesNotPegACore() throws {
        let app = launch()
        let transcript = app.scrollViews.firstMatch
        XCTAssertTrue(transcript.waitForExistence(timeout: 20),
                      "no scrollable conversation on screen")

        measure(metrics: [XCTCPUMetric(application: app)]) {
            for _ in 0..<12 {
                transcript.scroll(byDeltaX: 0, deltaY: -220)
                transcript.scroll(byDeltaX: 0, deltaY: 220)
            }
        }
    }

    /// The same thing held for a minute rather than sampled in bursts, because
    /// the ramp on the real app is **30-40 seconds** — every reading shorter
    /// than that has so far been a false pass.
    ///
    /// This one asserts nothing. It exists so `top -pid` or `sample` can be
    /// pointed at a process that is definitely being scrolled, for long enough
    /// to be past the ramp.
    func testHoldTheConversationUnderContinuousScrollForNinetySeconds() throws {
        let app = launch()
        let transcript = app.scrollViews.firstMatch
        XCTAssertTrue(transcript.waitForExistence(timeout: 20))

        print("PID \(app.processID) — scrolling for 90s. "
              + "Run: top -pid \(app.processID) -l 20 -stats pid,cpu,state")
        let until = Date().addingTimeInterval(90)
        while Date() < until {
            transcript.scroll(byDeltaX: 0, deltaY: -180)
            transcript.scroll(byDeltaX: 0, deltaY: 180)
        }
        XCTAssertTrue(app.windows.firstMatch.exists,
                      "the app died during the run, so any number taken is of a corpse")
    }
}

import XCTest

/// **What an idle Agent Deck costs.**
///
/// This is the only test in this repo that can see the defect of 2026-09-04.
/// The app sat at **99.3% CPU** with a 2.4 GB footprint doing nothing, because
/// an indeterminate `ProgressView` was left permanently on the thread pane and
/// drove the window's display cycle at the refresh rate.
///
/// `Tests/DeckKitTests/LayoutSettlesTests.swift` guards the *rule* that came
/// out of that — no steady state of the pane may be animated — and it can also
/// see continuous state-driven work. What it cannot see is an AppKit animation,
/// because AppKit suspends animations in a window that is not on screen.
/// Measured there: a timer-driven view reports ~49 layout passes a second, an
/// indeterminate `ProgressView` reports **0**. A real, visible window in a real
/// app process is the only place the cost is real, and that is here.
///
/// **This has not been run.** It is gated on `YOS_SCREEN_IS_FREE=1` like
/// everything else in this directory, and the owner was working on that Mac.
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 xcodebuild -project UITests/DeckUITests.xcodeproj \
///     -scheme DeckUITests -destination 'platform=macOS,arch=arm64' test \
///     -only-testing:DeckUITests/IdleCostTests
/// ```
final class IdleCostTests: XCTestCase {

    override func setUpWithError() throws {
        try requireTheScreenIsFree()
        continueAfterFailure = false
    }

    /// The app's own CPU percentage, straight out of `ps` — the same number the
    /// owner was shown when this was found.
    private func cpuPercent() throws -> Double {
        let pids = try shell("/usr/bin/pgrep", ["-x", "Agent Deck"])
            .split(separator: "\n").compactMap { Int($0) }
        let pid = try XCTUnwrap(pids.last, "no running 'Agent Deck' process to measure")
        let out = try shell("/bin/ps", ["-o", "%cpu=", "-p", String(pid)])
        return try XCTUnwrap(Double(out.trimmingCharacters(in: .whitespacesAndNewlines)),
                             "could not read %cpu out of \(out)")
    }

    private func shell(_ launchPath: String, _ arguments: [String]) throws -> String {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: launchPath)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        try process.run()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        return String(decoding: data, as: UTF8.self)
    }

    /// THE detector. Open the conversation on a desk that reads WORKING — which
    /// is the state his desks are in most of the time, and the state that was
    /// drawn with a spinner — then stop touching it and see what it costs.
    func testTheAppGoesQuietWhenTheOwnerStopsTouchingIt() throws {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        app.launch()

        // GOOD SIGNAL FIRST. A crashed or blank app costs no CPU either, and
        // would sail through the measurement below.
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30),
                      "no window, so the measurement below would be of nothing")
        XCTAssertTrue(
            app.outlines.cells.element(boundBy: 1).waitForExistence(timeout: 30),
            "the fixture roster never drew")
        let strip = app.descendants(matching: .any)
            .matching(NSPredicate(format: "label CONTAINS[c] 'Working'")).firstMatch
        XCTAssertTrue(
            strip.waitForExistence(timeout: 15),
            "the status strip is not on screen, so this is not measuring the "
            + "screen that spun")

        // Let the opening settle: first frames, fonts, the transcript's first
        // measurement. None of that is the defect.
        Thread.sleep(forTimeInterval: 5)
        // `ps` reports a decaying average, so sample after a real idle window.
        Thread.sleep(forTimeInterval: 10)

        let idle = try cpuPercent()
        XCTAssertLessThan(
            idle, 15.0,
            "Agent Deck is burning \(idle)% CPU with nobody touching it. He "
            + "measured 99.3% on the build that shipped and had to kill the app.")
    }

    /// The same, with the inspector closed and a long paste in the composer —
    /// the other two shapes of the screen he actually uses.
    func testItStaysQuietWithALongMessageInTheComposer() throws {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_FIXTURE"] = "1"
        app.launch()
        XCTAssertTrue(app.windows.firstMatch.waitForExistence(timeout: 30))

        let field = app.textFields.matching(
            NSPredicate(format: "label BEGINSWITH 'Message '")).firstMatch
        XCTAssertTrue(field.waitForExistence(timeout: 20), "no composer on a direct thread")
        field.click()
        for line in 1...12 {
            app.typeText("paragraph \(line) of the mandate")
            app.typeKey(.return, modifierFlags: [.shift])
        }

        Thread.sleep(forTimeInterval: 12)

        let idle = try cpuPercent()
        XCTAssertLessThan(
            idle, 15.0,
            "Agent Deck is burning \(idle)% CPU with a long paste sitting in the "
            + "box and nobody touching it")
    }
}

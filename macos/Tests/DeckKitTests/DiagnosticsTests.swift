import XCTest
@testable import DeckKit

/// **A way to find out, instead of another guess.**
///
/// Four rounds of this bug have now been closed by measurement rather than by
/// reading: the spinner, the baseline stacks, `.textSelection`, and the
/// byte-at-a-time transport (512KB in 18ms — it cannot be 99% of a core at one
/// frame per ten seconds). What is left is that the app burns a core **only
/// when a real `HTTPDeckClient` is attached**, with almost no network traffic,
/// and 2544 of 2552 samples inside SwiftUI's graph update. Nothing readable
/// explains it.
///
/// So this counts. `DECK_DIAGNOSE=1` makes the app print, once a second, how
/// many times each thing actually happened. One run against the real box says
/// which of these is true, and they are mutually exclusive:
///
///   - **store publishes are high** — our own state is churning, and the
///     per-site counts name which setter.
///   - **store publishes ~0 but view bodies are high** — SwiftUI/AppKit is
///     driving itself and nothing we publish is involved, which moves the hunt
///     out of `DeckStore` entirely.
///   - **both ~0** — the spin is not in the view layer at all, and the sample's
///     stack is misleading us.
///
/// It must cost nothing and say nothing unless it is asked for: this ships in
/// the same binary the owner runs.
final class DiagnosticsTests: XCTestCase {

    override func tearDown() {
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    /// The whole point of the switch. A diagnostic that costs something when it
    /// is off is a diagnostic you cannot ship in the build he uses.
    func testItCountsNothingUntilItIsAskedTo() {
        Diagnostics.isEnabled = false
        Diagnostics.reset()

        for _ in 0..<1000 { Diagnostics.count("store.publish") }

        XCTAssertTrue(Diagnostics.snapshot().isEmpty,
                      "counting while disabled means the owner's build pays for it")
    }

    func testItCountsWhatHappenedWhenItIsAskedTo() {
        Diagnostics.isEnabled = true
        Diagnostics.reset()

        Diagnostics.count("store.publish")
        Diagnostics.count("store.publish")
        Diagnostics.count("thread.body")

        XCTAssertEqual(Diagnostics.snapshot()["store.publish"], 2)
        XCTAssertEqual(Diagnostics.snapshot()["thread.body"], 1)
    }

    /// The report has to read as *rates*, not as totals — "this happened 40,000
    /// times since launch" does not say whether it is still happening.
    func testTheReportIsADeltaSinceTheLastReport() {
        Diagnostics.isEnabled = true
        Diagnostics.reset()

        Diagnostics.count("a")
        Diagnostics.count("a")
        let first = Diagnostics.drain()

        Diagnostics.count("a")
        let second = Diagnostics.drain()

        XCTAssertEqual(first["a"], 2)
        XCTAssertEqual(second["a"], 1, "the second report must be the delta, not the total")
        XCTAssertTrue(Diagnostics.drain().isEmpty, "a quiet second must report nothing at all")
    }

    /// The line it prints has to be readable on a phone-sized paste, and has to
    /// put the busiest counter first — that is the whole answer.
    func testTheLinePutsTheBusiestCounterFirst() throws {
        Diagnostics.isEnabled = true
        Diagnostics.reset()

        for _ in 0..<5 { Diagnostics.count("quiet") }
        for _ in 0..<900 { Diagnostics.count("loud") }

        let line = try XCTUnwrap(Diagnostics.report())

        XCTAssertTrue(line.contains("loud=900"), line)
        XCTAssertTrue(line.contains("quiet=5"), line)
        XCTAssertLessThan(
            try XCTUnwrap(line.range(of: "loud")).lowerBound,
            try XCTUnwrap(line.range(of: "quiet")).lowerBound,
            "the busiest counter is the answer; it goes first: \(line)")
    }

    /// Nothing happened, so there is nothing to say. A report every second that
    /// says "0" trains you to stop reading it.
    func testASilentSecondPrintsNothing() {
        Diagnostics.isEnabled = true
        Diagnostics.reset()

        XCTAssertNil(Diagnostics.report(), "a quiet second must print no line at all")
    }
}

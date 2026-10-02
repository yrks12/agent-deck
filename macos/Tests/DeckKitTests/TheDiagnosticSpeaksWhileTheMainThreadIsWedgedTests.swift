import XCTest
@testable import DeckKit

/// **The instrument was silent for exactly the seconds that mattered, and that
/// is why this has survived for days.**
///
/// He scrolled the conversation and the app froze. MEASURED on the running
/// process while it was stuck (`dist/freeze-sample-1212.txt`, `sample` for 6
/// seconds, 4898 samples): 100% CPU, `ps` state `RN`, **0 windows** answered to
/// the Accessibility API, and **4898 of 4898 main-thread samples inside one
/// `NSHostingView.beginTransaction`** — a single layout transaction that does
/// not return.
///
/// He was running that build with `DECK_DIAGNOSE=1`, which is meant to print one
/// line a second naming what is busy. Read what it printed during the freeze
/// (`dist/freeze-diagnose-1212.log`) — its **last line is an ordinary one**:
///
/// ```
/// [deck-diagnose] transcript.place=18 store.willChange=13 sidebar.body=12 …
/// ```
///
/// and then nothing. Not a spike. Not a warning. Nothing, because the reporter
/// was a `Timer` scheduled on the **main** run loop, and a main run loop cannot
/// service a timer while the main thread is inside the transaction. The one
/// instrument built to name this fault is switched off, by the fault, for the
/// entire duration of the fault.
///
/// That is the defect this file pins: **the reporter must speak from somewhere
/// the freeze cannot reach, and it must say that the main thread has stopped
/// answering and for how long.**
///
/// ## Red on `main`'s reporter, verbatim
///
/// `testALineComesOutWhileTheMainThreadIsWedged`, run against
/// `Timer.scheduledTimer(withTimeInterval:repeats:)` on the main run loop —
/// the reporter this repo shipped:
///
/// ```
/// TheDiagnosticSpeaksWhileTheMainThreadIsWedgedTests.swift:129: error:
///  -[…testALineComesOutWhileTheMainThreadIsWedged] : XCTAssertFalse failed -
///  the reporter spoke, but never said the main thread had stopped answering —
///  so the one line that names the fault is still missing. It said:
///  ["[deck-diagnose] on — one line a second, busiest counter first"]
///
/// Executed 5 tests, with 1 failure (0 unexpected) in 0.945 seconds
/// ```
///
/// Read the array. In 0.35 seconds of a wedged main thread the old reporter
/// said **one thing: hello**. That is the whole shape of
/// `freeze-diagnose-1212.log`.
///
/// ## Calibrated both ways
///
/// A wedged main thread must be named; a busy-but-alive one must **not** be, or
/// the line becomes noise and he stops reading it — which is the same failure as
/// printing nothing.
///
/// **No screen is taken.** No window, no view, no app bundle; every wait is
/// bounded at 0.35s.
final class TheDiagnosticSpeaksWhileTheMainThreadIsWedgedTests: XCTestCase {

    /// Everything the reporter said, collected off its own queue.
    private final class Heard: @unchecked Sendable {
        private let lock = NSLock()
        private var lines: [String] = []
        func append(_ line: String) { lock.lock(); lines.append(line); lock.unlock() }
        var all: [String] { lock.lock(); defer { lock.unlock() }; return lines }
        var stalls: [String] { all.filter { $0.contains("MAIN THREAD STALLED") } }
    }

    /// Short enough that the whole file is under half a second, long enough that
    /// a tick lands inside it.
    private static let tick: TimeInterval = 0.05
    private static let stallAfter: TimeInterval = 0.1
    private static let wedge: TimeInterval = 0.35

    override func tearDown() {
        Diagnostics.stopReporting()
        Diagnostics.emit = { line in FileHandle.standardError.write(Data((line + "\n").utf8)) }
        Diagnostics.reset()
        Diagnostics.isEnabled = false
        super.tearDown()
    }

    // MARK: - the sentence itself, without wedging anything

    func testItSaysNothingWhileTheMainThreadIsAnswering() {
        let now = Date()
        XCTAssertNil(
            Diagnostics.stallLine(now: now, lastReply: now.addingTimeInterval(-0.01),
                                  stallAfter: 2),
            "a main thread that answered 10ms ago is healthy; accusing it of "
            + "stalling makes the line noise and he stops reading it")
    }

    func testItNamesTheStallAndHowLongItHasLasted() throws {
        let now = Date()
        let line = try XCTUnwrap(
            Diagnostics.stallLine(now: now, lastReply: now.addingTimeInterval(-6.2),
                                  stallAfter: 2),
            "six seconds of silence from the main thread is the freeze he "
            + "photographed, and it must produce a line")

        XCTAssertTrue(line.contains("MAIN THREAD STALLED"), line)
        XCTAssertTrue(
            line.contains("6.2s"), "the line has to say how long, because 0.3s is a "
            + "stutter and 6s is the outage: \(line)")
    }

    // MARK: - the measurement that the previous reporter could not make

    /// **The freeze, reproduced honestly: the main thread does not return.**
    ///
    /// A busy loop with no run-loop pump is exactly what
    /// `GraphHost.flushTransactions` looked like from the outside — the thread is
    /// running at 100% and servicing nothing. The reporter has to speak anyway.
    func testALineComesOutWhileTheMainThreadIsWedged() {
        let heard = Heard()
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        Diagnostics.emit = { heard.append($0) }
        Diagnostics.startReporting(every: Self.tick, stallAfter: Self.stallAfter)
        Diagnostics.count("transcript.place")

        // No `RunLoop.run`, deliberately: a pump here would service the very
        // queue the fault prevents from being serviced, and the test would pass
        // over the broken build.
        let deadline = Date().addingTimeInterval(Self.wedge)
        while Date() < deadline { _ = Date().timeIntervalSince1970 }

        Diagnostics.stopReporting()

        XCTAssertFalse(
            heard.all.isEmpty,
            "the reporter printed nothing at all while the main thread was wedged "
            + "for \(Self.wedge)s. That is what his freeze looked like from the "
            + "outside: 100% CPU, no window, and a diagnostic log that simply stops.")
        XCTAssertFalse(
            heard.stalls.isEmpty,
            "the reporter spoke, but never said the main thread had stopped "
            + "answering — so the one line that names the fault is still missing. "
            + "It said: \(heard.all)")
    }

    /// The other half of the calibration. Something has to be *wrong* for this
    /// line to appear, or it is decoration.
    func testABusyButAnsweringMainThreadIsNeverAccused() {
        let heard = Heard()
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        Diagnostics.emit = { heard.append($0) }
        Diagnostics.startReporting(every: Self.tick, stallAfter: Self.stallAfter)

        let deadline = Date().addingTimeInterval(Self.wedge)
        while Date() < deadline {
            Diagnostics.count("transcript.place")
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }

        Diagnostics.stopReporting()

        XCTAssertFalse(
            heard.all.isEmpty,
            "a main thread doing real work must still produce its ordinary "
            + "once-a-tick line")
        XCTAssertTrue(
            heard.stalls.isEmpty,
            "the main thread answered throughout and was still accused of "
            + "stalling — a line that cries wolf is one he learns to ignore, "
            + "which is the same outcome as printing nothing: \(heard.stalls)")
    }

    /// It ships in the build he runs. With the switch down it must schedule
    /// nothing and say nothing.
    func testTheSwitchStillTurnsTheWholeThingOff() {
        let heard = Heard()
        Diagnostics.isEnabled = false
        Diagnostics.emit = { heard.append($0) }

        Diagnostics.startReporting(every: Self.tick, stallAfter: Self.stallAfter)
        let deadline = Date().addingTimeInterval(0.15)
        while Date() < deadline {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }

        XCTAssertTrue(heard.all.isEmpty,
                      "a disabled diagnostic that still prints is a diagnostic that "
                      + "ships noise to the owner: \(heard.all)")
    }
}

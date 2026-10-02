import Foundation

/// **What is actually happening, per second, in the build he is running.**
///
/// This exists because four rounds of a 99% CPU bug were closed by measurement
/// and none by reading. The remaining fact is that the app burns a core only
/// when a real `HTTPDeckClient` is attached, with almost no network traffic and
/// no timers anywhere in the code. Reading it again is not going to work.
///
/// Turn it on with `DECK_DIAGNOSE=1`. Once a second it writes one line to
/// standard error naming every counter that moved, busiest first:
///
/// ```
/// [deck-diagnose] 1.0s  thread.body=3412 store.publish=3410 sync.publish=1 http=0
/// ```
///
/// **Off by default and free when off.** It ships in the same binary the owner
/// runs, so a disabled counter must not take a lock, allocate, or format a
/// string — `DiagnosticsTests` pins that.
public enum Diagnostics {

    /// Read once from the environment; settable so tests can drive it.
    /// `nonisolated(unsafe)` rather than an actor on purpose: this is called
    /// from view bodies on the main thread and from actors off it, and a
    /// diagnostic that needs `await` cannot be dropped into a `body`.
    nonisolated(unsafe) public static var isEnabled =
        ProcessInfo.processInfo.environment["DECK_DIAGNOSE"] == "1"

    nonisolated(unsafe) private static var counts: [String: Int] = [:]
    private static let lock = NSLock()

    /// One occurrence of `name`. The disabled path is a single boolean read.
    @inline(__always)
    public static func count(_ name: String) {
        guard isEnabled else { return }
        lock.lock()
        counts[name, default: 0] += 1
        lock.unlock()
    }

    public static func snapshot() -> [String: Int] {
        lock.lock(); defer { lock.unlock() }
        return counts
    }

    /// Everything since the last drain, and reset. Rates, not totals: a total
    /// cannot say whether the thing is *still* happening.
    public static func drain() -> [String: Int] {
        lock.lock(); defer { lock.unlock() }
        let taken = counts
        counts.removeAll(keepingCapacity: true)
        return taken
    }

    public static func reset() {
        lock.lock(); counts.removeAll(); lock.unlock()
    }

    /// One line, busiest first — the busiest counter is the answer. `nil` when
    /// nothing moved, because a line of zeroes every second trains you to stop
    /// reading it.
    public static func report() -> String? {
        let taken = drain()
        guard !taken.isEmpty else { return nil }
        let body = taken
            .sorted { ($0.value, $1.key) > ($1.value, $0.key) }
            .map { "\($0.key)=\($0.value)" }
            .joined(separator: " ")
        return "[deck-diagnose] \(body)"
    }

    // MARK: - saying it out loud, from somewhere the freeze cannot reach

    /// Where a line goes. Standard error in the app; a test replaces it, which
    /// is the only way to assert that a line came out *during* a wedge.
    nonisolated(unsafe) public static var emit: @Sendable (String) -> Void = { line in
        FileHandle.standardError.write(Data((line + "\n").utf8))
    }

    /// The last moment the main thread answered. Written from the main thread,
    /// read from the reporter's queue, so it takes the lock like everything else.
    nonisolated(unsafe) private static var lastMainReply = Date.distantPast
    private static let replyLock = NSLock()

    /// **"I am still here."** Dispatched to the main thread once a tick; if the
    /// main thread is inside a layout transaction that never returns, this never
    /// runs, and that silence is the measurement.
    public static func mainThreadAnswered(at moment: Date = Date()) {
        replyLock.lock(); lastMainReply = max(lastMainReply, moment); replyLock.unlock()
    }

    public static var lastMainThreadReply: Date {
        replyLock.lock(); defer { replyLock.unlock() }
        return lastMainReply
    }

    /// **The line the last six days never printed.** Pure, so it can be checked
    /// without wedging anything.
    ///
    /// `nil` while the main thread is answering. Once it has been silent for
    /// longer than `stallAfter`, this says so and says for how long — which is
    /// the difference between "the app feels slow" and "the main thread has been
    /// inside one layout pass for six seconds".
    public static func stallLine(
        now: Date, lastReply: Date, stallAfter: TimeInterval
    ) -> String? {
        let silent = now.timeIntervalSince(lastReply)
        guard silent >= stallAfter else { return nil }
        return String(
            format: "[deck-diagnose] MAIN THREAD STALLED %.1fs — it is inside one "
                + "update and has not come back; sample it now", silent)
    }

    nonisolated(unsafe) private static var reporter: DispatchSourceTimer?
    private static let reportQueue = DispatchQueue(label: "\(DeckIdentity.bundleID).diagnose")

    /// Starts the once-a-second report. Safe to call when disabled: it does
    /// nothing and schedules nothing.
    ///
    /// **Not a `Timer` on the main run loop, and that is the whole point.** The
    /// freeze he sampled is the main thread inside a single
    /// `NSHostingView.beginTransaction` that does not return — 4898 of 4898
    /// samples in SwiftUI layout, 0 windows answered to the Accessibility API.
    /// A main-run-loop timer cannot fire during that, so the instrument written
    /// to name the fault went **silent for exactly the seconds that mattered**:
    /// `dist/freeze-diagnose-1212.log` simply stops, and its last line before it
    /// stops is an ordinary one (`transcript.place=18`). That is why five rounds
    /// of this found nothing.
    ///
    /// So the reporter runs on its own queue and each tick asks the main thread
    /// to say it is alive. A wedged main thread cannot answer, the silence is
    /// timed, and the run he does next says *"MAIN THREAD STALLED 6.2s"* while
    /// it is still stalled.
    public static func startReporting(every tick: TimeInterval = 1.0,
                                      stallAfter: TimeInterval = 2.0) {
        guard isEnabled else { return }
        emit("[deck-diagnose] on — one line a second, busiest counter first")
        mainThreadAnswered()
        let timer = DispatchSource.makeTimerSource(queue: reportQueue)
        timer.schedule(deadline: .now() + tick, repeating: tick)
        timer.setEventHandler {
            DispatchQueue.main.async { mainThreadAnswered() }
            if let stalled = stallLine(
                now: Date(), lastReply: lastMainThreadReply, stallAfter: stallAfter) {
                emit(stalled)
            }
            if let line = report() { emit(line) }
        }
        timer.resume()
        reporter = timer
    }

    /// Only the tests need this; the app reports until it quits.
    public static func stopReporting() {
        reporter?.cancel()
        reporter = nil
    }
}

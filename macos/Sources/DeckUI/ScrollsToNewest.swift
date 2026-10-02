import Foundation
import DeckKit

/// **When the conversation asks to be taken to its newest line — and, more to
/// the point, when it is not allowed to.**
///
/// The 167-second freezes were localised by A/B on his own display, his own
/// data, one variable per build, with the bar (15 minutes / ~900 diagnostic
/// ticks including reconnects) set before the run:
///
/// ```
/// build                                    reopens survived   stalls
/// main, unchanged                                 4           stalled
/// alignment removed from the ScrollView cap       4           stalled
/// proxy.scrollTo removed, nothing else           17           ZERO
/// ```
///
/// 906 ticks and 17 thread reopens with no stall. Alignment is refuted. The
/// scroll-to-newest is the driver.
///
/// **The mechanism, from three `sample(1)` captures taken mid-stall.**
/// `transcript.place` at 3,200–3,900/sec and `transcript.measure` at
/// 1,600–1,950/sec — a steady 2:1 — while `store.willChange`, `thread.body`
/// and `transcript.body` all read **zero**. No state changed and no body ran:
/// this is one graph transaction placing a lazy stack over and over without
/// converging. The anchor is the **last** row of a `LazyVStack`, which is
/// unrealised, so its offset is an estimate; scrolling toward it realises rows,
/// which moves the content height, which moves the offset, which re-scrolls.
/// Every episode is preceded within four ticks by
/// `http.streamOpened=1 store.openThread=1 sync.connectAttempt=1` — a stream
/// reconnect rebuilding the transcript and re-arming that anchor against a
/// freshly unrealised list.
///
/// ## The property this type exists to hold
///
/// **A scroll request must not be issued inside the change that provoked it.**
/// `onChange` runs while SwiftUI is mid-update; a `scrollTo` from there adds a
/// scroll target to a transaction that is still resolving, and a reconnect
/// delivering a burst of lines adds one per line. So the request is *recorded*
/// here and *issued* on a later turn of the run loop, and a burst collapses to
/// the newest row rather than one scroll apiece.
///
/// Measured off-display at 608x949 over the shipped `TranscriptList`, one line
/// appended to a settled 52-message thread, with only a synchronous
/// `layoutSubtreeIfNeeded()` and no run loop:
///
/// ```
///                       scroll offset   document height
/// settled                  y=0.0            4260
/// main: after sync layout  y=3324.5         4326    <- landed mid-transaction
/// here: after sync layout  y=0.0            4326    <- nothing has moved yet
/// here: after the run loop y=3346.0         4326    <- and now it has
/// ```
///
/// `TheTranscriptScrollLandsAfterTheLayoutItProvokedTests` is that measurement
/// as assertions, in both directions.
///
/// ## What this does not claim
///
/// The spin has never reproduced without a display — eleven attempts, and
/// `TranscriptSettlesTests` is the record of five of them. Off-display the
/// scroll converges (4260 → 4326 and stops), so nothing here can show the
/// divergence ending. What is proven is that the structural precondition the
/// A/B named is gone: the scroll no longer re-enters the transaction that
/// triggered it, and a burst of lines no longer stacks scroll targets.
///
/// ## Still open, and better behaviour besides
///
/// Today the transcript yanks him to the newest line even when he has scrolled
/// up to read something. Scrolling only when he is already near the bottom
/// would fix that *and* cut the request rate further, but reading the scroll
/// offset from SwiftUI needs `onScrollGeometryChange`, which is macOS 15; this
/// package targets macOS 14. It would need an `NSScrollView` bridge, and a
/// measurement bridge into this view is the exact shape of the fault, so it is
/// not being added on a guess.
final class ScrollsToNewest {
    /// Hands a block to a later turn of the run loop. Injected so a test can
    /// hold the turn in its hand instead of sleeping on a guess.
    typealias Schedule = (@escaping () -> Void) -> Void

    /// Named here so the shipped binary and the tests cannot drift on the
    /// spelling. With `DECK_DIAGNOSE=1` a burst that coalesced reads as one
    /// `transcript.scroll` against many `transcript.body`; a burst that did not
    /// reads one apiece. Zero cost with the switch down.
    static let requestCounter = "transcript.scroll"

    private let schedule: Schedule
    /// The newest row we owe a scroll to and the proxy to take it there, or
    /// nil if we owe none.
    private var pending: (row: String, scroll: (String) -> Void)?
    /// Whether a turn has already been booked. **This is the coalescing**: the
    /// second line of a burst overwrites `pending` and books nothing.
    private var booked = false

    /// How many scroll requests have actually been issued. Only a test reads
    /// it; it is the number the coalescing assertion is about.
    private(set) var issued = 0

    init(schedule: @escaping Schedule = { work in DispatchQueue.main.async(execute: work) }) {
        self.schedule = schedule
    }

    /// The newest drawn row is now `id`. **Never scrolls inside this call** —
    /// that is the whole point of the type.
    ///
    /// `scroll` is kept alongside the row and **replaced** by each later call,
    /// not captured once: it closes over the `ScrollViewProxy` of the body
    /// running now, and that body is rebuilt whenever the conversation changes.
    /// Firing the burst's *first* closure against the burst's *last* row would
    /// be scrolling a proxy he has already left.
    func newestRow(is id: String, scroll: @escaping (String) -> Void) {
        pending = (id, scroll)
        guard !booked else { return }
        booked = true
        schedule { [weak self] in
            guard let self else { return }
            self.booked = false
            guard let owed = self.pending else { return }
            self.pending = nil
            self.issued += 1
            Diagnostics.count(Self.requestCounter)
            owed.scroll(owed.row)
        }
    }
}

import SwiftUI
import DeckKit

/// **A layout pass, counted, in the build he is actually running.**
///
/// Five rounds of the 100% CPU defect were closed by reading code and none of
/// them held. The sample taken while he had the app at 98.8% and said *"still
/// stuck"* named the thing at last, and it was not any counter this repo had:
///
/// ```
/// NSHostingView.beginTransaction()
///  -> GraphHost.flushTransactions()                       4081
///    -> AG::Subgraph::update()                            3600
///      -> LazySubviewPlacements.updateValue()              934
///        -> LazySubviewPlacements.placeSubviews(...)       869
///          -> LazyStack<>.place(subviews:context:cache:in:)  832
/// ```
///
/// `ListChurnTests`, `SidebarChurnTests` and `InspectorIsQuietTests` count
/// **body evaluations**; `LayoutSettlesTests` counts **root layout passes**.
/// Every one of them stayed green through the whole outage, because the spin is
/// SwiftUI *placing the subviews of a lazy stack* thousands of times inside a
/// graph transaction that never converges — and only 89 of 4095 samples were in
/// our binary at all.
///
/// **So this counts placements**, and it does it in the shipped binary rather
/// than in a test, because the fault has never once reproduced in a window that
/// is not on a screen. `TranscriptSettlesTests` is the record of five ways of
/// trying, including a `CVDisplayLink` that delivered 72 real vsync ticks in
/// 0.6s to a hosted 200-row list and moved this counter zero times. He has a
/// screen and the tests must never take it, so the instrument goes where the
/// screen is.
///
/// ## What he does with it
///
/// ```
/// DECK_DIAGNOSE=1 "$HOME/Projects/deck-app/dist/Agent Deck.app/Contents/MacOS/Agent Deck"
/// ```
///
/// The binary inside the bundle, not `open`: the counters go to standard error
/// once a second and `open` detaches the process from the terminal, so he would
/// get the app and none of the answer. The bundle is the one
/// `Scripts/make-app-bundle.sh` writes; it is not installed under
/// `/Applications`.
///
/// One line a second, busiest counter first. A settled transcript never names
/// `transcript.place` at all — `Diagnostics.report` omits a counter that did
/// not move. A spinning one puts it at the front of the line with a number in
/// the hundreds or thousands, and that is the answer, in a minute, without
/// anyone having to guess from the outside again.
///
/// ## Why it is a `Layout` and not a hook in `body`
///
/// A body counter cannot see this: the transcript's body ran once and the
/// placement pass ran nine hundred times. `Layout` is the only public seat on
/// the placement pass itself, and this one is a pass-through — it proposes what
/// it was proposed and places its child filling the bounds it was given, so it
/// moves nothing. `TranscriptSettlesTests` pins that by laying the same list
/// out with the counter on and off and comparing the heights.
struct CountsPlacements: Layout {
    /// **Stored, never built at the call site.** `Diagnostics.count` is free
    /// when the switch is off only if its *argument* is free too, and
    /// `name + ".place"` would allocate and format a string on every placement
    /// of every shipped build. `DiagnosticsTests` pins the disabled path.
    let placements: String
    let measurements: String

    func sizeThatFits(
        proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
    ) -> CGSize {
        Diagnostics.count(measurements)
        return subviews.first?.sizeThatFits(proposal) ?? .zero
    }

    func placeSubviews(
        in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()
    ) {
        Diagnostics.count(placements)
        subviews.first?.place(
            at: bounds.origin, anchor: .topLeading,
            proposal: ProposedViewSize(bounds.size))
    }
}

extension View {
    /// Counts this view's placement and measurement passes — **only while
    /// diagnostics are on**.
    ///
    /// With the switch down the view tree is byte-for-byte the one that was
    /// there before this file existed, so the diagnostic cannot itself become
    /// the next round of the defect it was written to find. That is not
    /// caution for its own sake: this app has already cost him a day to a
    /// wrapper that looked free.
    @ViewBuilder
    func countingPlacements(_ placements: String, _ measurements: String) -> some View {
        if Diagnostics.isEnabled {
            CountsPlacements(placements: placements, measurements: measurements) { self }
        } else {
            self
        }
    }
}

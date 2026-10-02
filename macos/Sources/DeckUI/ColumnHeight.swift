import SwiftUI

// MARK: - read this before you take the `alignment:` argument off
//
// **These three frames are not the 2026-09-07 freeze, and it was measured.**
//
// The freeze sample (`dist/freeze-sample-1212.txt`, 4898 of 4898 main-thread
// samples in SwiftUI layout) recurses through exactly the construct below:
//
//     _FlexFrameLayout.placement(of:in:)
//       FrameLayoutCommon.commonPlacement(of:in:childProposal:)
//         ViewDimensions.subscript.getter
//           LayoutEngineBox.explicitAlignment(_:at:)      <- 1087 frames
//             UnaryLayoutEngine.childPlacement(at:)
//               _FlexFrameLayout.placement(of:in:)        <- re-enters
//
// `_FlexFrameLayout` **is** `.frame(...)` and `explicitAlignment` is what an
// alignment argument resolves, so these three look guilty. They are not — but
// read the next paragraph before quoting the numbers, because the first
// measurement of this was made over the wrong tree.
//
// **The first sweep measured a plain `VStack`, and the shipped transcript is a
// `LazyVStack`.** `TheTranscriptsLayoutWorkIsLinearTests.shippedTranscript`
// substitutes one for the other on purpose (a lazy stack off-screen realises
// only the rows in view, which would read flat for the wrong reason) and that
// substitution is right for the question *that* file asks — layout cost per
// realised row. It is wrong for this one. Over a plain `VStack` every row is
// already measured, so an alignment-guide query is a cache hit and is free by
// construction; over a `LazyVStack` it is a `measureEstimates` walk, or it
// would be. So the sweep could not have cleared the argument, whatever it read.
//
// MEASURED again, over the real thing — the shipped `TranscriptRowView` inside
// a real `LazyVStack` inside a `ScrollView`, off-display at 608x949, one
// settle, only the alignment argument changed:
//
//     messages                    10      30      52     120
//     alignment: .top            7 / 5   7 / 5   7 / 5   7 / 5   measures/places
//     alignment: default         7 / 5   7 / 5   7 / 5   7 / 5
//     rows realised, either       10      14      14      14
//
// Identical to the operation, at every message count, over the tree that
// ships. **The conclusion survives; the reasoning that reached it did not.** A
// non-default alignment on a flexible frame costs zero extra layout work here.
//
// **And the `.frame` is doing more than hinting an ideal.** It was proposed
// that this modifier be replaced by a one-child `Layout` barrier, on the
// grounds that a `.frame` "still forwards nil proposals and still exposes the
// child's `ViewDimensions`". Measured, same content, only the cap varying:
//
//     messages                    10      30      52     120
//     UNCAPPED, rows realised     10      30      52     120   <- one per message
//     CAPPED  , rows realised      2       2       2       2
//
// `idealHeight: 0` answers a nil height proposal itself and never asks the
// child, so the `measureEstimates` walk never starts. There is nothing left
// for a barrier to cut, and taking this modifier off costs the walk *and* the
// window: uncapped, the transcript reports 896 / 2,502 / 4,275 / 9,594 points
// of ideal height for 10 / 30 / 52 / 120 messages.
//
// Two more things the original sweep found, both of which say to leave this
// alone:
//
//   * `takesTheSizeItIsGiven()` **reduces** the transcript's layout work. With
//     the column root capped, the rows cost 3.00 operations each; without it,
//     17.00 — the uncapped column asks the scroll view for an ideal height, and
//     an ideal height means measuring the whole conversation again.
//   * the alignment-bearing flexible frames the sample actually recurses
//     through are older than this file. `git log -L` on each of them:
//     `ApprovalCardView.swift:71` — 6e5d164, 2026-09-01. `ThreadView.swift:291`
//     — 9657e59, 2026-09-04. This file — 77bc6fe, **2026-09-07**, the morning of
//     the freeze. These frames **joined** that class; they did not create it,
//     and he had been reporting freezes for days before they existed.
//
// So `alignment:` stays, and with it the property it buys — content pinned to
// the top of a column instead of floating in the middle of one. Pinned over the
// shipped lazy tree by
// `TheTranscriptsLayoutWorkIsLinearTests.testTopAlignmentCostsExactlyNothing`,
// which fails the moment those two rows of numbers stop being equal; the fix
// then is a zero-spacing `VStack` with a `Spacer(minLength: 0)`, which pins the
// top without an alignment-guide query. The cap itself is pinned by
// `TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests`.

extension View {
    /// **This view takes the height the window gives it, and never asks for
    /// one of its own.**
    ///
    /// He opened the app twice over two days and saw an empty window: no roster,
    /// no search field, no conversation, just a dark column. MEASURED on his
    /// display, from the Accessibility tree of the running process:
    ///
    /// ```
    /// AXWindow              @(304,  33)  1208 x   949     <- correct
    ///  └ AXHostingView      @(304,  33)  1208 x   949     <- correct
    ///     └ AXSplitGroup    @(304,-9328) 1208 x 19723     <- the defect
    /// ```
    ///
    /// The split view behind `NavigationSplitView` was **19,723 points tall
    /// inside a 949-point window**, and vertically centred, so the visible band
    /// landed in its empty middle: the search field sat 9,296pt above the top of
    /// the window and the footer 9,322pt below the bottom of it. Everything was
    /// built, laid out and alive. None of it was anywhere he could see.
    ///
    /// ## What actually reports the number
    ///
    /// A `ScrollView` answers the question "how tall would you like to be?" with
    /// **the height of its content**. That is right for a scroll view sizing
    /// itself to a sheet and catastrophic for one that is a window column:
    ///
    /// ```
    /// IDEAL transcript-52: intrinsic=(172.0, 3002.0) fitting=(630.0, 3002.0)
    /// ```
    ///
    /// — 3,002pt for 52 short test lines; 19,527 for his 52 real ones. Each
    /// column of a `NavigationSplitView` is hosted in its own `NSHostingView`
    /// inside an `NSSplitViewItem`, and that hosting view publishes SwiftUI's
    /// ideal height to AppKit as its **intrinsic content size**. Auto Layout
    /// treats it as a requirement, grows the enclosing `NSSplitView` until it is
    /// satisfied, and centres the result in a window that cannot hold it. The
    /// number is content-dependent, which is why one session measured 1,990 and
    /// the next 19,723 — the same fault, a longer conversation.
    ///
    /// ## What this does
    ///
    /// Sets an ideal height of zero while leaving the view free to fill
    /// whatever it is proposed. Asked "how tall would you like to be?" the
    /// column now answers "however tall you are" — the hosting view reports
    /// `noIntrinsicMetric` for height, Auto Layout has nothing to satisfy, and
    /// the split view stays the size of the window. Nothing about how the view
    /// is *placed* changes: given 949 points it still receives 949 and still
    /// scrolls its content inside them.
    ///
    /// Top-aligned on purpose. `maxHeight: .infinity` alone centres content that
    /// is shorter than the column, which would have quietly moved the settings
    /// panel down its own column the day it was applied.
    ///
    /// Pinned by `AColumnFitsTheWindowItIsInTests`, which measures the ideal
    /// height of each shipped column and is calibrated against a view built to
    /// over-report.
    func takesTheHeightItIsGiven() -> some View {
        frame(minHeight: 0, idealHeight: 0, maxHeight: .infinity, alignment: .top)
    }

    /// **This view takes the WIDTH the column gives it, and never asks for one
    /// of its own.**
    ///
    /// Same defect as above, other axis, and it survived the first round of
    /// this fix. MEASURED on his display, from the Accessibility tree of the
    /// running app *after* the height was corrected:
    ///
    /// ```
    /// AXWindow        @(304, 33)  1208 x 949
    ///  ├ AXGroup      @(242,-473)  300 x 2013   <- roster column
    ///  └ AXGroup      @(234,-481) 1347 x 2029   <- detail column
    /// ```
    ///
    /// The window begins at x=304 and **both columns begin left of it**, at 242
    /// and 234. A column wider than the space it is given is centred in that
    /// space exactly as a too-tall one is centred in the window, so it hangs off
    /// both edges — which is why he could see approval cards in the middle of
    /// the window and the inspector was clipped past the right edge of the
    /// screen entirely.
    ///
    /// ## What reports the number
    ///
    /// A `Text` answers "how wide would you like to be?" with the width of the
    /// **whole sentence on one line**. Measured in a 300pt inspector:
    ///
    /// ```
    /// IDEAL FlatLabel(delivery.caveat) @300: intrinsicW=661.0
    /// IDEAL SettingsPanelView          @300: intrinsicW=721.0
    /// ```
    ///
    /// 661 of those 721 points are one sentence — `NotificationDelivery.caveat`.
    /// It is deliberately NOT quoted here: `OneSentenceAboutDeliveryTests` holds
    /// that exactly one file in the app may say what a notification reaches him
    /// on, and a copy in a comment goes stale in silence like any other.
    /// `.fixedSize(horizontal: false, vertical: true)` pins
    /// the *vertical* axis so the sentence may wrap; it leaves the ideal width
    /// exactly where it was. It is not one bad view: **every** wrapping sentence
    /// in either column does this, which is why this is applied at the column
    /// boundary rather than at any one of them.
    ///
    /// Leading-aligned on purpose, for the same reason the height variant is
    /// top-aligned: `maxWidth: .infinity` alone centres content narrower than
    /// the column.
    func takesTheWidthItIsGiven() -> some View {
        frame(minWidth: 0, idealWidth: 0, maxWidth: .infinity, alignment: .leading)
    }

    /// **A window column, on both axes: it takes the size it is given and asks
    /// for nothing.**
    ///
    /// This is the one applied at the column roots in `DeckRootView`, because
    /// the root is where the fault leaves the app. Each column of a
    /// `NavigationSplitView` is hosted in its own `NSHostingView` inside an
    /// `NSSplitViewItem`, and *that hosting view* is what publishes SwiftUI's
    /// ideal size to AppKit as `intrinsicContentSize`. Anything inside a column
    /// that still asks for a size of its own rides straight out through the
    /// root — so capping descendants one at a time fixes the ones you found and
    /// leaves the class open. Capping the root closes it.
    ///
    /// Pinned by `AColumnFitsTheWindowItIsInTests`, which measures both axes of
    /// every column root as `DeckRootView` composes them and is calibrated
    /// against views built to over-report on each axis.
    func takesTheSizeItIsGiven() -> some View {
        frame(
            minWidth: 0, idealWidth: 0, maxWidth: .infinity,
            minHeight: 0, idealHeight: 0, maxHeight: .infinity,
            alignment: .topLeading)
    }
}

import SwiftUI
import DeckKit

/// **The conversation, and the one view in this app that refuses to be
/// redrawn.**
///
/// Five rounds of the 100% CPU defect went on making rows cheaper — a spinner,
/// a `Label`, the window chrome, text selection, a wall of pasted text. Each
/// was a real cost. None was the fault. The sample taken while the owner was
/// using the app, rather than while it sat idle, named it:
///
/// ```
/// 119 swift::RefCounts<…>            46 ForEachState.item(
/// 117 _swift_getGenericMetadata(     35 Array<A>.motionVectors(
/// ```
///
/// That is a view tree being **rebuilt**, not a stable one being laid out. The
/// list was being treated as changed over and over, and every per-row cost
/// anyone measured was the price of a rebuild that should not have happened.
///
/// **Why equality is the fix and not a smaller publish.** `DeckStore` drives
/// the sidebar *and* the conversation, so a desk changing state — which it does
/// continuously while it works, which is exactly when he said *"when its
/// working its stuck on my ui"* — is a real change to the sidebar and no change
/// at all to the transcript. Suppressing that publish would be wrong; the
/// sidebar has to move. What has to stop is it reaching this list. So the list
/// takes **values** rather than the store, and `.equatable()` lets SwiftUI skip
/// it whole.
///
/// **The dangerous half is the equality itself.** A transcript that never
/// rebuilds is a transcript that never shows him a reply — which is worse than
/// the spin, because a slow app looks broken and a silent one looks finished.
/// `ListChurnTests` asserts both directions: equal for the same conversation,
/// and **not** equal the moment a message is added.
///
/// ## The chain of command is drawn here and decided nowhere near here
///
/// He held this beside the reference app and said *"we are long we from their
/// experience"*. Four of the differences are one idea — the transcript has to
/// say **who talked to whom**, and it must never make him scroll past machine
/// chatter to find the answer he asked for. Attribution rows, the traffic
/// rollup, the date dividers and the turn's face are all decided in
/// `ThreadTimeline.items`, over the values this view already receives, and this
/// file only draws what came back. That is the same rule the equality above
/// rests on: a row that had to work anything out for itself would put the spin
/// straight back.
struct TranscriptList: View, Equatable {
    let entries: [ThreadEntry]
    /// The desk this conversation belongs to — what turns a `peer:` id into the
    /// *other* desk's wire name, and so what the faces are derived from.
    var desk: String = ""
    var notice: String?
    var problem: String?
    var openPeerThread: (String) -> Void = { _ in }
    var answer: (ApprovalCard, ApprovalOption) -> Void = { _, _ in }
    /// The first line he had not read when he opened it; `NEW` goes above it.
    var newFrom: String?
    /// The desk's character while it works. **An object, not a value, and not
    /// compared**: a desk going to work is not a change to the conversation
    /// (`ListChurnTests`), so only the rows that draw the character observe it.
    var live: TranscriptLive?
    /// K4: what a tap on a decision card does. Not compared, like the others.
    var decide: (Decision, String) -> Void = { _, _ in }
    /// The bubble he last clicked (⌘R answers it). Compared: it is drawn.
    var selectedMessageID: String?
    /// A tap on a quote. Compared, so a new tap reaches `onChange` below.
    var jump: QuoteJumpRequest?
    /// Quote-replies. Closures, so not compared.
    var reply: ((Message) -> Void)?
    var select: ((String) -> Void)?
    var openQuote: ((MessageQuote) -> Void)?
    /// The deck holds older lines than the first one drawn. Compared: it draws
    /// "Show earlier messages" at the top.
    var hasEarlier = false
    /// A page of older lines is on its way. Compared: it dims the control.
    var loadingEarlier = false
    /// Fetches one page further back. A closure, so not compared.
    var loadEarlier: (() -> Void)?

    /// The row that was at the top when he asked for older lines. A prepend
    /// keeps the scroll offset, not the row under it, so without this the
    /// page he asked for would push what he was reading off the screen.
    @State private var keepInView: String?

    /// Which rollups he has opened. **View state on purpose**: it is about what
    /// he is looking at, not about the conversation, and putting it in the
    /// store would publish it to the sidebar as well.
    @State private var opened: Set<String> = []

    /// **What keeps the scroll out of the layout pass that provokes it.**
    ///
    /// `@State`, so it outlives the value type it hangs off and one instance
    /// serves the whole conversation. It holds nothing about the transcript —
    /// only which row is owed a scroll and whether a turn of the run loop has
    /// been booked — so it publishes nothing and cannot itself redraw the list.
    /// See `ScrollsToNewest` for the A/B that put it here.
    @State private var scroller = ScrollsToNewest()

    /// Whether the newest line is on screen, for the ↓ button. A reference the
    /// list writes and never observes, so scrolling does not rebuild the list —
    /// only the button redraws.
    @State private var bottom = BottomTracker()

    /// **The counter that would have been unmistakable during the spin.**
    ///
    /// `transcript.body` below says the list was rebuilt. It stayed flat
    /// through the whole 98.8% outage, because the list was *not* being
    /// rebuilt — it was being placed, over and over, inside a graph transaction
    /// that never converged. This names that pass instead, on the lazy stack
    /// itself, which is the exact frame the live sample sat in
    /// (`LazyStack.place(subviews:context:cache:in:)`, 832 samples).
    ///
    /// Public so the test and the running app cannot drift apart on the
    /// spelling — a diagnostic nobody can assert on is a diagnostic nobody can
    /// trust. Zero cost with `DECK_DIAGNOSE` unset; see `CountsPlacements`.
    static let placementCounter = "transcript.place"
    static let measurementCounter = "transcript.measure"

    /// Closures are never equal, so they are deliberately not compared: what
    /// this view draws is decided entirely by the values above, and the
    /// closures only decide what a tap does.
    static func == (lhs: TranscriptList, rhs: TranscriptList) -> Bool {
        lhs.entries == rhs.entries
            && lhs.desk == rhs.desk
            && lhs.notice == rhs.notice
            && lhs.problem == rhs.problem
            && lhs.newFrom == rhs.newFrom
            && lhs.selectedMessageID == rhs.selectedMessageID
            && lhs.jump == rhs.jump
            && lhs.hasEarlier == rhs.hasEarlier
            && lhs.loadingEarlier == rhs.loadingEarlier
    }

    private var hasApprovalContent: Bool { notice != nil || problem != nil }

    /// **The drawing order**, with the rollups he has opened spread back out.
    ///
    /// A function of values, so it can be read by a test with no screen and so
    /// nothing here is decided while a row is being laid out. Opening a rollup
    /// keeps the rollup row — that is how he closes it again — and puts what it
    /// was standing in for underneath it.
    func timeline(opened: Set<String>) -> [TranscriptItem] {
        ThreadTimeline.items(entries, desk: desk, newFrom: newFrom).flatMap { item -> [TranscriptItem] in
            guard case .rollup(let rollup) = item, opened.contains(rollup.id) else { return [item] }
            return [item] + ThreadTimeline.expanded(rollup, desk: desk)
        }
    }

    var body: some View {
        // Counted so a test can prove this body was skipped. A rebuild that
        // nobody can observe is a rebuild nobody can stop coming back.
        let _ = Diagnostics.count("transcript.body")

        if entries.isEmpty && !hasApprovalContent {
            ContentUnavailableView(
                "No messages yet",
                systemImage: "text.bubble",
                description: Text("Nothing has been said in this thread.")
            )
            .frame(maxHeight: .infinity)
        } else {
            // **Built once per body, not twice.** The drawing order was worked
            // out here for the `ForEach` and then worked out a second time
            // inside `onChange` — the same `ThreadTimeline.items` walk over the
            // same entries, allocating a second array of the whole conversation
            // on every line a desk sends. That is the retain/release traffic at
            // the bottom of the third freeze sample. One walk, shared.
            let drawn = timeline(opened: opened)
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(spacing: TranscriptMetrics.rowSpacing) {
                        if hasEarlier, let loadEarlier {
                            EarlierMessagesRow(loading: loadingEarlier) {
                                keepInView = drawn.first?.id
                                loadEarlier()
                            }
                        }
                        if entries.isEmpty {
                            Text("Nothing has been said in this thread.")
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .padding(.vertical, 8)
                        }
                        // **One list, in the order things happened.** A tool
                        // call is not a footnote under the conversation — it is
                        // a thing the desk did at a time, and it is drawn where
                        // that time is.
                        ForEach(drawn) { item in
                            itemView(item, isLast: item.id == drawn.last?.id) {
                                // The character just appeared under the newest
                                // line; follow it only if he was at the bottom.
                                guard bottom.isAtBottom, let target = drawn.last?.id else { return }
                                scroller.newestRow(is: target) { row in
                                    proxy.scrollTo(row, anchor: .bottom)
                                }
                            }
                                .id(item.id)
                                // Whether the newest row is realised is whether
                                // he is at the bottom — for the ↓ button. No
                                // extra row: one after the last line moved
                                // where the scroll lands.
                                .onAppear { if item.id == drawn.last?.id { bottom.set(true) } }
                                .onDisappear { if item.id == drawn.last?.id { bottom.set(false) } }
                        }
                        // Only the two things that are *about* the list rather
                        // than in it: a request this window could not read, and
                        // an answer that did not land.
                        approvalFootnotes
                    }
                    .padding(.horizontal, TranscriptMetrics.horizontalPadding)
                    .padding(.vertical, TranscriptMetrics.verticalPadding)
                    // Counted around the lazy stack, not around each row: a
                    // wrapper per row would put a layout container between
                    // every bubble and the column that decides its width, and
                    // width is the half of this fault that *is* measurable.
                    // Measured either way on a 200-row list churning at 0.9 of
                    // a core — the container reports 399-594 passes a second
                    // and the rows 12k-18k, and both read exactly 0 settled, so
                    // the container is enough to answer the only question he
                    // has: is it still working, or is it done.
                    .countingPlacements(Self.placementCounter, Self.measurementCounter)
                }
                // **The one line between his 949-point window and a
                // 19,723-point split view.** A `ScrollView` reports its
                // *content's* height as the height it would like to be, and
                // this one is a window column: 52 messages asked for 19,527
                // points, AppKit took that as the column's required intrinsic
                // height, grew the split view to it and centred it — so the
                // conversation, the roster and the footer all sat thousands of
                // points off the screen and he saw an empty app. See
                // `ColumnHeight`; pinned by `AColumnFitsTheWindowItIsInTests`.
                //
                // **It is also what keeps the LazyVStack lazy, which is the
                // more expensive half.** A `ScrollView` measures its content
                // with a nil proposal on the scroll axis, and a `LazyVStack`
                // handed a nil height cannot be lazy — it walks every row of
                // the `ForEach`. MEASURED over this content, only the cap
                // varying: uncapped, 10 / 30 / 52 / 120 row bodies for 10 / 30
                // / 52 / 120 messages, exactly one each; capped, 2 at every
                // length. The `idealHeight: 0` answers that nil proposal
                // itself and never asks the scroll view, so the walk never
                // starts. Anyone replacing this modifier has to keep that
                // property, not just the ideal-size one.
                .takesTheHeightItIsGiven()
                // ↓ — back to the newest line, shown only when he has scrolled
                // away from it.
                .overlay(alignment: .bottom) {
                    JumpToNewestButton(tracker: bottom) {
                        guard let target = drawn.last?.id else { return }
                        scroller.newestRow(is: target) { row in
                            proxy.scrollTo(row, anchor: .bottom)
                        }
                    }
                }
                // **A mount is not an update, so it needs its own request.**
                // `DeckStore.open(threadID:)` sends `thread` through `.loading`
                // before `.loaded(screen)`, so opening a thread — the first one
                // ever, or switching from one to another — tears the `.loaded`
                // case's view subtree down and mounts a brand new
                // `TranscriptList` with `entries` already full. `onChange`
                // below only compares *later* updates to an already-mounted
                // view, so that first render booked no scroll at all and the
                // ScrollView sat wherever a fresh LazyVStack starts — the top,
                // oldest lines first, exactly "when i open chat i need to scrol
                // down".
                //
                // `onChange(of:initial:true)` looks like the fix, and is not
                // one: measured on this exact view, with it in place a line
                // that arrives right after a fresh mount settles lands
                // reproducibly 104pt short of the true end — the append's own
                // scroll request, not the mount's, stops landing. `onAppear`
                // alongside the untouched `onChange` keeps "first appearance"
                // and "a later change" as two separate requests into the same
                // coalescing queue instead of one modifier straddling both, and
                // `TranscriptOpensAtTheNewestMessageTests` pins both directions.
                .onAppear {
                    guard let target = drawn.last?.id else { return }
                    scroller.newestRow(is: target) { row in
                        proxy.scrollTo(row, anchor: .bottom)
                    }
                }
                // A tap on a quote: open the rollup the original is folded
                // into when it is, then scroll to it a turn later — the same
                // reason `ScrollsToNewest` defers, never inside this update.
                .onChange(of: jump) { _, request in
                    guard let request else { return }
                    switch QuoteJump.destination(of: request.messageID, in: timeline(opened: opened)) {
                    case .row(let row):
                        DispatchQueue.main.async { proxy.scrollTo(row, anchor: .center) }
                    case .insideRollup(let rollupID, let row):
                        opened.insert(rollupID)
                        DispatchQueue.main.async { proxy.scrollTo(row, anchor: .center) }
                    case .notLoaded:
                        break
                    }
                }
                // Older lines landed above: put back the row he was reading.
                .onChange(of: entries.first?.id) { _, _ in
                    guard let row = keepInView else { return }
                    keepInView = nil
                    DispatchQueue.main.async { proxy.scrollTo(row, anchor: .top) }
                }
                .onChange(of: entries.last?.id) { _, last in
                    guard last != nil else { return }
                    // The **last drawn row**, not the last entry: a turn now
                    // ends with the desk's face under it, and anchoring on the
                    // bubble above would leave that face off the bottom edge
                    // and read as a cut-off screen.
                    guard let target = drawn.last?.id else { return }
                    // **Not animated.** `Array<A>.motionVectors` is in the live
                    // sample at 35 frames: an animated transaction over this
                    // list re-measures every row of the LazyVStack for as long
                    // as it runs, and a desk mid-reply sends lines faster than
                    // the animation lasts — so it never stops. This app has
                    // already cost him a day for a permanent animation once.
                    //
                    // The anchor is the deck's own message id, so it moves once
                    // per line and not once per publish — measured in
                    // `TranscriptScrollAnchorTests`.
                    //
                    // **That file did NOT rule this modifier out, and the note
                    // here that said it had was wrong.** It counts how OFTEN
                    // the anchor moves and never what one move costs, so it
                    // could not have spoken to the 98.8% spin either way. An
                    // A/B on his own display since then — three builds, one
                    // variable each, the bar set before the run — says this
                    // modifier IS the driver: with the scroll removed, 17
                    // thread reopens and 906 diagnostic ticks with zero stalls,
                    // against 4 reopens and a stall on unmodified `main` and
                    // again with the alignment taken off the cap. Alignment is
                    // refuted; this is the one that mattered.
                    //
                    // So the scroll stays — deleting it was the experiment, not
                    // the product — and what changes is *when* it is issued.
                    // `onChange` runs while SwiftUI is mid-update, and a
                    // `scrollTo` from here aims at the last row of a LazyVStack
                    // that is not realised yet: reaching for it realises rows,
                    // which moves the content height, which moves the offset,
                    // which re-scrolls, inside a transaction that never gets
                    // back to the run loop. `ScrollsToNewest` records the
                    // request and issues it a turn later, and collapses a burst
                    // — a stream reconnect replaying a thread — to one scroll
                    // at the newest row instead of one per line. Pinned by
                    // `TheTranscriptScrollLandsAfterTheLayoutItProvokedTests`,
                    // which reads the real scroll offset off-display.
                    //
                    // What one move costs is measured in
                    // `TheConversationCostsTheSameAtFiftyTwoAndAHundredAndTwentyTests`,
                    // over the shipped tree at 608x949: the first new message
                    // realises the screenful at the end of the conversation (14
                    // rows) and every message after it realises exactly one —
                    // the same on a 120-message thread as on a 52-message one.
                    // The scroll really does land off-display, which is what
                    // makes those numbers mean anything: settled, only the
                    // first half of the list is ever measured; after one new
                    // line, only the second half is.
                    scroller.newestRow(is: target) { row in
                        proxy.scrollTo(row, anchor: .bottom)
                    }
                }
            }
        }
    }

    /// One drawable row of the conversation, whatever kind it is. The switch is
    /// exhaustive on purpose: a new kind of row is a compile error here rather
    /// than one that quietly never draws.
    @ViewBuilder
    private func itemView(
        _ item: TranscriptItem, isLast: Bool, follow: @escaping () -> Void
    ) -> some View {
        // The newest row carries the working character: under it, or — when
        // it is the desk's own face — as it. Part of that row rather than a row
        // of its own, so the scroll to the newest row lands on the character.
        if isLast, let live {
            if case .turnFace(let face) = item {
                LiveTurnFaceRow(face: face, live: live)
            } else {
                VStack(alignment: .leading, spacing: TranscriptMetrics.rowSpacing) {
                    rowView(item)
                    WorkingSlot(live: live, follow: follow)
                }
            }
        } else {
            rowView(item)
        }
    }

    @ViewBuilder
    private func rowView(_ item: TranscriptItem) -> some View {
        switch item {
        case .dayBreak(let stamp):
            DayBreakRow(stamp: stamp)
        case .attribution(let line):
            AttributionRow(line: line, openPeerThread: openPeerThread)
        case .rollup(let rollup):
            TrafficRollupRow(rollup: rollup, isOpen: opened.contains(rollup.id)) {
                if opened.contains(rollup.id) {
                    opened.remove(rollup.id)
                } else {
                    opened.insert(rollup.id)
                }
            }
        case .entry(let entry):
            entryView(entry)
        case .turnFace(let face):
            TurnFaceRow(face: face)
        case .caption(let caption):
            SystemCaptionRow(caption: caption, isOpen: opened.contains(caption.id)) {
                if opened.contains(caption.id) {
                    opened.remove(caption.id)
                } else {
                    opened.insert(caption.id)
                }
            }
        case .newDivider:
            NewDividerRow()
        }
    }

    @ViewBuilder
    private func entryView(_ entry: ThreadEntry) -> some View {
        switch entry {
        case .said(let row):
            TranscriptRowView(row: row, openPeerThread: openPeerThread, decide: decide,
                              reply: reply, select: select, openQuote: openQuote,
                              isSelected: row.id == selectedMessageID)
        case .toolCall(let card):
            HStack {
                ApprovalCardView(card: card) { option in answer(card, option) }
                Spacer(minLength: 0)
            }
        }
    }

    @ViewBuilder
    private var approvalFootnotes: some View {
        VStack(alignment: .leading, spacing: 10) {
            // Neither of these is decoration: one means a request exists that
            // this window cannot show, the other means an answer did not land.
            if let notice {
                FlatLabel(notice, systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let problem {
                FlatLabel(problem, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.top, 4)
    }
}

// MARK: - the rows that carry the chain of command

/// "Thu, Sep 3 at 7:10 AM", centred between two days of conversation.
///
/// A heading to a screen reader, because that is what it is: everything under
/// it happened on that day, and rotor navigation by heading is how somebody
/// reading this with VoiceOver gets to yesterday without arrowing through it.
/// The top of a conversation that goes back further than what is held.
struct EarlierMessagesRow: View {
    let loading: Bool
    let load: () -> Void

    var body: some View {
        Button(action: load) {
            Text(loading ? "Loading earlier messages…" : "Show earlier messages")
                .font(.callout)
        }
        .buttonStyle(.link)
        .disabled(loading)
        .frame(maxWidth: .infinity)
        .padding(.vertical, 6)
        .accessibilityIdentifier("transcript.showEarlier")
    }
}

struct DayBreakRow: View {
    let stamp: DayBreak

    var body: some View {
        HStack(spacing: 10) {
            line
            Text(stamp.text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            line
        }
        .padding(.vertical, 6)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(stamp.text)
        .accessibilityAddTraits(.isHeader)
    }

    private var line: some View {
        Rectangle()
            .fill(.quaternary)
            .frame(height: 1)
    }
}

/// "Messaged Initech" / "Message from Initech UX", centred above the line
/// it is about, with that desk's own face beside it.
///
/// This is how he manages a chain of command without reading the chain: the
/// sentence says which way the traffic went, and the face is the same face the
/// sidebar draws for that desk.
struct AttributionRow: View {
    let line: AttributionLine
    let openPeerThread: (String) -> Void

    var body: some View {
        HStack {
            Spacer(minLength: 0)
            if let peerThreadID = line.peerThreadID {
                Button { openPeerThread(peerThreadID) } label: { sentence }
                    .buttonStyle(.plain)
                    .accessibilityLabel(line.text)
                    .accessibilityHint("Opens the full conversation this line came from")
            } else {
                sentence
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(line.text)
            }
            Spacer(minLength: 0)
        }
        .padding(.top, 4)
    }

    private var sentence: some View {
        HStack(spacing: 5) {
            Text(line.text)
                .font(.caption)
                .foregroundStyle(.secondary)
            TinyFace(desk: line.desk)
        }
        .fixedSize(horizontal: false, vertical: true)
    }
}

/// "41 messages with 3 Bots" — a run of agent-to-agent traffic standing in for
/// itself until he asks to see it.
///
/// **He manages outcomes and must never scroll machine chatter.** The row is a
/// real `Button`, so it is reachable by keyboard with the system's own focus
/// ring, and it says on its face whether it is open — a disclosure that only
/// looked different would be invisible to the one person who most needs to
/// know whether there is more underneath.
struct TrafficRollupRow: View {
    let rollup: TrafficRollup
    let isOpen: Bool
    let toggle: () -> Void

    var body: some View {
        HStack {
            Spacer(minLength: 0)
            Button(action: toggle) {
                HStack(spacing: 5) {
                    Text(rollup.text)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                    ForEach(rollup.faces.prefix(3), id: \.self) { desk in
                        TinyFace(desk: desk)
                    }
                    Image(systemName: isOpen ? "chevron.up" : "chevron.down")
                        .font(.system(size: 8, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(.quinary, in: Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(rollup.text)
            .accessibilityHint(isOpen ? "Hides these messages" : "Shows these messages")
            .accessibilityAddTraits(isOpen ? [.isButton, .isSelected] : .isButton)
            Spacer(minLength: 0)
        }
        .padding(.vertical, 4)
    }
}

/// The small round face under the last bubble of a desk's turn, so two
/// consecutive turns from different desks are tellable apart.
///
/// Hidden from a screen reader: every bubble above it already says who spoke,
/// and a face announced after each one would be pure repetition.
struct TurnFaceRow: View {
    let face: TurnFace

    var body: some View {
        HStack {
            TinyFace(desk: face.desk, size: 20)
            Spacer(minLength: 0)
        }
        .accessibilityHidden(true)
    }
}

/// A desk's face at the size a sentence can carry.
///
/// The look is derived here rather than handed in because the conversation's
/// rows carry wire names and not roster rows — but it is one FNV-1a hash over a
/// short name, on a handful of rows, inside a view SwiftUI skips whole unless
/// the conversation itself changed. See `TranscriptList`'s equality.
struct TinyFace: View {
    let desk: String
    var size: CGFloat = 14

    var body: some View {
        AvatarView(
            look: AvatarLook.forName(desk),
            attention: .quiet,
            isDimmed: false,
            localAvatarURL: nil,
            size: size)
    }
}

// MARK: - The reference's thread furniture (D2)

/// "Routine · Morning Shorts report" — centred, grey, one line; a click shows
/// the whole of what was sent. A system line is never his bubble.
struct SystemCaptionRow: View {
    let caption: SystemCaption
    let isOpen: Bool
    let toggle: () -> Void

    var body: some View {
        VStack(spacing: 4) {
            Button(action: toggle) {
                Text(caption.text)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            .buttonStyle(.plain)
            .help(caption.fullText)
            .accessibilityLabel(caption.text)
            .accessibilityHint(isOpen ? "Hides the full text" : "Shows the full text")
            if isOpen {
                Text(caption.fullText)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 24)
        .padding(.vertical, 2)
    }
}

/// `NEW`, a thin rule either side, above the first line he had not read.
struct NewDividerRow: View {
    var body: some View {
        HStack(spacing: 10) {
            rule
            Text("NEW")
                .font(.caption2.weight(.semibold))
                .foregroundStyle(DeckPalette.waiting)
            rule
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("New messages")
    }

    private var rule: some View {
        Rectangle().fill(DeckPalette.waiting.opacity(0.6)).frame(height: 1)
    }
}

/// What the transcript draws live without being rebuilt: the working desk's
/// character. Set by the thread pane; observed only by the two rows below.
public final class TranscriptLive: ObservableObject {
    @Published public private(set) var working: AvatarLook?

    public init() {}

    public func set(working look: AvatarLook?) {
        if working != look { working = look }
    }
}

/// The desk's character under the last line while it works — the reference's "on it".
/// Zero height otherwise. Only the eyes move; the size never does, so the list
/// is not re-measured.
struct WorkingSlot: View {
    @ObservedObject var live: TranscriptLive
    var follow: () -> Void = {}

    var body: some View {
        if let look = live.working {
            AvatarView(look: look, attention: .working, isDimmed: false, localAvatarURL: nil, size: 22)
                // Room for the online dot, which sits just outside the face.
                .padding(.bottom, 4)
                .onAppear(perform: follow)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel("Working")
        }
    }
}

/// The newest turn's face: still when the desk is quiet, the working character
/// (same size, eyes moving) while it works.
struct LiveTurnFaceRow: View {
    let face: TurnFace
    @ObservedObject var live: TranscriptLive

    var body: some View {
        if let look = live.working {
            HStack {
                AvatarView(look: look, attention: .working, isDimmed: false, localAvatarURL: nil, size: 20)
                Spacer(minLength: 0)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Working")
        } else {
            TurnFaceRow(face: face)
        }
    }
}

/// Where the bottom of the conversation is. Written by a row as it is
/// realised; read only by `JumpToNewestButton`.
final class BottomTracker: ObservableObject {
    @Published private(set) var isAtBottom = true

    /// Publishes only a change: an appear that repeats the known answer must
    /// not redraw even the button.
    func set(_ value: Bool) {
        if isAtBottom != value { isAtBottom = value }
    }
}

/// ↓ — a round button over the bottom of the conversation, shown only when the
/// newest line is off screen.
struct JumpToNewestButton: View {
    @ObservedObject var tracker: BottomTracker
    let jump: () -> Void

    var body: some View {
        if !tracker.isAtBottom {
            Button(action: jump) {
                Image(systemName: "arrow.down")
                    .font(.system(size: 13, weight: .semibold))
                    .frame(width: 32, height: 32)
                    .background(.regularMaterial, in: Circle())
                    .overlay(Circle().strokeBorder(.quaternary))
            }
            .buttonStyle(.plain)
            .padding(.bottom, 10)
            .help("Jump to the newest message")
            .accessibilityLabel("Jump to the newest message")
        }
    }
}

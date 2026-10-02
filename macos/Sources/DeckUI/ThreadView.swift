import SwiftUI
import DeckKit

/// The conversation pane: header, transcript, and either a composer or the
/// view-only footer. Which of those two appears is decided in DeckKit, off the
/// thread payload's flag, and switched on here.
public struct ThreadView: View {
    @ObservedObject var store: DeckStore
    @FocusState private var composerFocused: Bool
    /// ⌘V of a picture or a file attaches it (`ComposerAttachments.swift`).
    @State private var pasteMonitor = ComposerPasteMonitor()
    /// The working character, handed to the transcript as an object so a desk
    /// starting work never rebuilds the conversation.
    @StateObject private var live = TranscriptLive()
    /// C2: hold-to-talk and calls — the app's one session, so a call outlives
    /// the thread it started in. The pane never observes the session itself.
    private let voice = VoiceDock.shared

    public init(store: DeckStore) {
        self.store = store
    }

    public var body: some View {
        // Free unless DECK_DIAGNOSE=1. If this runs thousands of times a second
        // while store.willChange stays near zero, SwiftUI is driving itself and
        // nothing this app publishes is involved.
        let _ = Diagnostics.count("thread.body")
        VStack(spacing: 0) {
            // A call with another desk (or with no thread open) stays in sight.
            CallPillView(session: voice.session, agents: store.agents) { store.openDesk(named: $0) }
            ListenStrip(player: voice.listen)
            // An unsaved "+" thread owns the pane while it exists: he is mid-
            // sentence with something that has no desk yet, and the roster's
            // selection must not draw over it.
            if let pending = store.pending {
                PendingThreadView(store: store, pending: pending)
            } else {
                resolved
            }
        }
    }

    @ViewBuilder
    private var resolved: some View {
        switch store.thread {
        case .loading:
            // Words, not a spinner. An indeterminate `ProgressView` is an
            // AppKit animation that holds a dispatch worker for as long as it
            // runs — measured: ten opened off-screen left thirteen threads
            // behind — and the thread opens in well under a second anyway.
            Text("Opening thread…")
                .font(.callout)
                .foregroundStyle(.secondary)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        case .empty:
            ContentUnavailableView(
                "No conversation open",
                systemImage: "bubble.left.and.bubble.right",
                description: Text("Pick an agent on the left to see its thread.")
            )
        case .failed(let error):
            // The same screen the sidebar draws, from the same decision. When
            // the roster fails there is one condition, and the two panes used
            // to name it differently — "Can't reach the deck" beside a spinner.
            FailureView(failure: FailurePresentation.make(error)) {
                if let threadID = store.selectedThreadID {
                    store.open(threadID: threadID)
                } else {
                    Task { await store.loadRoster() }
                }
            }
        case .loaded(let screen):
            loaded(screen)
        }
    }

    private func loaded(_ screen: ThreadScreen) -> some View {
        VStack(spacing: 0) {
            // Who he is talking to and whether the stream is up. Both used to
            // be written into the window's chrome from this body; see
            // `ThreadHeader` for the minute that measured what that cost.
            // The phone's header: the face, the name, one status line, and
            // the screen, spoken-replies and call buttons.
            HStack(spacing: 8) {
                ThreadHeader(
                    title: headerAgent(screen)?.displayName ?? screen.presentation.headerTitle,
                    subtitle: screen.presentation.headerSubtitle,
                    connection: screen.connection,
                    look: headerAgent(screen)?.look,
                    attention: headerAgent(screen).map(RowAttention.init(for:)) ?? .quiet,
                    agentState: headerAgent(screen)?.state.label ?? "",
                    summary: headerAgent(screen)?.summaryLine
                )
                headerButtons(screen)
            }
            .padding(.trailing, 12)
            .background(DeckPalette.canvas)
            Divider()
            CallBarView(session: voice.session, agent: store.selectedAgentName.flatMap { store.agents[$0] })
            // Said once, right here, because this is the thread he is already
            // looking at: the interview that just opened it could not
            // pre-accept Claude Code's trust dialog, so its window is sitting
            // on one now.
            if let warning = store.pretrustWarning {
                PretrustWarningBanner(text: warning)
                Divider()
            }
            // No thread switch over the messages (owner, 2026-09-30: a stock
            // segmented control floated over the transcript and cut it off).
            // As on the phone, the Direct thread is the view: desk-to-desk
            // traffic is the inline "N messages with X" chip, and a relayed
            // line's "from X ›" opens that pair's thread when he wants it all.
            transcript(screen)
            // **The desk's sign-in card, in its own thread** — the same card,
            // the same buttons, the same order as the side pane draws, because
            // it IS the side pane's view. Owner: "Don't have the login on the
            // screen, only on the side?"
            ForEach(store.threadSignIns) { item in
                Divider()
                SignInCardView(
                    item: item,
                    signIn: store.macSignIns[item.askID],
                    canSignInOnMac: store.signIn != nil,
                    macBrowserLabel: store.macBrowserLabel,
                    canTakeOver: store.canTakeOverScreens,
                    showsOpenDesk: false,
                    answer: { item, action in Task { await store.answer(item: item, with: action) } },
                    signInVerb: { item, verb in Task { await store.signIn(item, verb) } },
                    takeOver: { store.takeOver(desk: $0, focus: .screen) })
                .equatable()
                .padding(12)
                .background(Color.orange.opacity(0.14))
            }
            Divider()
            // Directly above the box he types in, because that is where he is
            // looking when he wants to know whether anything is happening. He
            // could not tell "it is working" from "it is stuck" from "it is
            // done" without leaving the conversation to go and find out.
            // Only what he acts on, what is broken, or a desk that is asleep.
            // A quiet desk says nothing, and a working one is the character
            // under its last line.
            if let status = store.deskStatus, status.showsOnConversation {
                DeskStatusStrip(
                    status: status,
                    offers: store.takeoverOffers,
                    takeOver: { store.takeOver(focus: $0) })
                Divider()
            }
            VoiceComposerLine(session: voice.session)
            VoiceNoteNotice(composer: voice.notes)
            switch screen.presentation.composer {
            case .enabled(let placeholder):
                VoiceNoteSlot(composer: voice.notes) {
                    composer(placeholder: placeholder)
                }
            case .viewOnly(let footer, let closeTitle):
                viewOnlyFooter(footer, closeTitle: closeTitle)
            }
        }
        .background(DeckPalette.canvas)
        .voiceFeed(voice.session, store: store)
        .onAppear {
            wireVoiceNotes()
            pasteMonitor.install(isComposing: { composerFocused }, attach: { store.attach($0) })
        }
        .onDisappear { pasteMonitor.remove() }
        // A picture or a file dropped on the conversation or the composer.
        .acceptsAttachmentDrops { store.attach($0) }
        .environment(\.attachmentFetch, store.attachmentFetch)
        .environment(\.mediaLoader, store.mediaLoader)
        // **Nothing here touches the window.** The detail column used to set
        // `.navigationSubtitle` and hang a `ToolbarItem` off `screen.connection`
        // — an NSTextField and an NSControl written from a SwiftUI body, which
        // is the loop that took a whole core within 45 seconds of launch and
        // starved the app's own networking while it ran. Both are drawn in the
        // pane now, by `ThreadHeader`.
        //
        // The window title is still the deck's identity and still set exactly
        // once, in `DeckApp`, from a launch-time constant. It was never the
        // problem: what costs is a body evaluation writing chrome, not chrome
        // that was written at startup.
        // **No `.task(id: selectedAgentName)` here any more.** That is what
        // fetched `/v1/approvals`, and it runs once per *selection* — so a tool
        // call raised while he sat reading could not appear until he clicked
        // another desk and came back, which is the "reopen the chat" he
        // reported. The store polls it for as long as a conversation is open;
        // the view no longer decides when the deck is asked.
    }

    /// The desk a direct thread is with; `nil` on a peer thread, which is
    /// two desks and draws no single face.
    private func headerAgent(_ screen: ThreadScreen) -> Agent? {
        ThreadID.desk(ofDirect: screen.presentation.threadID).flatMap { store.agents[$0] }
    }

    /// The phone's header buttons: the desk's screen, its terminal (the
    /// Mac's), spoken replies, and the call. A button is only drawn when the
    /// deck can serve it (`takeoverOffers`), so none leads nowhere.
    @ViewBuilder
    private func headerButtons(_ screen: ThreadScreen) -> some View {
        let offers = store.takeoverOffers
        if let offer = offers.first(where: { $0.focus == .screen }) {
            headerButton("display", help: offer.hint, label: "\(screen.presentation.headerTitle)'s screen") {
                store.takeOver(focus: .screen)
            }
        }
        if let offer = offers.first(where: { $0.focus == .terminal }) {
            headerButton("terminal", help: offer.hint, label: "\(screen.presentation.headerTitle)'s terminal") {
                store.takeOver(focus: .terminal)
            }
        }
        SpokenRepliesToggle(name: screen.presentation.headerTitle)
        CallButton(session: voice.session)
    }

    private func headerButton(_ glyph: String, help: String, label: String,
                              action: @escaping () -> Void) -> some View {
        Button {
            DeckHaptics.tap()
            action()
        } label: {
            Image(systemName: glyph)
                .font(.system(size: 13, weight: .medium))
                .frame(width: 30, height: 30)
                .background(.quaternary, in: Circle())
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(label)
    }

    /// The conversation itself, handed over as **values**.
    ///
    /// `TranscriptList` is `Equatable`, so SwiftUI skips it entirely when the
    /// conversation has not changed — and one store drives both the sidebar and
    /// this pane, so it is asked to redraw constantly for reasons that have
    /// nothing to do with what was said. See `ListChurnTests`.
    private func transcript(_ screen: ThreadScreen) -> some View {
        TranscriptList(
            entries: screen.entries,
            // The desk this page belongs to. §6.2's own rule needs it to turn a
            // `peer:` id into the *other* desk's wire name, which is what every
            // face in the transcript is derived from.
            desk: ThreadID.desk(ofDirect: screen.presentation.threadID)
                ?? store.selectedAgentName ?? "",
            notice: store.approvalNotice,
            problem: store.approvalProblem,
            openPeerThread: { store.open(threadID: $0) },
            answer: { card, option in Task { await store.answer(card: card, with: option) } },
            newFrom: screen.newFrom,
            live: live,
            decide: { decision, value in Task { await store.answer(decision: decision, with: value) } },
            selectedMessageID: store.selectedMessageID,
            jump: store.quoteJump,
            reply: { store.reply(to: $0) },
            select: { store.selectedMessageID = $0 },
            openQuote: { store.jump(to: $0) },
            hasEarlier: screen.hasEarlier,
            loadingEarlier: store.isLoadingEarlier,
            loadEarlier: { store.loadEarlier() }
        )
        .equatable()
        .onChange(of: store.workingLook, initial: true) { _, look in live.set(working: look) }
    }

    /// **The composer pill: [+] · "Message <Name>" · the accessory slot.**
    ///
    /// The box grows to `ComposerBox.maximumLines` and then scrolls inside
    /// itself. **Return is bound exactly once**, by `.onSubmit` on the field;
    /// Shift-Return and Option-Return make a new line. The rule is no longer
    /// printed under the box (he asked for it gone) — it is the field's
    /// tooltip and its VoiceOver hint, `ComposerBox.sendHint`.
    private func composer(placeholder: String) -> some View {
        VStack(spacing: 0) {
        if let target = store.replyDraft.target {
            ReplyStrip(target: target) { store.cancelReply() }
        }
        if let tray = store.composerTray { ComposerTrayView(tray: tray) }
        HStack(alignment: .bottom, spacing: 8) {
            Button(action: attachFile) {
                Image(systemName: "plus")
                    .font(.system(size: 14, weight: .medium))
                    .frame(width: 32, height: 32)
                    .background(.quaternary, in: Circle())
            }
            .buttonStyle(.plain)
            .help("Attach files — or paste or drop a screenshot")
            .accessibilityLabel("Add a file")

            TextField(placeholder, text: $store.composerDraft, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(ComposerBox.minimumLines...ComposerBox.maximumLines)
                .padding(.vertical, 8)
                .padding(.horizontal, 14)
                .frame(minWidth: 0, maxWidth: .infinity, alignment: .leading)
                .background(ThreadColors.composer,
                            in: RoundedRectangle(cornerRadius: CGFloat(DeckTokens.fieldRadius), style: .continuous))
                .focused($composerFocused)
                .onSubmit(submit)
                .help(ComposerBox.sendHint)
                .accessibilityLabel(placeholder)
                .accessibilityHint(ComposerBox.sendHint)

            // The phone's rule (`ComposerMode`): his voice while the box is
            // empty, the send arrow as soon as there are words.
            // An attachment counts as words; send waits for every upload.
            switch store.composerTray.map({ !$0.isEmpty }) == true
                ? ComposerMode.send(enabled: store.composerCanSend)
                : ComposerMode.make(draft: store.composerDraft, isReadOnly: false,
                                    canRecord: true, isSending: store.draftIsSending) {
            case .record:
                composerAccessory
            case .send(let enabled):
                Button(action: submit) {
                    Image(systemName: store.draftIsSending ? "ellipsis" : "arrow.up")
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(DeckPalette.canvas)
                        .frame(width: 32, height: 32)
                        .background(Circle().fill(enabled ? DeckPalette.ink : Color.secondary.opacity(0.5)))
                }
                .buttonStyle(.plain)
                .disabled(!enabled)
                .accessibilityLabel("Send message")
            case .viewOnly:
                EmptyView()
            }
        }
        .padding(.horizontal, 12)
        .padding(.top, 8)
        .padding(.bottom, 10)
        }
        .background(DeckPalette.canvas)
        // ⌘R: reply to the selected message, or the desk's newest. A button
        // nobody sees, because a shortcut needs one in the hierarchy.
        .background {
            Button("Reply") { store.replyToSelected() }
                .keyboardShortcut("r", modifiers: .command)
                .buttonStyle(.plain)
                .opacity(0)
                .frame(width: 0, height: 0)
                .accessibilityHidden(true)
        }
        // Starting a reply puts him in the box, ready to type.
        .onChange(of: store.replyDraft.target?.messageID) { _, id in
            if id != nil { composerFocused = true }
        }
        .overlay(alignment: .top) {
            if let sendError = store.sendError ?? store.decisionProblem {
                Text(sendError)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .padding(.top, 2)
            }
        }
    }

    /// **The mic's slot**: one mic, and it records a voice message. It never
    /// starts a call — that is the phone button in the header.
    private var composerAccessory: some View {
        VoiceNoteButton(composer: voice.notes,
                        threadID: store.selectedThreadID.flatMap { ThreadID.desk(ofDirect: $0) != nil ? $0 : nil },
                        name: store.selectedAgentName.flatMap { store.agents[$0]?.displayName } ?? "the agent")
    }

    /// His voice message landed: draw it now, and let the answer be spoken.
    private func wireVoiceNotes() {
        let dock = voice
        dock.notes.onSent = { [weak store] message in
            store?.voiceNoteSent(message)
            guard let desk = ThreadID.desk(ofDirect: message.threadID) else { return }
            dock.replies.arm(desk: desk, after: message.id, voice: store?.agents[desk]?.voice)
        }
    }

    /// [+]: files are uploaded to the deck like a pasted one — a path on this
    /// Mac means nothing to a desk on the box. A folder cannot be uploaded,
    /// so its path still goes into the draft (§9) as before.
    private func attachFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = true
        guard panel.runModal() == .OK else { return }
        var folders: [String] = []
        var files: [PreparedAttachment] = []
        for url in panel.urls {
            if store.composerTray != nil, let file = AttachmentLoading.file(at: url) { files.append(file) }
            else { folders.append(url.path) }
        }
        store.attach(files)
        if !folders.isEmpty {
            let paths = folders.joined(separator: " ")
            let draft = store.composerDraft
            store.composerDraft = draft.isEmpty || draft.hasSuffix(" ") ? draft + paths : draft + " " + paths
        }
        composerFocused = true
    }

    /// The composer is not disabled here — it is absent. There is no text field
    /// on a peer thread to type into, mis-enable, or focus with a shortcut.
    private func viewOnlyFooter(_ footer: String, closeTitle: String) -> some View {
        HStack(spacing: 12) {
            FlatLabel(footer, systemImage: "eye")
                .font(.callout)
                .foregroundStyle(.secondary)
            Spacer()
            Button(closeTitle) { store.closeThread() }
                .buttonStyle(.deckSecondary)
        }
        .padding(12)
        .background(.bar)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(footer)
    }

    /// The draft is the store's, and only a send the deck accepted empties it.
    /// Clearing it here -- before the await -- is what made a refusal look
    /// exactly like a success with his sentence gone.
    private func submit() {
        Task { await store.submitComposer() }
    }
}

/// **Whether anything is happening, on the conversation, all the time.**
///
/// He sent messages into a screen that did not move and concluded the system
/// was broken while it was working. The six conditions he has to tell apart are
/// decided in `DeskStatus`; this only draws the one it was handed.
///
/// Never colour alone: the word is always there and the symbol carries the same
/// meaning again. The headline stays at `.primary` and only the symbol and a
/// faint wash take the tint, so nothing here depends on a contrast ratio a
/// tinted-text banner would have to defend.
///
/// **Nothing here moves.** The first version drew a spinner for the states that
/// "are motion" — working, sending, reconnecting — and left it on the pane
/// permanently. An indeterminate progress indicator is an AppKit animation: it
/// drove the window's display cycle at the refresh rate, and this strip sits in
/// the stack that sizes the transcript's `ScrollView`, so every frame
/// re-measured every row of the conversation. The owner's Mac went to 99.3%
/// CPU and 2.4 GB and the app had to be killed. A still glyph and a word say
/// the same thing for nothing. See `LayoutSettlesTests`.
struct DeskStatusStrip: View {
    let status: DeskStatus
    /// The ways into that desk's own machine, or empty on a deck that cannot
    /// serve one. Drawn **only** while the desk is waiting on him: this strip
    /// is on the conversation permanently, and a control that is always there
    /// is one he stops seeing on the day it matters.
    let offers: [TakeoverOffer]
    let takeOver: (TakeoverOffer.Focus) -> Void

    private var showsOffers: Bool {
        status.tone == .waitingOnYou && !offers.isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            sentence
            // **The way in, where the problem is announced.** He is already
            // looking at this line when a desk stops; before this it told him
            // something was wrong and gave him nothing to do about it, and the
            // only route to that desk's screen was a button that existed while
            // the pointer rested on a thumbnail two columns away.
            //
            // Never opened for him — see `Takeover`. One obvious click.
            if showsOffers {
                actions
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 6)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(tint.opacity(0.11))
        // `.contain`, not `.ignore`: the sentence below still reads as one
        // element, and the buttons stay reachable as their own.
        .accessibilityElement(children: .contain)
        // Deliberately NOT animated. This strip's height is what sizes the
        // transcript's ScrollView, so animating it re-measures every row of the
        // LazyVStack for the length of the animation, on every state change.
    }

    /// What is happening, in one element.
    private var sentence: some View {
        // `.top`, not `.firstTextBaseline`. A baseline-aligned stack holding
        // views that have no text baseline makes SwiftUI resolve a *fallback*
        // baseline through a hidden NSTextField, and it sets that field's font
        // on every pass — which invalidates its intrinsic content size, dirties
        // the window's constraints and schedules the next pass. That chain is
        // in the crash sample verbatim, under FallbackAlignmentProvider.
        HStack(alignment: .top, spacing: 8) {
            mark
                // Nudged onto the headline's optical line, which is all the
                // baseline alignment was ever buying.
                .padding(.top, 1)
            VStack(alignment: .leading, spacing: 1) {
                Text(status.headline)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.primary)
                if let detail = status.detail {
                    Text(detail)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .ignore)
        // One sentence, the way FailureView and the message bubbles read.
        .accessibilityLabel(status.spokenLabel)
        .accessibilityAddTraits(.updatesFrequently)
    }

    /// One button per way in. Words and a still glyph — never colour alone —
    /// and each says what it will do before it is pressed, in the tooltip and
    /// to a screen reader.
    private var actions: some View {
        // `.center`, never a text baseline: an SF Symbol has no line of text
        // to sit on. See the note on `sentence`.
        HStack(alignment: .center, spacing: 8) {
            ForEach(Array(offers.enumerated()), id: \.element.id) { index, offer in
                Button {
                    takeOver(offer.focus)
                } label: {
                    HStack(alignment: .center, spacing: 5) {
                        Image(systemName: offer.symbol)
                        Text(offer.title)
                    }
                }
                // The first is the one he came for; the second is the other
                // half of the same machine and is drawn as the quieter of the
                // two rather than as a second equal choice. `.bordered` for
                // the quiet one rather than a tinted `.borderedProminent`:
                // prominent draws its label white on whatever tint it is
                // given, and a grey fill under white text is a contrast ratio
                // this app would have to defend.
                .modifier(TakeoverButtonLook(isPrimary: index == 0))
                .controlSize(.small)
                .help(offer.hint)
                .accessibilityLabel(offer.title)
                .accessibilityHint(offer.hint)
            }
            Spacer(minLength: 0)
        }
    }

    /// Always a still glyph. `DeskStatus` guarantees there is one for every
    /// condition, so there is no branch here that could reintroduce motion.
    private var mark: some View {
        Image(systemName: status.symbol)
            .font(.caption)
            .foregroundStyle(tint)
    }

    private var tint: Color {
        switch status.tone {
        case .working: return DeckPalette.working
        case .waitingOnYou: return .orange
        case .quiet: return .secondary
        case .offline: return .secondary
        case .disconnected: return .orange
        }
    }
}

/// **The one look every way into a take-over wears**, so the button on the
/// conversation and the button in the inspector are recognisably the same
/// thing. Prominent for the screen, ordinary for the terminal beside it —
/// never a tinted prominent button, whose label is drawn white on whatever
/// tint it is handed.
struct TakeoverButtonLook: ViewModifier {
    let isPrimary: Bool

    func body(content: Content) -> some View {
        if isPrimary {
            content.buttonStyle(.deckPrimary)
        } else {
            content.buttonStyle(.deckSecondary)
        }
    }
}

/// One line of the transcript. Either he is being spoken to, or he is
/// overhearing — §6.2 decides which in DeckKit and this only draws it.
struct TranscriptRowView: View {
    let row: TranscriptRow
    let openPeerThread: (String) -> Void
    /// K4: his answer to a decision card — an option's value or his own words.
    var decide: (Decision, String) -> Void = { _, _ in }
    /// Quote-replies (`QuoteReplyViews`): start one, select for ⌘R, and
    /// follow a quote back to its original.
    var reply: ((Message) -> Void)?
    var select: ((String) -> Void)?
    var openQuote: ((MessageQuote) -> Void)?
    var isSelected = false

    var body: some View {
        if let decision = row.message.decision, !row.attribution.isRelayed {
            DecisionCardView(decision: decision) { decide(decision, $0) }
                .replyable(row.message, isSelected: isSelected, reply: reply, select: select)
        } else if row.attribution.isRelayed {
            RelayedLineView(row: row, openPeerThread: openPeerThread)
                .replyable(row.message, isSelected: isSelected, reply: reply, select: select)
        } else {
            MessageBubble(message: row.message, openQuote: openQuote)
                .replyable(row.message, isSelected: isSelected, reply: reply, select: select)
        }
    }
}

/// A dispatch this desk sent, or the reply that came back.
///
/// Deliberately **not** a bubble. A bubble on this side of the pane is the
/// desk answering the owner, and these are a worker's dense counts and ids —
/// drawing them as speech to him is the failure the relay exists to prevent.
///
/// It reads as secondary through geometry, not through washing the text out:
/// indented under the conversation, narrower, one size down, behind a rule and
/// a faint fill. The words themselves stay at full contrast, because the
/// previous peer view failed by being uniform grey with no author — quiet is
/// not the same as unreadable.
struct RelayedLineView: View {
    let row: TranscriptRow
    let openPeerThread: (String) -> Void

    private var isDispatch: Bool {
        if case .dispatch = row.attribution { return true }
        return false
    }

    var body: some View {
        HStack(alignment: .top, spacing: RelayMetrics.spacing) {
            // The rule is what carries "this is an aside" at a glance, before
            // any word is read.
            Capsule()
                .fill(.tertiary)
                .frame(width: RelayMetrics.rule)

            VStack(alignment: .leading, spacing: 3) {
                attributionChip
                Text(row.message.text)
                    .font(.callout)
                    .foregroundStyle(.primary)
                    .multilineTextAlignment(.leading)
                    .fixedSize(horizontal: false, vertical: true)
                    // A relayed line is an agent's message and can be as long
                    // as one, so it is pinned for the same reason and by the
                    // same rule as the bubble — but this row has the least room
                    // in the app, so it hands over **its own** chrome rather
                    // than the bubble's. See `MessageBubble.spansView`.
                    .pinnedWidthForLongText(row.message.text, chrome: RelayMetrics.chrome)
                    .textSelection(.enabled)
                    // `.accessibilityLabel` alone published *nothing* here:
                    // selectable text is not a plain label-bearing element, so
                    // the chip announced "from Seeker" and the counts after it
                    // were silent. Same shape as MessageBubble — take the
                    // element over, then say the sentence.
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(
                        row.spokenLabel(timestamp: Theme.spokenTimestamp(row.message.sentAt))
                    )
                    .accessibilityTextContentType(.narrative)
            }
            Spacer(minLength: RelayMetrics.gutter)
        }
        .padding(.vertical, 6)
        .padding(.horizontal, RelayMetrics.padding)
        .background(.quinary, in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        .padding(.leading, RelayMetrics.indent)
        .padding(.trailing, RelayMetrics.trailingInset)
        .accessibilityElement(children: .contain)
    }

    /// The label in front, and the way in. It is a real button so it is
    /// reachable by keyboard with the system's own focus ring; the message
    /// text beside it stays selectable, because dense counts are made to be
    /// copied.
    @ViewBuilder
    private var attributionChip: some View {
        let name = row.attribution.label ?? ""
        if let peerThreadID = row.attribution.peerThreadID {
            Button {
                openPeerThread(peerThreadID)
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: isDispatch ? "arrow.turn.up.right" : "arrow.turn.down.left")
                    Text(name)
                    Image(systemName: "chevron.right")
                        .font(.system(size: 8, weight: .semibold))
                }
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .accessibilityLabel(name)
            .accessibilityHint("Opens the full conversation this line came from")
        }
    }
}

/// **A relayed line's own geometry, in one place, because its width is worked
/// out from it.**
///
/// `ours-04-clipping.jpg` is what a guessed width looks like: the pinned text
/// was sized against a single global gutter that knew nothing about the row it
/// was applied to, and this row — indented, ruled, inset and the narrowest in
/// the app — came out 50pt wider than the column at every width from 380 to
/// 640pt. A vertical `ScrollView` does not scroll sideways, so those 50pt of
/// what his desk said were not off screen, they were gone.
///
/// So the numbers the layout uses and the number the width is derived from are
/// now **the same numbers**. Moving the indent without moving the width is no
/// longer possible.
enum RelayMetrics {
    static let indent: CGFloat = 28
    static let trailingInset: CGFloat = 48
    static let padding: CGFloat = 10
    static let rule: CGFloat = 2
    static let spacing: CGFloat = 8
    /// Keeps the line short of the pane's right-hand edge even when the text
    /// is not pinned at all.
    static let gutter: CGFloat = 24

    /// Everything either side of the words. Two `spacing` gaps, because the
    /// `HStack` puts one before the text and one after it.
    static var chrome: CGFloat {
        TranscriptMetrics.horizontalPadding * 2
            + indent + trailingInset + padding * 2 + rule + spacing * 2 + gutter
    }
}

/// The same, for a bubble: its own padding either side, and the minimum gap it
/// keeps from the opposite margin so a reply never reads as full-bleed.
enum BubbleMetrics {
    static let padding: CGFloat = 11
    static let oppositeGutter: CGFloat = 60

    static var chrome: CGFloat {
        TranscriptMetrics.horizontalPadding * 2 + padding * 2 + oppositeGutter
    }
}

/// The transcript's own inset, both sides. It is here rather than inline in
/// `TranscriptList` because every row's width is derived from it.
enum TranscriptMetrics {
    static let horizontalPadding: CGFloat = 16
    static let verticalPadding: CGFloat = 14
    static let rowSpacing: CGFloat = 8
}

struct MessageBubble: View {
    let message: Message
    /// Only a test opens a bubble already expanded; on screen he presses the
    /// button. It is an argument rather than a hook so the collapsed and
    /// expanded heights can both be measured — see `LongMessageLayoutTests`.
    var startExpanded = false
    /// A tap on the quote this message carries, when it is a reply.
    var openQuote: ((MessageQuote) -> Void)?

    var body: some View {
        HStack {
            if message.isFromUser { Spacer(minLength: BubbleMetrics.oppositeGutter) }
            VStack(alignment: message.isFromUser ? .trailing : .leading, spacing: 6) {
                if let quote = message.replyTo {
                    QuoteBlock(quote: quote, open: openQuote)
                }
                if message.channel == .voice {
                    // Said on a live call, not typed.
                    Image(systemName: "mic.fill")
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Said on a call")
                }
                if let note = message.voiceNote {
                    // His recorded voice message; the text below is what the
                    // deck heard.
                    VoiceNotePlayButton(player: VoiceDock.shared.notePlayer, note: note)
                }
                // His pictures from the deck, and the lines that named them
                // taken out of the words (`AttachmentLines`).
                ForEach(AttachmentLines.sentImages(message), id: \.value) { sent in
                    if let url = sent.url { SentImage(url: url) }
                }
                // A video, audio clip, PDF or other file the deck holds: a
                // player or a chip that opens it (`HeldAttachmentViews`).
                ForEach(AttachmentLines.held(message), id: \.value) { held in
                    HeldAttachmentView(attachment: held)
                }
                if !AttachmentLines.visibleText(message).isEmpty {
                    LongTextBody(text: AttachmentLines.visibleText(message), startExpanded: startExpanded) { shown, collapsed in
                        spansView(of: shown, collapsed: collapsed)
                    }
                }
                // The deck derives attachments from the text and serves no
                // files, so only an image this Mac can actually open is drawn.
                // Anything else stays as the path it already is in the text.
                ForEach(message.attachments.compactMap(\.localImageURL), id: \.self) { url in
                    AsyncImage(url: url) { image in
                        image.resizable().scaledToFit()
                    } placeholder: {
                        RoundedRectangle(cornerRadius: 8)
                            .fill(.quaternary)
                            .frame(height: 120)
                    }
                    .frame(maxWidth: 320)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .accessibilityLabel("Image attached to message")
                }
            }
            .padding(.horizontal, BubbleMetrics.padding)
            .padding(.vertical, 8)
            .background(message.isFromUser ? ThreadColors.ownerBubble : ThreadColors.agentBubble)
            .foregroundStyle(Color.primary)
            .clipShape(RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous))
            .textSelection(.enabled)
            .modifier(ListenOnBubble(player: VoiceDock.shared.listen, message: message))
            if !message.isFromUser { Spacer(minLength: BubbleMetrics.oppositeGutter) }
        }
        // `.contain`, not `.ignore`. Ignoring the children spoke the whole
        // message in one element and hid everything inside it — which was
        // right when a bubble was only text, and would now make "Show full
        // message" unreachable to the one person who most needs a way to get
        // at the rest of it.
        .accessibilityElement(children: .contain)
        // The sentence is DeckKit's and it leads with the message itself. It is
        // built from the FULL text whether the bubble is collapsed or not: what
        // a screen reader hears does not change because of a layout decision.
        .accessibilityLabel(message.spokenLabel(timestamp: Theme.spokenTimestamp(message.sentAt)))
        .accessibilityTextContentType(.narrative)
    }


    /// **What the desk wrote, rendered.** Agents write Markdown — his own
    /// working agreement tells every one of them to bold the one thing that
    /// matters — and this pane used to draw the asterisks.
    ///
    /// Takes the string to draw rather than reading `message.text`, because a
    /// collapsed bubble draws its preview and an expanded one draws the lot.
    /// The preview is balanced first, so a cut inside `**` cannot emphasise
    /// everything under it; see `MarkdownBody.balanced`.
    private func spansView(of shown: String, collapsed: Bool) -> some View {
        MarkdownText(
            text: shown, asSinglePassage: collapsed,
            // Both bubbles are grey now, so code is tinted in both.
            tintsCode: true)
            .pinnedWidthForLongText(shown, chrome: BubbleMetrics.chrome)
    }
}

/// **Why a long message gets a definite width: "when i get to this long
/// message its stuck".**
///
/// A wrapping `Text` has to lay the whole string out to answer "how big are
/// you", and what that costs depends entirely on *which* size it is asked for.
/// Measured headless, one query on the real bubble:
///
/// ```
///  chars | ideal width, fixedSize | ideal, none | proposed 900pt, fixedSize | proposed, none
///  2,000 |                 4.74ms |      0.45ms |                    0.61ms |         1.25ms
///  8,000 |                24.20ms |      0.21ms |                    0.62ms |        10.58ms
/// 20,000 |                38.77ms |      0.19ms |                    0.70ms |         6.31ms
/// 50,000 |               115.22ms |      0.18ms |                    0.69ms |        34.82ms
/// ```
///
/// **Neither column is safe.** Dropping `fixedSize` makes the ideal query free
/// and the *real* one — the one every actual layout pass makes — cost up to
/// 35ms. Keeping it does the opposite. The first attempt at this fix removed
/// `fixedSize`, which would have moved the stall rather than ended it.
///
/// A **definite** width is fast under both, and flat in length: 0.61ms at
/// 8,000 characters and 0.69ms at 50,000. `maxWidth` is not enough — measured
/// at 23.70ms and 116.65ms — because the proposal has to be a number before
/// the text engine will stop deriving the wrap from scratch.
///
/// `containerRelativeFrame` gets that number from the enclosing `ScrollView`,
/// so it tracks a resized window with no `GeometryReader` and no state to
/// write back during layout.
///
/// **The number subtracted from it is the caller's, not a constant here, and
/// that is what `ours-04-clipping.jpg` cost.** A single global gutter of 120pt
/// was right for a bubble and 50pt short for a relayed line, which is indented,
/// ruled and inset — so from a 380pt column that row asked for 260pt of text
/// inside 348pt of room it needed 210pt for, and the overflow ran under the
/// inspector and off a view that only scrolls sideways in the sense that it
/// does not. `RelayMetrics.chrome` and `BubbleMetrics.chrome` are each built
/// out of the very paddings their own view applies, so a row cannot be
/// re-inset without its width following.
///
/// **It is applied only above `pinAboveCharacters`, and that costs nothing
/// visually:** a message that long already spans the whole bubble at any pane
/// width, so pinning it changes no pixel. Below it, the natural content width
/// is what makes a two-word reply look like a two-word reply, and the query is
/// ~1ms either way.
///
/// Heights are identical in every variant measured — 48 / 496 / 1952 / 4912 /
/// 12272pt — so **none of this drops a line of what he pasted**, which is the
/// property `LongMessageLayoutTests` asserts alongside the cost.
extension View {
    /// - Parameter chrome: everything this row puts either side of its words —
    ///   its own paddings, its indent, its rule, and the transcript's inset.
    ///   The caller owns it because only the caller knows it.
    @ViewBuilder
    func pinnedWidthForLongText(_ text: String, chrome: CGFloat) -> some View {
        // `utf8.count` is O(1) on a native Swift string; `count` is O(n) and
        // this runs in a body. Bytes rather than characters only shifts the
        // threshold for non-ASCII, and it shifts it towards pinning — the
        // cheap side.
        if text.utf8.count > BubbleWidth.pinAboveCharacters {
            containerRelativeFrame(.horizontal, alignment: .leading) { available, _ in
                // `max(_, minimum)` so a column dragged narrower than one row's
                // chrome asks for a positive width rather than a negative one.
                // A negative proposal is not a narrow layout, it is an
                // undefined one, and undefined is where the placement pass
                // stops converging.
                max(min(available - chrome, BubbleWidth.maximum), BubbleWidth.minimum)
            }
        } else {
            // Untouched, so a short reply keeps the content width that makes it
            // look like a short reply.
            self
        }
    }
}

enum BubbleWidth {
    /// Above this, a message already fills the bubble on any pane, so pinning
    /// its width is invisible — and it is where an unpinned width starts
    /// costing tens of milliseconds per layout query.
    static let pinAboveCharacters = 200
    /// A line of prose stops being readable long before it stops fitting.
    static let maximum: CGFloat = 560
    /// Narrower than this and the words are unreadable anyway; it exists so
    /// the proposal is always a definite positive number.
    static let minimum: CGFloat = 80
}

/// The pretrust sentence, at the top of the very thread it is about — not a
/// toast that has already gone by the time he looks up, and not a fact
/// waiting in the settings panel for him to go find.
struct PretrustWarningBanner: View {
    let text: String

    var body: some View {
        FlatLabel(text, systemImage: "exclamationmark.triangle.fill")
            .font(.callout)
            .foregroundStyle(.orange)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(Color.orange.opacity(0.12))
            .accessibilityElement(children: .combine)
    }
}

/// **The thread's colours are the shared palette** — his bubble a lighter
/// grey on the right, the desk's a darker grey on the left, never an
/// accent-blue bubble. The same greys the iPhone draws (`DeckPalette`,
/// `ThemeParityTests`); the Mac used to keep its own, a shade apart.
enum ThreadColors {
    static let ownerBubble = DeckPalette.ownerBubble
    static let agentBubble = DeckPalette.agentBubble
    static let composer = DeckPalette.field
}

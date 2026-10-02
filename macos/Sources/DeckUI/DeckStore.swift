import Foundation
import SwiftUI
import AppKit
import Combine
import DeckKit

/// Four states, every async surface. An empty roster and a failed one must
/// never look the same, and neither may look like a half-drawn list.
public enum LoadState<Value>: Sendable where Value: Sendable {
    case loading
    case empty
    case failed(DeckError)
    case loaded(Value)
}

/// So a publisher can refuse to publish a state it is already in. `@Published`
/// fires on every `set`, equal or not, and one of those sets rebuilds the whole
/// conversation — see `ListChurnTests`.
extension LoadState: Equatable where Value: Equatable {}

/// What the thread pane is showing.
public struct ThreadScreen: Sendable {
    public var presentation: ThreadPresentation
    /// The transcript, in the deck's order, each line already carrying §6.2's
    /// attribution. Rows rather than messages because the same record reaches
    /// this app from three views under one id, and two parallel arrays is
    /// exactly how one of them gets counted twice.
    public var rows: [TranscriptRow]
    /// **What the conversation actually draws**: those rows and the desk's tool
    /// calls, in one list, in the order they happened. `rows` is kept beside it
    /// because the echo, the dedupe and §6.2's attribution are all about
    /// messages; `entries` is the drawing order and nothing else derives from
    /// it.
    public var entries: [ThreadEntry]
    public var connection: ConnectionState
    /// Other threads this agent is in — the read-only peer transcripts.
    public var siblings: [ThreadSummary]
    /// The first line he had not read when he opened it: where `NEW` goes.
    public var newFrom: String?
    /// The deck holds lines older than the first one drawn.
    public var hasEarlier: Bool

    /// Derived, never stored: there is one array of records in this struct.
    public var messages: [Message] { rows.map(\.message) }

    public init(
        presentation: ThreadPresentation,
        rows: [TranscriptRow],
        entries: [ThreadEntry] = [],
        connection: ConnectionState,
        siblings: [ThreadSummary],
        newFrom: String? = nil,
        hasEarlier: Bool = false
    ) {
        self.newFrom = newFrom
        self.hasEarlier = hasEarlier
        self.presentation = presentation
        self.rows = rows
        self.entries = entries.isEmpty ? rows.map(ThreadEntry.said) : entries
        self.connection = connection
        self.siblings = siblings
    }
}

/// The one object the views observe. It owns the actors, forwards their
/// updates, and holds nothing the actors already hold.
@MainActor
public final class DeckStore: ObservableObject {

    @Published public private(set) var roster: LoadState<SidebarSnapshot> = .loading
    // Nothing is open and nothing has been asked for, so the pane says so. A
    // spinner here is a promise the app cannot keep: on a fresh window there is
    // no request in flight for it to resolve.
    @Published public private(set) var thread: LoadState<ThreadScreen> = .empty
    @Published public private(set) var connection: ConnectionState = .idle
    @Published public private(set) var agents: [String: Agent] = [:]

    @Published public private(set) var selectedAgentName: String?
    @Published public private(set) var selectedThreadID: String?
    @Published public private(set) var settingsAgent: Agent?
    @Published public private(set) var settingsError: DeckError?
    @Published public private(set) var draftIsSending = false
    @Published public private(set) var sendError: String?

    /// What is in the conversation's composer.
    ///
    /// It lives here, not in the view, for the same reason
    /// `PendingThread.typedText` does: a refusal must not be able to eat the
    /// sentence. The view cleared it before awaiting the deck, so a failed
    /// send was indistinguishable from a successful one -- an empty box --
    /// and the text was gone.
    @Published public var composerDraft = ""
    /// The message he is answering, shown as the strip above the composer.
    @Published public private(set) var replyDraft = ReplyDraft()
    /// The bubble he last clicked — what ⌘R answers.
    @Published public var selectedMessageID: String?
    /// A tap on a quote: the transcript scrolls to the original.
    @Published public private(set) var quoteJump: QuoteJumpRequest?
    /// A page of older lines is on its way.
    @Published public private(set) var isLoadingEarlier = false

    /// The approval cards drawn inline in the open conversation. Only the
    /// selected agent's, because the card is a message in *its* thread.
    @Published public private(set) var approvals: [ApprovalCard] = []
    /// **Everything waiting on him anywhere on this deck**, oldest first.
    ///
    /// Not filtered by the selection, and that is the whole point: `approvals`
    /// above is the open conversation's share, so an ask raised on a desk he is
    /// not reading reached him on no screen at all. This is what the attention
    /// tray draws, pinned above the inspector, whatever thread is open.
    ///
    /// Empty means nothing is waiting — the tray draws nothing rather than an
    /// empty box, because a box that is usually empty is a box he stops
    /// looking at.
    @Published public private(set) var attention: [AttentionItem] = []
    /// The Claude plan meter, from a client that can answer `GET /v1/usage`.
    /// `nil` on one that cannot, or before it has answered: no meter is drawn.
    @Published public private(set) var usage: ClaudeUsage?
    /// It moves slowly, so it is asked at most once a minute.
    private var usageFetchedAt: Date?
    /// "Move to account…", per desk: in flight, or why it did not work. A
    /// success leaves no entry; the row's badge is the proof.
    @Published public private(set) var accountMoves: [String: AccountMoveStatus] = [:]
    /// "Sign in on this Mac", per card id: where each one has got to.
    @Published public private(set) var macSignIns: [String: MacSignInPhase] = [:]
    /// Requests that arrived unreadable. Said out loud rather than dropped: a
    /// request nobody can see is still a session sitting blocked.
    @Published public private(set) var approvalNotice: String?
    @Published public private(set) var approvalProblem: String?

    /// What is in the search field, and what came back for it. `nil` outcome
    /// means the field is empty — not "no results".
    @Published public private(set) var searchQuery = ""
    @Published public private(set) var searchOutcome: SidebarSearchResult?

    /// Why the manual form's last create was refused, in the deck's terms. That
    /// form stays open on a refusal — every one of them is fixed by editing a
    /// field. The conversation's own refusals live on `pending` instead, next
    /// to the sentence they are about.
    @Published public private(set) var createProblem: String?
    @Published public private(set) var isCreating = false

    /// Set once, right after an interview whose `pretrust` came back not-ok:
    /// the deck could not pre-accept Claude Code's trust dialog, so the
    /// window that just opened is sitting on it. Shown in the thread he is
    /// already looking at, not left for him to find later in the panel.
    /// Cleared the moment he opens anything else — see `open(threadID:)`.
    @Published public private(set) var pretrustWarning: String?
    private var pretrustWarningThreadID: String?

    /// **What the open conversation says is happening, right now.**
    ///
    /// Computed rather than stored, off the four `@Published` facts it reads,
    /// so there is no fifth copy of the truth to fall out of step with them —
    /// which is how a "Working" light ends up staying on after the desk has
    /// finished. `nil` means there is nothing to claim: a healthy stream on a
    /// `peer:` transcript, which has no desk of its own.
    public var deskStatus: DeskStatus? {
        DeskStatus.make(
            agent: selectedAgentName.flatMap { agents[$0] },
            connection: connection,
            // Only the questions still waiting. The transcript now keeps the
            // answered ones on screen as a record, and a card he has already
            // dealt with must not keep the pane saying "Waiting on you".
            approvals: approvals.filter(\.isAnswerable),
            isSending: draftIsSending
        )
    }

    /// **The desk's character, while it works** — drawn under the last line of
    /// the conversation, the reference's "it's on it". `nil` when nothing is running.
    public var workingLook: AvatarLook? {
        guard let name = selectedAgentName, let desk = agents[name],
              selectedThreadID == desk.threadID,
              desk.state == .working || draftIsSending else { return nil }
        return desk.look
    }

    /// **The take-over that is open, or `nil` for closed.**
    ///
    /// It lives on the store rather than inside the thumbnail because three
    /// places offer the way in — the strip on the conversation, the inspector's
    /// blocked section, and the thumbnail itself — and a take-over presented
    /// from one of them has to be the same take-over. It is presented once, at
    /// `DeckRootView`, over the whole window, which is the only place with
    /// enough room to draw a 1280x800 display at a size he can read.
    ///
    /// **Only `takeOver(...)` sets it, and only he calls that.** Nothing on the
    /// roster path, the stream path or the approvals poll touches it: a desk
    /// stopping must never present a modal over whatever he is typing. See
    /// `Takeover` and `TheTakeOverIsOneClickFromWhereHeIsNeededTests`.
    @Published public private(set) var takeover: TakeoverRequest?

    /// **Watch mode**: the desk whose computer fills the main window, with the
    /// chat as a side panel. `nil` is the normal window.
    @Published public private(set) var watching: TakeoverRequest?

    /// The thread that exists before the agent does. `nil` means "+" has not
    /// been pressed, or what it opened has been sent, cancelled or walked away
    /// from — there is no third state and nothing lingers.
    @Published public private(set) var pending: PendingThread?
    /// The second door. Never opened by "+" itself.
    @Published public private(set) var isShowingManualSetup = false
    /// The Connectors & Skills panel, opened from the sidebar's bottom row.
    @Published public private(set) var isShowingStore = false
    /// The Money screen, opened from the sidebar's bottom rows.
    @Published public private(set) var isShowingMoney = false
    /// Standing approvals (§23), opened from the sidebar's bottom rows.
    @Published public private(set) var isShowingStanding = false
    /// Proposals from desks waiting on him: the badge on that row.
    @Published public private(set) var standingProposed = 0
    private var standingFetchedAt: Date?

    /// The routines panel, for the agent the inspector is showing.
    @Published public private(set) var routines: [Routine] = []
    @Published public private(set) var routinesEmptyMessage: String?
    @Published public private(set) var routinesProblem: String?

    private let client: DeckClient
    /// His screenshots and files go to the deck first (`OutgoingAttachments`).
    private let uploader: AttachmentUploading?
    private var trays: [String: AttachmentTray] = [:]
    private var trayWatches: [String: AnyCancellable] = [:]
    /// Handed in by the app for a live deck; nil everywhere else. See
    /// `shellClient` for why it is not derived from `client`.
    private let shell: DeskShellClient?
    /// Handed in beside the client for a deck that serves the screen from
    /// somewhere else; nil everywhere else. See `screenClient`.
    private let screens: AgentScreenClient?
    /// Handed in beside the client for a deck that answers desk calls from
    /// somewhere else; nil everywhere else. See `incomingCallClient`.
    private let incoming: IncomingCallClient?

    /// The same connection, asked whether it can fetch a desk's screen.
    ///
    /// `AgentScreenClient` is a separate protocol on purpose — a deck older
    /// than the agent's-computer routes conforms to `DeckClient` and not to
    /// this — so the cast is the honest question rather than a downcast that
    /// assumes. `nil` means the inspector draws no screen panel, which is the
    /// correct picture of a deck that cannot serve one.
    ///
    /// Two sources, exactly as `shellClient` has two and for the same reason:
    /// a deck object that serves the screen from somewhere else is handed in
    /// beside the client and wins; otherwise the client is asked itself.
    public var screenClient: AgentScreenClient? { screens ?? (client as? AgentScreenClient) }

    /// What runs one command on a desk's machine, or `nil` on a deck that
    /// cannot.
    ///
    /// **Two sources, and the order matters.** The fixture answers the
    /// terminal route itself, so the cast finds it — the same honest question
    /// `screenClient` asks. `HTTPDeckClient` cannot be extended to conform
    /// from outside its own file (`baseURL`, `tokens` and `performer` are all
    /// `private`, which in Swift means that file), so against a live deck the
    /// app hands one in beside the client and it wins. Neither is invented
    /// here: a store that resolved the deck's URL a second time would be a
    /// second answer to a question the app has already answered once.
    public var shellClient: DeskShellClient? { shell ?? (client as? DeskShellClient) }

    /// The desk's live terminal (a PTY over a socket), or `nil` on a client
    /// that cannot open one — then the one-command drawer is all there is.
    public var terminalStreams: DeskTerminalStreaming? { client as? DeskTerminalStreaming }

    /// Connectors & Skills. A separate protocol, so a transport that predates
    /// `/v1/store/*` answers nil and the sidebar row says so instead of
    /// opening a panel that cannot load.
    public var storeClient: StoreClient? { client as? StoreClient }
    /// Answering, declining and the "Calls from desks" settings; nil on a deck
    /// that predates desk calls, and the app says so instead of pretending.
    public var incomingCallClient: IncomingCallClient? { incoming ?? (client as? IncomingCallClient) }
    /// Bumped by every `open`. The thread pipeline is identified by this rather
    /// than by its thread id, because a desk can rename itself — and therefore
    /// re-key its thread — while the same pipeline is still the live one.
    private var threadGeneration = 0
    private let backoff: @Sendable (Int) async throws -> Void
    /// The one retry in flight while the deck is away, and how many have been
    /// tried. Zero means the deck is answering — see `markLive`.
    private var reconnectTask: Task<Void, Never>?
    private var reconnectAttempt = 0
    /// One roster request is cut off here: a stalled socket (a deck mid-restart,
    /// a waking tunnel) must not hold the sidebar's spinner for URLSession's 60s.
    private let rosterTimeout: TimeInterval
    /// **How often the open conversation asks for its tool calls.**
    ///
    /// `/v1/stream` carries no approval frame — §8 lists every type it sends
    /// and an ask is not one of them — so a poll is the *only* way a question
    /// the desk stopped on can reach a window that is already open. Before
    /// this, `/v1/approvals` was fetched once per selection, and a tool call
    /// raised a second after he opened the chat could not appear until he
    /// clicked another desk and came back. That is the "reopen the chat" he
    /// reported.
    ///
    /// Two seconds: an ask blocks a session until it is answered, and the
    /// response is a few hundred bytes over a loopback-shaped link. It runs
    /// for as long as this window is attached to a deck — **not** while a
    /// conversation happens to be open — and it publishes nothing when the
    /// answer has not changed, so a tick that finds nothing costs one small
    /// request and no redraw at all. See `publishApprovals`.
    private let approvalPollInterval: TimeInterval
    private let rosterModel: RosterModel
    private let approvalsModel: ApprovalsModel
    private let handoffsModel: HandoffsModel
    /// Opens a throwaway Chrome here for a passkey sign-in and hands it to
    /// every desk. `nil` against a deck that cannot take one.
    public let signIn: MacSignInFlow?
    /// Reads the login he already has in his Mac browser and shares it — the
    /// primary path. `nil` when no readable browser is found or the deck cannot
    /// take a login.
    public let macLogin: MacLoginImport?
    /// The name of the browser he uses, for the card's primary button. `nil`
    /// hides that button.
    public let macBrowserLabel: String?
    private let routinesModel: RoutinesModel
    /// The last roster the deck sent, kept here so filtering the sidebar is a
    /// synchronous pass over what is already on screen.
    private var lastPayload = RosterPayload(agents: [], threads: [], sectionOrder: [])
    /// **Every live ask on the deck**, as the deck sent it, so an answer is
    /// always made against the real thing rather than a card rebuilt from a
    /// button. Deck-wide because the tray can answer a desk he is not looking
    /// at; it used to hold only the selected desk's, which is why answering
    /// from anywhere else was impossible.
    private var pendingApprovals: [Approval] = []
    /// His own first line, kept until the deck's transcript carries it.
    private var pendingEcho: Message?
    /// A hire's line created the thread, so it is first; a message into an
    /// existing conversation is the newest thing in it.
    private var pendingEchoPosition: ThreadEcho.Position = .first
    /// The desk's `unread` as it stood when he opened the thread, taken before
    /// `markRead` zeroes it; turned into a message id once the lines arrive.
    private var unreadAtOpen = 0
    private var newFrom: String?
    /// K4: the newest state of each decision, from a frame or his own tap.
    private var decisionOverrides: [String: Decision] = [:]
    /// Why his last tap on a decision card did not land.
    @Published public private(set) var decisionProblem: String?
    private var sync: ConversationSync?
    private var earlierTask: Task<Void, Never>?
    private var pendingQuoteJump: String?
    private var syncTask: Task<Void, Never>?
    private var updatesTask: Task<Void, Never>?
    private var rosterFeedTask: Task<Void, Never>?
    private var readTask: Task<Void, Never>?
    /// **The deck-wide watch on everything waiting for him.**
    ///
    /// It used to live exactly as long as an open conversation, and that is
    /// what froze the attention tray. `open(threadID:)` cancelled it and
    /// returned early on `nil`; `closeThread()` calls `open(nil)`; and
    /// `loadApprovals()` has no other caller. So the moment he closed a chat,
    /// resolved items stopped leaving the tray and new asks stopped arriving —
    /// on the one strip whose entire reason for existing is the ask raised
    /// somewhere he is **not** looking. Nothing open at all is the strongest
    /// case of that, not an exemption from it.
    ///
    /// So it follows the deck, not the selection: started once the window is
    /// attached, idempotent, and ended by the window going — see `deinit`.
    private var attentionPollTask: Task<Void, Never>?
    /// The long-poll for the phone's "sign in from my Mac" requests.
    private var loginRequestTask: Task<Void, Never>?
    /// Owner alerts as Mac notifications: the same once-only page the phone
    /// reads (`GET /v1/owner/alerts`), polled on the attention clock.
    private let ownerNotifier = OwnerNotifier()
    private var ownerRouter: OwnerNotificationRouter?
    private var settings: AgentSettingsModel?
    private var credentialsObserver: NSObjectProtocol?
    /// Only ever non-nil under DECK_DIAGNOSE=1. Counts how often this object
    /// tells SwiftUI to rebuild, which is the number that says whether our own
    /// state is what is driving the graph.
    private var diagnoseObserver: AnyCancellable?

    public init(
        client: DeckClient,
        shell: DeskShellClient? = nil,
        screens: AgentScreenClient? = nil,
        visibleRowsPerSection: Int = .max,
        backoff: @escaping @Sendable (Int) async throws -> Void = ConversationSync.defaultBackoff,
        approvalPollInterval: TimeInterval = 2,
        signIn: MacSignInFlow? = nil,
        macLogin: MacLoginImport? = nil,
        macBrowserLabel: String? = nil,
        uploader: AttachmentUploading? = nil,
        rosterTimeout: TimeInterval = 12,
        incomingCalls: IncomingCallClient? = nil
    ) {
        self.rosterTimeout = rosterTimeout
        self.client = client
        self.uploader = uploader ?? client as? AttachmentUploading
        let sharing = client as? LoginSharingClient
        self.signIn = signIn ?? sharing.map {
            MacSignInFlow(browser: ChromeSignInBrowser(), sharing: $0)
        }
        // Prefer the login he already has. Built once from his default browser.
        if let macLogin {
            self.macLogin = macLogin
            self.macBrowserLabel = macBrowserLabel
        } else if let sharing, let found = MacLoginDiscovery.extractor() {
            self.macLogin = MacLoginImport(extractor: found.extractor, sharing: sharing)
            self.macBrowserLabel = macBrowserLabel ?? found.browserLabel
        } else {
            self.macLogin = nil
            self.macBrowserLabel = macBrowserLabel
        }
        self.shell = shell
        self.screens = screens
        self.incoming = incomingCalls
        self.backoff = backoff
        self.approvalPollInterval = approvalPollInterval
        self.rosterModel = RosterModel(client: client, visibleRowsPerSection: visibleRowsPerSection)
        self.approvalsModel = ApprovalsModel(client: client)
        self.handoffsModel = HandoffsModel(client: client)
        self.routinesModel = RoutinesModel(client: client)

        if Diagnostics.isEnabled {
            diagnoseObserver = objectWillChange.sink { _ in
                Diagnostics.count("store.willChange")
            }
        }

        // Settings is its own window. When the token in it changes, ask the
        // deck again rather than leaving a stale failure on the window behind.
        credentialsObserver = NotificationCenter.default.addObserver(
            forName: .deckCredentialsChanged, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in await self?.loadRoster() }
        }
    }

    // MARK: roster

    public func loadRoster() async {
        // Asking a deck for its roster is this window declaring itself attached
        // to that deck, and from that moment everything waiting on him has to
        // be able to reach the tray — including on a deck with no desks, where
        // nothing is ever selected and no thread is ever opened.
        watchForAsks()
        // Only when there is nothing on screen yet. This runs as a background
        // refresh too, and flashing a loaded sidebar back to a spinner is both
        // a visible flicker and two publishes — each one a full rebuild of the
        // conversation. See `ListChurnTests`.
        if case .loaded = roster {} else { set(roster: .loading) }
        do {
            let model = rosterModel
            do {
                try await FirstLoad.withTimeout(rosterTimeout) { try await model.load() }
            } catch is FirstLoad.TimedOut {
                throw DeckError.transport("the deck didn't answer in time")
            }
            await publishRoster(allowEmpty: true)
            await refreshAgents()
            await refreshUsage(force: true)
            let snapshot = await rosterModel.snapshot
            if selectedAgentName == nil, let first = snapshot.sections.first?.rows.first {
                select(agent: first.agent.name, threadID: first.threadID)
            } else if selectedThreadID == nil {
                // A deck with no desks opens no thread. Saying "empty" is the
                // honest answer; spinning would be waiting for nothing.
                thread = .empty
            }
        } catch let error as DeckError {
            fail(with: error)
        } catch {
            fail(with: .transport(error.localizedDescription))
        }
    }

    /// A roster that would not load leaves nothing to open, so both panes report
    /// the same condition — they contradicted each other before this.
    ///
    /// A conversation already on screen is the exception and is kept: its
    /// messages arrived, and a failed roster refresh does not un-arrive them.
    private func fail(with error: DeckError) {
        // **A refresh that could not reach the deck does not un-list his
        // desks.** Measured: the daemon was restarted for about four seconds
        // and this line replaced a loaded sidebar with a failure screen, in a
        // window that was open all day and had every desk on it. The roster on
        // screen arrived; the socket is what went.
        if case .loaded = roster, isDrop(error) {
            rideOutDrop()
            return
        }
        roster = .failed(error)
        if selectedThreadID == nil {
            thread = .failed(error)
        }
        // Never reached yet, and it was the socket, not a refusal: say so (with
        // "Try again") and keep asking on the backoff, as the phone does. A
        // deck that was down when the app opened is found when it comes up.
        if isDrop(error) {
            reconnectAttempt += 1
            scheduleReconnect()
        }
    }

    /// **Whether the deck went away, as opposed to answered.**
    ///
    /// Only a transport failure is ridden out. Everything else — a refused
    /// token, a deck with no token of its own, an unreadable payload — is the
    /// deck talking, and hiding it behind "Reconnecting" would leave him
    /// waiting for a recovery that is never coming because the fix is a thing
    /// he has to type.
    private func isDrop(_ error: DeckError) -> Bool {
        if case .transport = error { return true }
        return false
    }

    /// **The deck went away while something was on screen.**
    ///
    /// Say so, keep what was last known, and come back. All three, because any
    /// two of them is a different bug: saying so and blanking is the failure
    /// screen he saw; keeping it and saying nothing is a stale transcript that
    /// reads as current; and both without the retry is an app that never
    /// notices the deck is back.
    private func rideOutDrop() {
        reconnectAttempt += 1
        let state = ConnectionState.reconnecting(attempt: reconnectAttempt)
        if connection != state { connection = state }
        // The word goes on the conversation too, not only in the toolbar: he
        // is looking at the transcript.
        if case .loaded(var screen) = thread, screen.connection != state {
            screen.connection = state
            thread = .loaded(screen)
        }
        scheduleReconnect()
    }

    /// One retry in flight at a time, after the same backoff the stream uses —
    /// which grows with the attempt, so a deck that is down for an afternoon is
    /// not asked about a thousand times.
    private func scheduleReconnect() {
        guard reconnectTask == nil else { return }
        let wait = backoff
        let attempt = reconnectAttempt
        reconnectTask = Task { [weak self] in
            try? await wait(attempt)
            guard let self, !Task.isCancelled else { return }
            self.reconnectTask = nil
            await self.reconnectNow()
        }
    }

    /// Ask for the roster again and reopen the conversation from the cursor it
    /// already holds. If the deck is still down both fail the same way they
    /// just did, which schedules the next attempt.
    private func reconnectNow() async {
        await loadRoster()
        if let threadID = selectedThreadID {
            open(threadID: threadID)
        }
    }

    /// The deck answered. Stop counting, and stop trying.
    private func markLive() {
        guard reconnectAttempt != 0 else { return }
        reconnectAttempt = 0
        reconnectTask?.cancel()
        reconnectTask = nil
    }

    /// **Nothing is assigned here unless it actually changed**, for the same
    /// reason as `publishApprovals` — and this one matters more, because it
    /// runs on every frame the deck sends about any desk on the roster.
    ///
    /// `@Published` fires on every `set`, equal or not, and one store drives
    /// both the sidebar and the conversation. A desk that is working sends
    /// state continuously, so an unconditional assignment here was a full
    /// rebuild of the thread pane several times a second — which is
    /// `ForEachState.item` and `_swift_getGenericMetadata` at the top of his
    /// live sample, and *"when its working its stuck on my ui"* in his words.
    private func publishRoster(allowEmpty: Bool = false) async {
        Diagnostics.count("store.publishRoster")
        lastPayload = await rosterModel.payload
        let payload = lastPayload
        // A filtered sidebar is still a *loaded* one. Only a deck with no
        // desks at all is `.empty`, so "nothing matched what you typed" and
        // "this deck has no agents" never share a screen.
        if !searchQuery.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            let outcome = SidebarSearch.result(payload: payload, query: searchQuery)
            if searchOutcome != outcome { searchOutcome = outcome }
            set(roster: .loaded(outcome.snapshot))
            return
        }
        if searchOutcome != nil { searchOutcome = nil }
        let snapshot = await rosterModel.snapshot
        let isEmpty = snapshot.sections.allSatisfy { $0.rows.isEmpty }
        if isEmpty && allowEmpty {
            set(roster: .empty)
        } else if !isEmpty {
            set(roster: .loaded(snapshot))
        }
    }

    private func set(roster new: LoadState<SidebarSnapshot>) {
        guard roster != new else { return }
        roster = new
    }

    /// The desk dictionary the open conversation reads its names and state out
    /// of. Refreshed from the roster on every frame, so it is the other
    /// unconditional publish that kept the pane rebuilding.
    private func refreshAgents() async {
        let latest = await rosterModel.agentsByName()
        guard agents != latest else { return }
        agents = latest
    }

    /// Filtering the sidebar. Local, and deliberately not a request: the whole
    /// roster is already here.
    public func search(_ query: String) {
        searchQuery = query
        // A roster that never arrived cannot be filtered. Rebuilding from the
        // empty payload replaced the failure screen -- and the retry button
        // that was the only way out of it -- with "This deck has no desks on
        // its roster", which he would read as an answer about his deck.
        if case .failed = roster {
            searchOutcome = nil
            return
        }
        let payload = lastPayload
        guard !query.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            searchOutcome = nil
            let snapshot = SidebarSnapshot.build(from: payload)
            roster = snapshot.sections.isEmpty ? .empty : .loaded(snapshot)
            return
        }
        let outcome = SidebarSearch.result(payload: payload, query: query)
        searchOutcome = outcome
        roster = .loaded(outcome.snapshot)
    }

    // MARK: selection

    public func select(agent name: String, threadID: String?) {
        unreadAtOpen = agents[name]?.unread ?? 0
        selectedAgentName = name
        settingsAgent = agents[name]
        settingsError = nil
        settings = agents[name].map { AgentSettingsModel(agent: $0, client: client) }
        readTask?.cancel()
        readTask = Task { [weak self] in
            guard let self else { return }
            await self.rosterModel.markRead(agent: name)
            await self.publishRoster()
            await self.publishApprovals()
            await self.publishRoutines()
        }
        open(threadID: threadID)
    }

    /// **The screen, the inspector and the take-over follow the thread he is
    /// in, not the row he last clicked.**
    ///
    /// Owner, 2026-10-01: *"I always see Atlas's computer, no matter where I
    /// am."* Only `select(agent:)` moved the desk; every other way into a
    /// conversation — the line in Atlas's thread that opens its talk with
    /// another desk, a retry, a direct id — moved the thread and left the desk
    /// behind, so the thumbnail, the screen button and every click went to
    /// Atlas. The deck was measured serving each desk its own container; the
    /// wrong desk was chosen here (`TheScreenFollowsTheDeskHeIsInTests`).
    ///
    /// `direct:<desk>` is that desk, outright. `peer:<a>|<b>` is the side he
    /// did not come from; the selection stays where it was, because it is the
    /// perspective the peer transcript is drawn from.
    private func followDesk(of threadID: String?) {
        guard let threadID else { return }
        if let name = ThreadID.desk(ofDirect: threadID), let agent = agents[name] {
            let moved = selectedAgentName != name
            if moved { selectedAgentName = name }
            let shown = showInInspector(agent)
            if moved || shown { republishDeskPanels() }
        } else if let selected = selectedAgentName,
                  let other = ThreadID.peer(in: threadID, otherThan: selected),
                  let agent = agents[other] {
            if showInInspector(agent) { republishDeskPanels() }
        }
    }

    /// True when the inspector moved to `agent`.
    private func showInInspector(_ agent: Agent) -> Bool {
        guard settingsAgent?.name != agent.name else { return false }
        settingsAgent = agent
        settingsError = nil
        settings = AgentSettingsModel(agent: agent, client: client)
        return true
    }

    private func republishDeskPanels() {
        Task { [weak self] in
            await self?.publishApprovals()
            await self?.publishRoutines()
        }
    }

    /// The desk whose screen, terminal and take-over he is offered: the one
    /// the inspector is on, which `followDesk` keeps on the open thread.
    private var deskInView: Agent? {
        if let shown = settingsAgent { return agents[shown.name] ?? shown }
        return selectedAgentName.flatMap { agents[$0] }
    }

    public func open(threadID: String?) {
        Diagnostics.count("store.openThread")
        // Opening anything at all is walking away from the unsaved one. It was
        // never a desk, so there is nothing to discard but the draft itself.
        pending = nil
        // The echo belongs to exactly one thread. Anywhere else it would be a
        // sentence appearing in a conversation it was never said in.
        if pendingEcho?.threadID != threadID { pendingEcho = nil }
        // Likewise the pretrust warning: it is about the window that thread's
        // interview just opened, not a fact worth carrying to whatever he
        // opens next.
        if pretrustWarningThreadID != threadID {
            pretrustWarning = nil
            pretrustWarningThreadID = nil
        }
        if selectedThreadID != threadID {
            newFrom = nil
            // A reply belongs to the conversation it was started in.
            if replyDraft.target != nil { replyDraft.cancel() }
            selectedMessageID = nil
        }
        selectedThreadID = threadID
        followDesk(of: threadID)
        threadGeneration += 1
        let generation = threadGeneration
        syncTask?.cancel()
        updatesTask?.cancel()
        earlierTask?.cancel()
        isLoadingEarlier = false
        pendingQuoteJump = nil
        // Cancelled with the other two, not left to unwind on its own. It was
        // harmless while the stream delivered nothing; now that frames actually
        // arrive, a feed still attached to the thread he just left is a live
        // consumer of the deck's traffic that nobody is drawing.
        rosterFeedTask?.cancel()
        // The attention poll is deliberately NOT touched here. It watches the
        // deck, not this thread: see `attentionPollTask`.
        sync = nil

        guard let threadID else {
            thread = .empty
            return
        }
        // Reopening the *same* conversation — which is what a recovery is —
        // must not throw away what is already drawn. Those messages arrived;
        // the deck coming back is not new news about them, and a spinner in
        // their place is the blank pane the restart produced. Opening a
        // different thread still shows one, because then there is genuinely
        // nothing yet.
        if case .loaded(let drawn) = thread, drawn.presentation.threadID == threadID {
        } else {
            thread = .loading
        }
        let sync = ConversationSync(client: client, threadID: threadID, backoff: backoff)
        self.sync = sync

        updatesTask = Task { [weak self] in
            let stream = await sync.updates()
            for await update in stream {
                self?.apply(update, generation: generation)
            }
        }
        // The deck runs one stream for everything, so the sidebar's frames
        // arrive on the same connection the open thread is already using.
        rosterFeedTask = Task { [weak self] in
            let stream = await sync.deckEvents()
            for await event in stream {
                await self?.applyToRoster(event)
            }
        }
        syncTask = Task { [weak self] in
            do {
                try await sync.run()
            } catch let error as DeckError {
                self?.failThread(error)
            } catch is CancellationError {
                // Switching threads; nothing to report.
            } catch {
                self?.failThread(.transport(error.localizedDescription))
            }
        }
    }

    /// Waits for the in-flight thread work to finish. Used by tests so nothing
    /// has to sleep; harmless in the app.
    public func settle() async {
        await earlierTask?.value
        await readTask?.value
        await syncTask?.value
        await updatesTask?.value
        await rosterFeedTask?.value
    }

    /// **Guarded on the generation, not on the thread id.**
    ///
    /// The id was the wrong key twice over. It let a stale sync through
    /// whenever the same thread was reopened, and — the defect he actually hit
    /// — it rejected every update from the *current* sync the moment a desk
    /// renamed itself, because the rename changes `selectedThreadID` underneath
    /// a task that was started with the old one. The conversation stayed on
    /// screen and simply stopped moving.
    ///
    /// A counter bumped by `open` answers the only question this guard is
    /// really asking: is this update from the pipeline that is on screen now?
    private func apply(_ update: SyncUpdate, generation: Int) {
        guard generation == threadGeneration, let threadID = selectedThreadID else { return }
        Diagnostics.count("store.applyUpdate")
        if connection != update.connection { connection = update.connection }
        if update.connection == .live { markLive() }

        let siblings = update.participants.isEmpty ? [] : siblingThreads(for: threadID)
        // His line stays where he typed it until the deck's own copy arrives.
        let messages = ThreadTimeline.settleDecisions(
            ThreadEcho.merge(pendingEcho, into: update.messages, at: pendingEchoPosition),
            overrides: decisionOverrides)
        if let echo = pendingEcho, ThreadEcho.carried(echo, by: update.messages) {
            pendingEcho = nil
        }
        let presentation = ThreadPresentation.make(
            threadID: threadID,
            participants: update.participants,
            isReadOnly: update.isReadOnly,
            title: titleForThread(threadID),
            agents: agents
        )
        let rows = attributed(messages, page: threadID)
        // Placed once, on the first page, and then left where it was: the
        // divider marks what was new when he arrived, not what is new now.
        if newFrom == nil, unreadAtOpen > 0, !update.messages.isEmpty {
            newFrom = ThreadTimeline.firstUnread(in: update.messages, unread: unreadAtOpen)
            unreadAtOpen = 0
        }
        thread = .loaded(
            ThreadScreen(
                presentation: presentation,
                rows: rows,
                entries: ThreadTimeline.entries(rows: rows, toolCalls: toolCards(for: threadID)),
                connection: update.connection,
                siblings: siblings,
                newFrom: newFrom,
                hasEarlier: update.hasEarlier
            )
        )
        if let pendingQuoteJump { jumpOnceDrawn(pendingQuoteJump) }
    }

    /// The tool calls that belong in **this** thread.
    ///
    /// An ask is raised by a desk, so its card belongs in that desk's own
    /// conversation — `direct:<agent>`. A `peer:` transcript is two agents
    /// talking and has no desk of its own to have stopped, so nothing is drawn
    /// there rather than the selected agent's cards leaking into a record they
    /// were not part of.
    private func toolCards(for threadID: String) -> [ApprovalCard] {
        guard let selectedAgentName, threadID == "direct:\(selectedAgentName)" else { return [] }
        return approvals
    }

    /// Rebuilds the drawing order after the tool calls change but the messages
    /// have not. The two arrive on different clocks — one off the stream, one
    /// off the poll — and the list on screen is the join of them.
    private func republishEntries() {
        guard case .loaded(var screen) = thread else { return }
        let entries = ThreadTimeline.entries(
            rows: screen.rows, toolCalls: toolCards(for: screen.presentation.threadID)
        )
        guard entries != screen.entries else { return }
        screen.entries = entries
        thread = .loaded(screen)
    }

    /// §6.2, applied once for the whole pane. The desk is the participant of
    /// the page's own `direct:` id; on a `peer:` page there is none, and every
    /// line there already carries the page's id, so nothing is attributed.
    private func attributed(_ messages: [Message], page threadID: String) -> [TranscriptRow] {
        let known = agents
        return Transcript.rows(
            messages,
            page: threadID,
            desk: ThreadID.desk(ofDirect: threadID) ?? "",
            displayName: { known[$0]?.displayName ?? Agent.displayName(forWireName: $0) }
        )
    }

    private func titleForThread(_ id: String) -> String? {
        guard case .loaded(let snapshot) = roster else { return nil }
        return snapshot.sections
            .flatMap(\.rows)
            .flatMap(\.threads)
            .first { $0.id == id }?
            .title
    }

    private func siblingThreads(for id: String) -> [ThreadSummary] {
        guard case .loaded(let snapshot) = roster, let selectedAgentName else { return [] }
        return snapshot.sections
            .flatMap(\.rows)
            .first { $0.id == selectedAgentName }?
            .threads ?? []
    }

    /// **The sync gave up.**
    ///
    /// `ConversationSync.run()` retries a dropped *stream*, but its catch-up
    /// fetch sits above the `do` — so a deck that refuses the fetch as well,
    /// which is exactly what a restarting daemon does for a few seconds, throws
    /// all the way out here. This used to replace the conversation he was
    /// reading with a failure screen and call the connection Offline.
    private func failThread(_ error: DeckError) {
        if case .loaded = thread, isDrop(error) {
            rideOutDrop()
            return
        }
        thread = .failed(error)
        connection = .idle
    }

    // MARK: the sidebar's share of the deck stream

    /// Keeps the roster current off the live stream, so an unread badge or a
    /// state change never costs a second `GET /v1/agents`.
    private func applyToRoster(_ event: DeckEvent) async {
        Diagnostics.count("store.applyRosterEvent")
        switch event {
        case .unread(let agent, let count):
            await rosterModel.applyUnread(agent: agent, count: count)
        case .agentState(let name, let state, let blocked):
            await rosterModel.applyAgentState(name: name, state: state, blocked: blocked)
        case .hello(let snapshot):
            await rosterModel.replace(with: snapshot.asRosterPayload())
            await refreshAgents()
        case .agentRenamed(let oldName, let agent):
            // The whole rename, not just the roster half: the row, the
            // selection, the open thread's id and the settings panel all key on
            // the name. `applyRename` republishes, so this returns rather than
            // falling through and doing it twice.
            await applyRename(to: agent, replacing: oldName)
            return
        case .message(let message, _):
            // §3.0: `last_activity_by` is not on the `agent_state` frame, so
            // the newest message in a thread already open is what moves it.
            // Only a desk's own conversation — a `peer:` line is two desks
            // talking to each other and says nothing about who spoke to him.
            guard let desk = ThreadID.desk(ofDirect: message.threadID),
                  // A routine fire or a deck notice is addressed to the desk,
                  // so the ball is with it — §6.4 keeps `last_activity_by`
                  // at "owner" for them.
                  await rosterModel.applyLastSpeaker(
                      agent: desk, message.role == .agent ? .agent : .owner)
            else { return }
        case .decision(let threadID, let decision):
            // Drawn by the open conversation, not the sidebar.
            decisionOverrides[decision.id] = decision
            if threadID == selectedThreadID { republishDecisions() }
            return
        case .heartbeat:
            return
        }
        await publishRoster()
        // `agents` is what the open conversation reads its desk out of, and it
        // was only ever refreshed on a full roster load. So a live `agent_state`
        // frame moved the sidebar row and left the thread pane holding the
        // state the desk was in when the window opened — which is the whole
        // point of the frame, lost one line short of the screen.
        await refreshAgents()
    }

    // MARK: composing

    /// A read-only thread has no composer to call this from. If one is ever
    /// wired up anyway, `ThreadComposer` refuses it before the network — and
    /// the deck refuses it again with 409 `thread_is_read_only`.
    public func send(_ text: String, replyingTo target: ReplyTarget? = nil) async {
        guard let threadID = selectedThreadID else { return }
        let isReadOnly: Bool
        if case .loaded(let screen) = thread { isReadOnly = screen.presentation.isReadOnly }
        else { isReadOnly = true }

        draftIsSending = true
        defer { draftIsSending = false }

        let composer = ThreadComposer(client: client, threadID: threadID, isReadOnly: isReadOnly)
        let sync = self.sync
        switch await composer.submit(text, replyTo: target?.messageID) {
        case .sent(var stored):
            sendError = nil
            if stored.replyTo == nil { stored.replyTo = target?.quote }
            await confirm(stored, in: sync, threadID: threadID)
        case .refusedEmpty:
            sendError = nil
            // His line was going nowhere between the send returning and the
            // stream echoing it back: the box emptied and the transcript did
            // not change, so the only sign anything had happened was that his
            // sentence had disappeared. Draw it here until the deck's own copy
            // arrives, which `apply` then de-duplicates.
            showEcho(of: text, in: threadID, replyTo: target?.quote)
        case .refusedViewOnly:
            sendError = "This chat is view-only."
        case .failed(let error):
            sendError = error.userFacingText
        }
    }

    /// His voice message was transcribed and queued: his line shows now.
    public func voiceNoteSent(_ message: Message) {
        sendError = nil
        let sync = self.sync
        Task { await confirm(message, in: sync, threadID: message.threadID) }
    }

    /// **The deck's stored copy of his line, kept.** Into the sync it was sent
    /// on, so every later update carries it — the stream may never echo it —
    /// and drawn now, so it never blinks out between the two.
    private func confirm(_ stored: Message, in sync: ConversationSync?, threadID: String) async {
        await sync?.accept(stored)
        guard sync === self.sync else { return }
        showEcho(of: stored.text, in: threadID, stored: stored)
    }

    /// Put his own line into the open transcript straight away, at the bottom
    /// where the newest message belongs, and leave it there until the deck's
    /// transcript carries it.
    private func showEcho(
        of text: String, in threadID: String, replyTo: MessageQuote? = nil, stored: Message? = nil
    ) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let echo = stored ?? Message(
            id: "pending:\(threadID):\(trimmed.hashValue)",
            cursor: "",
            threadID: threadID,
            author: DeckOwner.name,
            role: .owner,
            sentAt: Date(),
            text: trimmed,
            replyTo: replyTo
        )
        pendingEcho = echo
        pendingEchoPosition = .last
        guard case .loaded(let screen) = thread else { return }
        let rows = attributed(
            ThreadTimeline.settleDecisions(
                ThreadEcho.merge(echo, into: screen.messages, at: .last), overrides: decisionOverrides),
            page: threadID
        )
        thread = .loaded(
            ThreadScreen(
                presentation: screen.presentation,
                rows: rows,
                entries: ThreadTimeline.entries(rows: rows, toolCalls: toolCards(for: threadID)),
                connection: screen.connection,
                siblings: screen.siblings,
                newFrom: screen.newFrom,
                hasEarlier: screen.hasEarlier
            )
        )
    }

    /// **K4: his tap on a decision card.** The deck posts the value into the
    /// thread as his message and wakes the desk; the card flips here as soon
    /// as the deck has taken it, and his line is drawn at once like any send.
    public func answer(decision: Decision, with value: String) async {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        guard let decisions = client as? DecisionClient else {
            decisionProblem = "This deck cannot take answers to decisions yet — type it instead."
            return
        }
        do {
            try await decisions.answerDecision(id: decision.id, value: trimmed)
            decisionProblem = nil
            var answered = decision
            answered.state = .answered
            answered.answer = trimmed
            decisionOverrides[decision.id] = answered
            if let threadID = selectedThreadID { showEcho(of: trimmed, in: threadID) }
            republishDecisions()
        } catch let error as DeckError {
            decisionProblem = error.userFacingText
        } catch {
            decisionProblem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    /// Re-applies `decisionOverrides` to the conversation on screen.
    private func republishDecisions() {
        guard case .loaded(var screen) = thread else { return }
        let settled = ThreadTimeline.settleDecisions(screen.messages, overrides: decisionOverrides)
        guard settled != screen.messages else { return }
        screen.rows = attributed(settled, page: screen.presentation.threadID)
        screen.entries = ThreadTimeline.entries(
            rows: screen.rows, toolCalls: toolCards(for: screen.presentation.threadID))
        thread = .loaded(screen)
    }

    /// Send what is in the composer, and empty it **only** if the deck took it.
    ///
    /// Every other outcome -- refused, view-only, the socket down -- leaves the
    /// sentence exactly where he can press Return again. Attachments ride as
    /// `Attached image: <path>` lines, once every one has reached the deck.
    public func submitComposer() async {
        let tray = composerTray
        if let tray, !tray.isEmpty, !tray.isReady { return }
        let attached = tray?.uploaded ?? []
        let text = attached.isEmpty ? composerDraft : AttachmentLines.compose(composerDraft, attached)
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        await send(text, replyingTo: replyDraft.target)
        if sendError == nil {
            composerDraft = ""
            tray?.clear()
            replyDraft.cancel()
        }
    }

    // MARK: quote-replies

    /// Swipe, hover button, menu: the strip goes up over the composer.
    public func reply(to message: Message) {
        replyDraft.begin(message) { [agents] name in
            agents[name]?.displayName ?? Agent.displayName(forWireName: name)
        }
    }

    /// The × on the strip.
    public func cancelReply() { replyDraft.cancel() }

    /// ⌘R: the bubble he last clicked, or — nothing selected — the newest
    /// thing anybody other than him said in this conversation.
    public func replyToSelected() {
        guard case .loaded(let screen) = thread else { return }
        let quotable = screen.messages.filter(ReplyDraft.canQuote)
        let chosen = selectedMessageID.flatMap { id in quotable.first { $0.id == id } }
            ?? quotable.last { !$0.isFromUser }
            ?? quotable.last
        if let chosen { reply(to: chosen) }
    }

    /// A tap on the quote inside a bubble.
    public func jump(to quote: MessageQuote) {
        selectedMessageID = quote.id  // the original is highlighted where it lands
        if case .loaded(let screen) = thread,
           !screen.messages.contains(where: { $0.id == quote.id }), screen.hasEarlier {
            // The original is older than anything drawn: page back to it first,
            // then scroll. A jump issued now would land nowhere.
            loadEarlier(until: quote.id)
            return
        }
        quoteJump = QuoteJumpRequest(messageID: quote.id)
    }

    /// **"Show earlier messages"**: one page further back in the open thread.
    /// The page arrives the way every other line does — through the sync's
    /// update stream — so nothing here draws it.
    public func loadEarlier() {
        loadEarlier(until: nil)
    }

    private func loadEarlier(until messageID: String?) {
        guard let sync, !isLoadingEarlier else { return }
        let generation = threadGeneration
        isLoadingEarlier = true
        earlierTask = Task { [weak self] in
            var found = false
            do {
                if let messageID {
                    found = try await sync.loadEarlier(until: messageID)
                } else {
                    try await sync.loadEarlier()
                }
            } catch {
                // Nothing older drawn; the button stays for another try.
            }
            guard let self, generation == self.threadGeneration else { return }
            self.isLoadingEarlier = false
            if found, let messageID { self.jumpOnceDrawn(messageID) }
        }
    }

    /// The page that holds the original reaches the screen through the update
    /// stream, which may not have been applied yet. Scroll now if it has,
    /// otherwise the moment `apply` draws it.
    private func jumpOnceDrawn(_ messageID: String) {
        if case .loaded(let screen) = thread, screen.messages.contains(where: { $0.id == messageID }) {
            pendingQuoteJump = nil
            quoteJump = QuoteJumpRequest(messageID: messageID)
        } else {
            pendingQuoteJump = messageID
        }
    }

    // MARK: attachments

    /// What he has attached to the open conversation and not sent yet. Nil
    /// where nothing can be uploaded (no thread, or a deck without the route).
    public var composerTray: AttachmentTray? {
        guard let threadID = selectedThreadID, let uploader else { return nil }
        if let tray = trays[threadID] { return tray }
        let tray = AttachmentTray(threadID: threadID, uploader: uploader)
        trays[threadID] = tray
        // The send button reads the tray: an upload finishing must redraw it.
        trayWatches[threadID] = tray.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        return tray
    }

    /// `GET /v1/attachments/...` with his token, for the pictures he sent.
    public var attachmentFetch: (@Sendable (String) async throws -> Data)? {
        guard let http = client as? HTTPDeckClient else { return nil }
        return { try await http.attachmentData(url: $0) }
    }

    /// Streams a held video or audio clip with his token. One per store: the
    /// asset's resource loader holds it weakly.
    public var mediaLoader: DeckMediaLoader? {
        if let made = madeMediaLoader { return made }
        guard let http = client as? HTTPDeckClient else { return nil }
        let made = DeckMediaLoader(client: http)
        madeMediaLoader = made
        return made
    }
    private var madeMediaLoader: DeckMediaLoader?

    /// A paste, a drop or the [+]: each one starts uploading at once.
    public func attach(_ items: [PreparedAttachment]) {
        guard let tray = composerTray else { return }
        items.forEach { tray.add($0) }
    }

    /// Words, or attachments that have all arrived, and nothing still going up.
    public var composerCanSend: Bool {
        guard !draftIsSending else { return false }
        if let tray = composerTray, !tray.isEmpty { return tray.isReady }
        return !composerDraft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public func closeThread() {
        open(threadID: nil)
        selectedThreadID = nil
        // Let go of the desk as well, or the sidebar row stays highlighted:
        // clicking it does not change the List's selection, the setter that
        // reopens a thread never runs, and there is no way back to the
        // conversation at all.
        selectedAgentName = nil
        settingsAgent = nil
        settings = nil
    }

    // MARK: routines

    public func loadRoutines() async {
        await routinesModel.load()
        await publishRoutines()
    }

    /// `true` only when the deck stored it — that is what closes the form.
    @discardableResult
    public func addRoutine(_ draft: RoutineDraft) async -> Bool {
        let created = await routinesModel.create(draft)
        await publishRoutines()
        return created
    }

    public func setRoutine(id: String, enabled: Bool) async {
        await routinesModel.setEnabled(enabled, id: id)
        await publishRoutines()
    }

    public func deleteRoutine(id: String) async {
        await routinesModel.delete(id: id)
        await publishRoutines()
    }

    private func publishRoutines() async {
        routinesProblem = await routinesModel.problem
        // A per-agent panel: an empty message about *this* desk, not about the
        // deck as a whole.
        // The inspector's desk, which is the one these are drawn under.
        guard let desk = settingsAgent?.name ?? selectedAgentName else {
            routines = []
            routinesEmptyMessage = await routinesModel.emptyMessage
            return
        }
        routines = await routinesModel.routines(forAgent: desk)
        let deckIsEmpty = await routinesModel.emptyMessage
        routinesEmptyMessage = routinesProblem == nil && routines.isEmpty
            ? (deckIsEmpty ?? RoutinesModel.emptyText)
            : nil
    }

    // MARK: hiring

    /// Returns `true` only when the deck accepted it, which is what closes the
    /// form. A refusal keeps the typed values and quotes the reason.
    @discardableResult
    public func createAgent(_ draft: AgentDraft) async -> Bool {
        if let problem = draft.problem {
            // Nothing is sent: the deck would refuse it and the answer is the
            // same either way, so the round trip is not worth the delay.
            createProblem = problem
            return false
        }
        isCreating = true
        defer { isCreating = false }
        do {
            let agent = try await client.createAgent(draft)
            createProblem = nil
            // The 201 is one element of the roster array (§11), so it goes
            // straight in. Nothing is invented here: section, thread id and
            // org edges are all the deck's answer, not the form's.
            await rosterModel.insert(agent)
            await refreshAgents()
            await publishRoster()
            select(agent: agent.name, threadID: agents[agent.name]?.threadID ?? agent.threadID)
            return true
        } catch let error as DeckError {
            createProblem = error.userFacingText
            return false
        } catch {
            createProblem = DeckError.transport(error.localizedDescription).userFacingText
            return false
        }
    }

    /// The front door. "+" opens a conversation, not a form: this puts an
    /// unsaved thread on screen and asks the deck for nothing at all.
    public func beginNewAgent() {
        pending = PendingThread()
        createProblem = nil
    }

    /// What is in the pending composer. It lives here, not in the view, so a
    /// refused create cannot take it with it.
    public func typePending(_ text: String) {
        guard var current = pending else { return }
        current.typedText = text
        // Editing the sentence retires the complaint about the old one.
        current.problem = nil
        pending = current
    }

    /// Escape, or the sidebar's cancel. Nothing was created, so nothing is
    /// deleted — the draft simply stops existing.
    public func cancelPending() {
        pending = nil
    }

    public func showManualSetup() {
        isShowingManualSetup = true
    }

    public func dismissManualSetup() {
        isShowingManualSetup = false
    }

    public func showStore() {
        guard storeClient != nil else { return }
        isShowingStore = true
    }

    public func dismissStore() {
        isShowingStore = false
    }

    /// `GET /v1/money`, from a client that can answer it (the real deck and the fixture).
    public var moneyClient: MoneySource? { client as? MoneySource }

    public func showMoney() {
        guard moneyClient != nil else { return }
        isShowingMoney = true
    }

    public func dismissMoney() {
        isShowingMoney = false
    }

    /// §23, from a client that can answer it (the real deck and the fixture).
    public var standingClient: StandingApprovalsClient? { client as? StandingApprovalsClient }

    public func showStanding() {
        guard standingClient != nil else { return }
        isShowingStanding = true
    }

    public func dismissStanding() {
        isShowingStanding = false
        Task { await refreshStandingProposed(force: true) }
    }

    /// The proposals waiting on him, at most every 30s (or `force`). A failure
    /// keeps the last count rather than flashing the badge away.
    func refreshStandingProposed(force: Bool = false) async {
        guard let source = standingClient else { return }
        if !force, let at = standingFetchedAt, Date().timeIntervalSince(at) < 30 { return }
        standingFetchedAt = Date()
        guard let rows = try? await source.standingApprovals(status: "proposed", desk: nil) else { return }
        let count = StandingPresentation.proposedCount(rows)
        if count != standingProposed { standingProposed = count }
    }

    /// His first message *is* the hire: `POST /v1/agents/interview` with what
    /// he typed as the `role_hint`. The name, the title and the description are
    /// the agent's to write, in the conversation that stays on screen.
    ///
    /// Returns `true` only when the deck stood a desk up. On a refusal the
    /// thread stays, the sentence stays, and the reason is shown under it —
    /// losing what he typed is the one failure mode this must not have.
    @discardableResult
    public func submitPending() async -> Bool {
        guard var current = pending else { return false }
        let draft = current.draft
        if let problem = draft.problem {
            current.problem = problem
            pending = current
            return false
        }
        current.isCreating = true
        current.problem = nil
        pending = current

        isCreating = true
        defer { isCreating = false }
        do {
            let outcome = try await client.startInterview(draft)
            let provisional = outcome.agent
            createProblem = nil
            // Two requests stand between his sentence and the transcript that
            // will contain it. It is drawn from here in the meantime.
            pendingEcho = Message(
                id: "pending:\(provisional.threadID)",
                cursor: "",
                threadID: provisional.threadID,
                author: DeckOwner.name,
                role: .owner,
                sentAt: Date(),
                text: draft.roleHint.trimmingCharacters(in: .whitespacesAndNewlines)
            )
            // This sentence created the thread, so everything the deck puts in
            // there is an answer to it and his line stays at the top.
            pendingEchoPosition = .first
            pending = nil
            await rosterModel.insert(provisional)
            await refreshAgents()
            await publishRoster()
            select(agent: provisional.name, threadID: provisional.threadID)
            // After `select`, which is what clears a warning left over from
            // whatever thread was open before — this one belongs to the
            // thread just opened, not to that.
            if let sentence = outcome.pretrust?.problemSentence {
                pretrustWarning = sentence
                pretrustWarningThreadID = provisional.threadID
            }
            return true
        } catch {
            let failure = (error as? DeckError) ?? .transport(error.localizedDescription)
            current.isCreating = false
            current.problem = failure.userFacingText
            pending = current
            return false
        }
    }

    /// The desk renamed itself — which happens while the owner is reading its
    /// thread, because naming itself is the first thing it does.
    ///
    /// The name is the identity of everything: the row id, the thread id, the
    /// selection. Swapping it naively drops the selection and the open
    /// conversation goes with it. So the roster row is *replaced in place*, the
    /// selection is carried across, and the messages already on screen are left
    /// exactly where they are — no reload, because a reload is what loses his
    /// place in the transcript.
    public func applyRename(to agent: Agent, replacing oldName: String) async {
        let wasReading = selectedAgentName == oldName
        await rosterModel.rename(oldName, to: agent)
        await refreshAgents()
        await publishRoster()

        guard wasReading else { return }
        // Selection only. `thread` is deliberately untouched: the transcript is
        // the same transcript under a new name.
        //
        // Assigned only where something moved. The deck re-sends what it
        // already told us, and every one of these is `@Published` — a set that
        // changes nothing still rebuilds the whole conversation.
        if selectedAgentName != agent.name { selectedAgentName = agent.name }
        if selectedThreadID != agent.threadID { selectedThreadID = agent.threadID }
        if settingsAgent != agent {
            settingsAgent = agent
            settings = AgentSettingsModel(agent: agent, client: client)
        }
        // The frame carries the count from before he was watching — the desk
        // spoke, and what it said is the conversation already on his screen. A
        // badge on the row he is reading is a lie he has to click to clear.
        await rosterModel.markRead(agent: agent.name)
        await publishRoster()
        if case .loaded(var screen) = thread {
            let was = screen.presentation
            screen.presentation.threadID = agent.threadID
            screen.presentation.headerTitle = agent.displayName
            // The chip beside the name is part of the header, and leaving the
            // placeholder's there is how "New hire" outlives the new hire.
            screen.presentation.headerSubtitle = agent.title.isEmpty ? nil : agent.title
            if screen.presentation != was { thread = .loaded(screen) }
        }
    }

    /// **Go to the desk that is stopped**, by wire name.
    ///
    /// The last hop of the tray: a handed-over step tells him to sign into
    /// something, and the place he does that is the desk's own conversation and
    /// its screen. A desk that is not on the roster is ignored rather than
    /// clearing the selection — landing on an empty pane would be a worse
    /// answer than staying where he was.
    public func openDesk(named name: String) {
        guard let agent = agents[name] else { return }
        select(agent: agent.name, threadID: agent.threadID)
    }

    // MARK: the take-over

    /// What the desk he is looking at can be reached with. Empty on a deck
    /// that cannot serve a screen or a shell, so a surface that draws these
    /// draws nothing rather than a button that leads nowhere.
    public var takeoverOffers: [TakeoverOffer] {
        Takeover.offers(for: deskInView,
                        canServeScreen: screenClient != nil,
                        canServeShell: shellClient != nil)
    }

    /// **Take over the desk the conversation is open on.** The strip's route.
    public func takeOver(focus: TakeoverOffer.Focus) {
        guard let name = deskInView?.name else { return }
        takeOver(desk: name, focus: focus)
    }

    /// **Says one line to a desk in its own thread** — the take-over's "keep
    /// your hands off" and "handed back". True when the deck took it.
    public func tellDesk(_ desk: String, _ text: String) async -> Bool {
        (try? await client.send(threadID: "direct:\(desk)", text: text)) != nil
    }

    /// **Take over a named desk.** The inspector's and the thumbnail's route.
    ///
    /// Refuses anything this deck cannot actually serve, rather than opening a
    /// stage with nothing in it: the offer and the action are decided by the
    /// same function, so a button that is drawn always works and one that is
    /// not drawn cannot be reached by a stale keyboard shortcut either.
    public func takeOver(desk name: String, focus: TakeoverOffer.Focus) {
        guard let agent = agents[name] ?? (settingsAgent?.name == name ? settingsAgent : nil),
              Takeover.offers(for: agent, canServeScreen: screenClient != nil,
                              canServeShell: shellClient != nil)
                  .contains(where: { $0.focus == focus })
        else { return }
        let request = Takeover.request(for: agent, focus: focus)
        // `@Published` fires on every set, equal or not, and one store drives
        // every column of this window.
        if takeover != request { takeover = request }
    }

    /// Done, ⌘W, or the sheet being dismissed. There is no other way out and
    /// nothing else clears it — a roster refresh must not close a take-over he
    /// is in the middle of using.
    public func endTakeover() {
        if takeover != nil { takeover = nil }
    }

    /// **Watch a desk's computer in the main window.** Only a desk this deck
    /// can show a screen for; the sheet, if open, gives way (one place at a time).
    public func watch(desk name: String) {
        guard screenClient != nil,
              let agent = agents[name] ?? (settingsAgent?.name == name ? settingsAgent : nil)
        else { return }
        let request = Takeover.request(for: agent, focus: .screen)
        endTakeover()
        if watching != request { watching = request }
    }

    /// ⌘⇧W: watch the desk in view, or stop watching.
    public func toggleWatch() {
        if watching != nil { return endWatch() }
        guard let name = deskInView?.name else { return }
        watch(desk: name)
    }

    public func endWatch() {
        if watching != nil { watching = nil }
    }

    // MARK: approvals

    /// One `GET /v1/approvals` and one `GET /v1/handoffs` for the whole deck;
    /// the thread pane filters, the tray does not.
    ///
    /// Both, in one call, because they are one question to him — *is anything
    /// of mine stopped?* — and asking them on two clocks would put the two
    /// halves of the tray on screen at different moments for no reason he
    /// could see.
    public func loadApprovals() async {
        await approvalsModel.load()
        await handoffsModel.load()
        await publishApprovals()
    }

    /// **Starts the deck-wide watch, once.**
    ///
    /// Idempotent on purpose: `loadRoster()` runs on the window's `.task`, and
    /// again whenever the token changes, and a second timer per refresh would
    /// double the request rate every time he saved a token.
    ///
    /// `self` is re-checked on every pass rather than captured once, so a
    /// window that has gone leaves nothing waking up to redial a deck nobody is
    /// looking at.
    public func watchForAsks() {
        listenForPhoneSignIns()
        guard attentionPollTask == nil else { return }
        attentionPollTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let store = self else { return }
                await store.loadApprovals()
                await store.notifyOwner()
                await store.refreshUsage()
                await store.refreshStandingProposed()
                let interval = store.approvalPollInterval
                try? await Task.sleep(nanoseconds: UInt64(interval * 1_000_000_000))
            }
        }
    }

    /// The app came to the front: the plan meter may have changed while he was
    /// away (a second account signed in), so ask now, past the minute cache.
    func appBecameActive() async { await refreshUsage(force: true) }

    /// Asks for the meter when it is a minute old (or `force`). Optional by
    /// design: a failure keeps the last meter rather than flashing it away.
    func refreshUsage(force: Bool = false) async {
        guard let source = client as? ClaudeUsageSource else { return }
        if !force, let at = usageFetchedAt, Date().timeIntervalSince(at) < 60 { return }
        usageFetchedAt = Date()
        guard let next = try? await source.usage() else { return }
        // `@Published` fires on every set, equal or not.
        if next != usage { usage = next }
    }

    /// `POST /v1/agents/{name}/account`. Each refusal lands on the desk it was
    /// about, in its own words; a deck that cannot move says so.
    public func moveDesk(_ name: String, to account: String) async {
        guard let mover = client as? AccountMover else {
            accountMoves[name] = .failed(AccountMoveError.unsupported.userFacingText)
            return
        }
        accountMoves[name] = .moving
        do {
            try await mover.move(agent: name, to: account)
            accountMoves[name] = nil
            // The row's `account` and the meters' `desks` moved with it.
            await loadRoster()
            await refreshUsage(force: true)
        } catch let error as AccountMoveError {
            accountMoves[name] = .failed(error.userFacingText)
        } catch let error as DeckError {
            accountMoves[name] = .failed(error.userFacingText)
        } catch {
            accountMoves[name] = .failed(DeckError.transport(error.localizedDescription).userFacingText)
        }
    }

    public func dismissAccountMove(_ name: String) {
        accountMoves[name] = nil
    }

    // MARK: signing in to a Claude account

    /// The sign-in the sheet is showing, if any.
    @Published private(set) var accountSignIn: AccountSignInRequest?

    var accountLoginClient: AccountLoginClient? { client as? AccountLoginClient }

    /// The buttons show only on a deck that serves accounts and a client that
    /// can reach the login routes; otherwise they are not drawn at all.
    var canSignInToAccounts: Bool {
        accountLoginClient != nil && AccountSignIn.isOffered(usage: usage)
    }

    func beginSignIn(_ request: AccountSignInRequest) { accountSignIn = request }
    func endSignIn() { accountSignIn = nil }

    /// A sign-in finished: the accounts and meters are asked for again now.
    func accountSignedIn() async { await refreshUsage(force: true) }

    /// **Tells him, on this Mac, what a desk needs him for or said to him.**
    /// No-op outside the app bundle (tests, `swift run`) and for a client that
    /// is not the HTTP one. The thread on screen does not banner while the
    /// app is frontmost; a tap opens the desk's thread.
    func notifyOwner() async {
        guard OwnerNotifier.isAvailable, let http = client as? HTTPDeckClient else { return }
        if ownerRouter == nil {
            let router = OwnerNotificationRouter(
                openThread: { [weak self] in NSApplication.shared.isActive ? self?.selectedThreadID : nil },
                onRoute: { [weak self] route in
                    NSApplication.shared.activate(ignoringOtherApps: true)
                    self?.select(agent: route.agent, threadID: route.threadID)
                })
            // Reply typed on the banner: sent as a quote-reply without
            // opening anything. A refusal opens the thread with his words in
            // the box, never drops them.
            router.onReply = { @MainActor [weak self] reply in
                do {
                    _ = try await http.send(threadID: reply.threadID, text: reply.text, replyTo: reply.replyTo)
                } catch {
                    NSApplication.shared.activate(ignoringOtherApps: true)
                    self?.select(agent: ThreadID.desk(ofDirect: reply.threadID) ?? "", threadID: reply.threadID)
                    self?.composerDraft = reply.text
                }
            }
            // Approve / No on a desk's proposal: answered without opening
            // anything; a refusal opens Standing approvals, where it is said.
            router.onStanding = { @MainActor [weak self] answer in
                if await answer.send(via: http) != nil { self?.showStanding() }
                await self?.refreshStandingProposed(force: true)
            }
            router.install()
            ownerRouter = router
            await OwnerNotifier.requestPermission()
        }
        ownerNotifier.openThread = NSApplication.shared.isActive ? selectedThreadID : nil
        // He is at this Mac: the app in front and the keyboard or mouse used
        // in the last two minutes. The deck then holds pushes everywhere.
        let idle = CGEventSource.secondsSinceLastEventType(.combinedSessionState,
                                                           eventType: CGEventType(rawValue: ~0)!)
        let active = NSApplication.shared.isActive && idle < 120
        await ownerNotifier.poll { [weak self] since in
            let page = try await http.ownerAlerts(since: since, active: active)
            await self?.takeRinging(page.ringing)
            return page
        }
        // A poll that missed the deck still lets a ring run out on screen.
        takeRinging(ringing)
    }

    // MARK: a desk calling him

    /// The incoming call drawn over the window, or nil. A tap on the ring's
    /// notification opens the desk's thread (`onRoute` above) and this card is
    /// already up for as long as the deck says it rings.
    @Published public private(set) var incomingRing: IncomingRing?
    /// Why his Decline did not land, in one sentence on the card.
    @Published public private(set) var incomingCallProblem: String?
    private var ringScreen = IncomingCallPresenter()
    private var ringing: [IncomingRing] = []

    /// Every alerts page lists what is ringing; the card follows it.
    func takeRinging(_ rings: [IncomingRing], now: Date = Date()) {
        ringing = rings
        let shown = ringScreen.update(rings, now: now)
        guard shown != incomingRing else { return }
        if shown?.id != incomingRing?.id { incomingCallProblem = nil }
        incomingRing = shown
    }

    /// **Answer**: the desk's thread opens and the SAME call as the header's
    /// Call starts on it, through the app's one `VoiceSession`, by answering
    /// the ring. The session is pointed at the desk with the thread's real
    /// transcript, so what was already said is not read out as news.
    func answer(_ ring: IncomingRing, on session: VoiceSession) async {
        ringScreen.handle(ring.id)
        incomingRing = nil
        incomingCallProblem = nil
        select(agent: ring.agent, threadID: ring.threadID)
        var screen: ThreadScreen?
        for _ in 0..<100 {   // up to 5 s for the thread to load
            if case .loaded(let open) = thread, open.presentation.threadID == ring.threadID { screen = open; break }
            if case .failed = thread { break }
            try? await Task.sleep(nanoseconds: 50_000_000)
        }
        let agent = agents[ring.agent]
        session.sync(desk: ring.agent,
                     displayName: screen?.presentation.headerTitle ?? Agent.displayName(forWireName: ring.agent),
                     voice: agent?.voice, working: agent?.state == .working, messages: screen?.messages ?? [])
        await session.answerCall(ringID: ring.id)
    }

    /// **Decline**: the deck posts the desk's reason to his thread. Refused,
    /// the card stays up and says why.
    func decline(_ ring: IncomingRing) async {
        guard let calls = incomingCallClient else {
            incomingCallProblem = "This deck can't take calls from desks yet."
            return
        }
        do {
            try await calls.declineRing(id: ring.id)
            ringScreen.handle(ring.id)
            if incomingRing?.id == ring.id { incomingRing = nil }
            incomingCallProblem = nil
        } catch {
            let why = (error as? DeckError)?.userFacingText ?? error.localizedDescription
            incomingCallProblem = "Couldn't decline: \(why)".replacingOccurrences(of: "\n", with: " ")
        }
    }

    /// Answers the card that was on screen with the option that was on screen —
    /// the rule the deck is asked to write is the one the user read.
    public func answer(card: ApprovalCard, with option: ApprovalOption) async {
        await answer(approvalID: card.approvalID, with: option)
    }

    /// **"Always, up to a limit…"** from the card on screen (§23). `nil` when
    /// the deck made the policies and let this one through; otherwise its reason.
    public func stand(card: ApprovalCard, with body: StandingFromCard) async -> String? {
        guard let approval = pendingApprovals.first(where: { $0.id == card.approvalID }) else {
            return DeckError.unknownAsk.userFacingText
        }
        guard let standing = client as? StandingCardClient else { return StandingPresentation.unavailable }
        let made = await approvalsModel.stand(approval: approval, with: body, via: standing)
        let problem = made ? nil : await approvalsModel.problem
        await publishApprovals()
        return problem
    }

    /// **The answer from the tray, sent down whichever route the thing came
    /// from.**
    ///
    /// The button carries a verb, never a whole option, and the verb is looked
    /// up against the ask the deck really sent. That matters twice over: a
    /// permission goes to `POST /v1/approvals/{id}` and reuses the one path
    /// that already knows about double-granting and already-answered
    /// questions, and a handed-over step goes to `POST /v1/handoffs/{id}`
    /// where `done` and `skipped` are different instructions to the desk.
    /// Sending one down the other's route would either grant a permission
    /// nobody asked for or tell a desk to abandon a job that is finished.
    public func answer(item: AttentionItem, with action: AttentionAction) async {
        switch action.verb {
        case .permission(let reply):
            guard let approval = pendingApprovals.first(where: { $0.id == item.askID }),
                  let option = approval.options.first(where: { $0.reply == reply })
            else { return }
            await answer(approvalID: item.askID, with: option)
        case .handoff(let outcome):
            guard let handoff = await handoffsModel.handoff(id: item.askID) else { return }
            await handoffsModel.resolve(handoff, as: outcome)
            // The whole publish, not just the tray: a refusal from the deck has
            // to reach the screen as words. Republishing only the list would
            // remove nothing and say nothing, which reads as a tap that did not
            // register rather than one the deck turned down.
            await publishApprovals()
        }
    }

    // MARK: use the login he already has

    /// **The primary action.** Reads the requested site's cookies from the
    /// browser he is already signed in with and shares them to every desk, then
    /// answers the card `done`. If he is not signed in to this site here, it
    /// offers the fresh passkey sign-in instead rather than failing.
    public func useMacLogin(_ item: AttentionItem) async {
        guard let macLogin, let host = item.signInOnMacURL?.host else { return }
        macSignIns[item.askID] = .importing
        do {
            let share = try await macLogin.run(site: host)
            macSignIns[item.askID] = .shared(desks: share.desks)
        } catch MacLoginImportError.notLoggedInHere {
            macSignIns[item.askID] = .offerPasskeyInstead(
                MacLoginImport.sentence(for: MacLoginImportError.notLoggedInHere))
            return
        } catch {
            macSignIns[item.askID] = .failed(MacLoginImport.sentence(for: error))
            return
        }
        if let handoff = await handoffsModel.handoff(id: item.askID) {
            await handoffsModel.resolve(handoff, as: .done)
        }
        await publishApprovals()
    }

    // MARK: sign in on this Mac (passkey, the fallback)

    /// Opens Chrome on this Mac at the card's site, in a throwaway profile, for
    /// him to sign in with his passkey. Nothing is shared yet.
    public func startMacSignIn(_ item: AttentionItem) async {
        guard let signIn, let url = item.signInOnMacURL else { return }
        do {
            try await signIn.start(id: item.askID, url: url)
            macSignIns[item.askID] = .waitingForYou
        } catch {
            macSignIns[item.askID] = .failed(MacSignIn.sentence(for: error))
        }
    }

    /// He says he is signed in: every desk gets the sign-in, Chrome closes,
    /// and the card is answered `done` so the desk goes back and checks.
    /// A refusal leaves the card up, with the reason on it.
    public func finishMacSignIn(_ item: AttentionItem) async {
        guard let signIn else { return }
        macSignIns[item.askID] = .sharing
        do {
            let share = try await signIn.finish(id: item.askID)
            macSignIns[item.askID] = .shared(desks: share.desks)
        } catch {
            macSignIns[item.askID] = .failed(MacSignIn.sentence(for: error))
            return
        }
        if let handoff = await handoffsModel.handoff(id: item.askID) {
            await handoffsModel.resolve(handoff, as: .done)
        }
        await publishApprovals()
    }

    public func cancelMacSignIn(_ item: AttentionItem) async {
        await signIn?.cancel(id: item.askID)
        macSignIns[item.askID] = nil
    }

    // MARK: the desk's own thread, and the phone asking this Mac

    /// The sign-in cards the open desk's thread draws inline — the same items,
    /// drawn by the same `SignInCardView`, as the side pane.
    public var threadSignIns: [AttentionItem] {
        AttentionItem.forThread(attention, desk: selectedAgentName)
    }

    /// One tap on a card's sign-in part, whichever card drew it.
    func signIn(_ item: AttentionItem, _ verb: MacSignInVerb) async {
        switch verb {
        case .useLogin: await useMacLogin(item)
        case .start: await startMacSignIn(item)
        case .share: await finishMacSignIn(item)
        case .cancel: await cancelMacSignIn(item)
        }
    }

    /// Whether a card may offer "Take over": this deck can serve a screen.
    public var canTakeOverScreens: Bool { screenClient != nil }

    /// **The phone's "sign in from my Mac", answered here.** Claims requests
    /// on a long-poll and runs the same sign-in the card's buttons run. Only
    /// in the app bundle: `swift test` never polls a deck or opens Chrome.
    public func listenForPhoneSignIns() {
        guard loginRequestTask == nil, OwnerNotifier.isAvailable,
              let requests = client as? LoginRequestClient else { return }
        let handler = MacLoginRequestHandler(
            requests: requests,
            importer: macLogin,
            resolveHandoff: { [weak self] id in
                await self?.resolveHandoffDone(id)
            },
            openPasskey: { [weak self] request in
                try await self?.openPasskeyForPhone(request)
            },
            notify: { text in await MacSignInNotice.post(text) })
        let node = MacNodeIdentity.describe().name
        loginRequestTask = Task { await handler.listen(node: node) }
    }

    private func resolveHandoffDone(_ id: String) async {
        await handoffsModel.load()
        if let handoff = await handoffsModel.handoff(id: id) {
            await handoffsModel.resolve(handoff, as: .done)
        }
        await publishApprovals()
    }

    private func openPasskeyForPhone(_ request: LoginRequest) async throws {
        await loadApprovals()
        guard let item = attention.first(where: { $0.askID == request.handoff }) else {
            throw SignInRefused("That sign-in card is no longer waiting.")
        }
        await startMacSignIn(item)
        if case .failed(let why)? = macSignIns[item.askID] {
            throw SignInRefused(why)
        }
    }

    /// Answered against the ask the deck actually sent — never against a copy
    /// rebuilt from the button — and only while it is still live here.
    private func answer(approvalID: String, with option: ApprovalOption) async {
        guard let approval = pendingApprovals.first(where: { $0.id == approvalID }) else {
            return
        }
        await approvalsModel.answer(approval: approval, with: option)
        await publishApprovals()
    }

    /// **Everything on this deck that is waiting on him, in one list.**
    ///
    /// Two routes, two kinds of stopped, one strip — because to him they are
    /// the same event. A permission question he can answer from the chair he is
    /// in; a handed-over step he has to go and do. Both are "something of mine
    /// is not moving until I act", and both were reaching him only on WhatsApp.
    ///
    /// Worked out before anything about the selection, and attributed
    /// afterwards: an ask the deck could not place matches no conversation and
    /// used to be dropped here. It is carried instead, labelled with what the
    /// deck did know.
    ///
    /// Oldest first across both, which is the deck's own ordering rule for
    /// handoffs and the right one for the pair: the oldest block is the one
    /// that has been costing the most.
    private func publishAttention() async {
        let items = AttentionItem.queue(
            approvals: await approvalsModel.waiting(),
            handoffs: await handoffsModel.waiting(),
            agents: agents)
        if attention != items { attention = items }
    }

    /// **Nothing is assigned here unless it actually changed.**
    ///
    /// This now runs on a timer, and `@Published` fires on every `set` whether
    /// or not the value moved. An unconditional assignment would tell SwiftUI
    /// to rebuild the whole thread pane once per poll for ever — and this pane
    /// sizes the transcript's `LazyVStack`, so every rebuild re-measures every
    /// row. That is the exact shape of the defect that put the owner's Mac at
    /// 99.3% CPU; see `LayoutSettlesTests`.
    private func publishApprovals() async {
        Diagnostics.count("store.publishApprovals")
        // Both halves of "something is stuck and you cannot see it" are said in
        // the same place. A handed-over step that arrived unreadable is a desk
        // sitting stopped exactly as an unreadable approval is, and counting one
        // out loud while swallowing the other would be arbitrary.
        let notice = [await approvalsModel.notice, await handoffsModel.notice]
            .compactMap { $0 }
        let said = notice.isEmpty ? nil : notice.joined(separator: " ")
        if approvalNotice != said { approvalNotice = said }
        // The approval route's failure wins when both failed: it is the one he
        // is most often waiting on, and two sentences stacked in one caption is
        // a wall rather than a message.
        let stuckProblem = await handoffsModel.problem
        let problem = await approvalsModel.problem ?? stuckProblem
        if approvalProblem != problem { approvalProblem = problem }

        // **Deck-wide, and worked out before anything about the selection.**
        pendingApprovals = await approvalsModel.waiting()
        await publishAttention()

        guard let selectedAgentName else {
            if !approvals.isEmpty { approvals = [] }
            republishEntries()
            return
        }
        let cards = await approvalsModel.entries(forAgent: selectedAgentName).map {
            ApprovalCard.make(
                approval: $0.approval,
                agents: agents,
                decision: $0.decision,
                settledElsewhere: $0.settledElsewhere,
                standing: $0.standing
            )
        }
        if approvals != cards { approvals = cards }
        republishEntries()
    }

    // MARK: settings panel

    public func setNotifications(_ enabled: Bool) async {
        guard let settings else { return }
        await settings.setNotifications(enabled)
        await absorbSettings(settings)
    }

    public func saveProfile(title: String, summary: String) async {
        guard let settings, let agent = settingsAgent else { return }
        // `name` is the identity every thread id and org edge is keyed on, so
        // it is shown but never sent. The charter goes back as it came.
        await settings.save(name: agent.name, title: title, detail: agent.detail,
                            summary: summary)
        await absorbSettings(settings)
    }

    private func absorbSettings(_ settings: AgentSettingsModel) async {
        settingsAgent = await settings.agent
        settingsError = await settings.lastError
        guard let updated = settingsAgent else { return }
        agents[updated.name] = updated
        await rosterModel.applyAgentUpdate(updated)
        await publishRoster()
    }

    deinit {
        syncTask?.cancel()
        updatesTask?.cancel()
        rosterFeedTask?.cancel()
        readTask?.cancel()
        attentionPollTask?.cancel()
        loginRequestTask?.cancel()
        // A window that has gone must not leave something waking up to redial
        // a deck nobody is looking at.
        reconnectTask?.cancel()
        if let credentialsObserver {
            NotificationCenter.default.removeObserver(credentialsObserver)
        }
    }
}

import SwiftUI
import DeckKit

/// **Everything the phone holds about the deck, in one place.**
///
/// The logic is DeckKit's — `RosterModel` builds the roster exactly as the Mac
/// sidebar does, `AttentionItem.queue` orders the asks exactly as the Mac
/// strip does. This type only owns the phone's lifecycle: connect, keep the
/// live stream open, poll what the stream does not carry, and act on his taps.
@MainActor
final class PhoneStore: ObservableObject {
    enum Phase: Equatable { case connect, live }

    @Published var phase: Phase
    @Published private(set) var snapshot: SidebarSnapshot = .empty
    @Published private(set) var agents: [String: Agent] = [:]
    @Published private(set) var attention: [AttentionItem] = []
    @Published private(set) var usage: ClaudeUsage?
    /// His Macs on this deck: "Your Mac" opens its live view (Mac control).
    @Published private(set) var macs: [MacNodeSummary] = []
    @Published private(set) var accountMoves: [String: AccountMoveStatus] = [:]
    @Published private(set) var connection: ConnectionState = .idle
    @Published private(set) var loadError: String?
    @Published private(set) var hasLoaded = false
    @Published var actionError: String?
    /// The phone's "sign in from my Mac", per card: what the Mac is doing.
    @Published private(set) var remoteSignIns: [String: RemoteSignInPhase] = [:]

    let deck: DeckConnection
    /// Set by the launch environment for demos and tests: nothing is sent,
    /// answered or marked read. The UI still draws every control.
    let isReadOnly: Bool
    let addressHint: String

    private(set) var client: DeckClient?
    private(set) var decisions: DecisionClient?
    /// K6 calls, over the same deck and token.
    var callClient: CallClient? { http }
    /// A desk calling him: decline, and who may call when. Not in the read-only demo.
    var incomingCallClient: IncomingCallClient? { isReadOnly ? nil : http }
    /// The desk's own machine (screen frames and input), same deck and token.
    var screenClient: AgentScreenClient? { http }
    /// A reply in the desk's call voice (`POST /v1/speech`), same deck and token.
    var speechClient: SpeechClient? { http }
    /// Voice messages: upload, transcription on the deck, playback.
    var voiceNoteClient: VoiceNoteClient? { isReadOnly ? nil : http }
    /// His screenshots and files: uploaded to the deck, and drawn back.
    var attachmentClient: HTTPDeckClient? { isReadOnly ? nil : http }
    /// Signing in to a Claude account. Offered only where the deck serves accounts.
    var accountLoginClient: AccountLoginClient? {
        isReadOnly || !AccountSignIn.isOffered(usage: usage) ? nil : http
    }
    /// The pictures he sent, fetched with his token for his bubbles.
    var attachmentImages: HTTPDeckClient? { http }
    /// A held video or audio clip, streamed with his token. One per deck:
    /// the asset's resource loader holds it weakly.
    var mediaLoader: DeckMediaLoader? {
        guard let http else { return nil }
        if let made = madeMediaLoader { return made }
        let made = DeckMediaLoader(client: http)
        madeMediaLoader = made
        return made
    }
    /// Dropped whenever `http` is replaced or cleared, so a re-pair never
    /// streams with the old deck's token.
    private var madeMediaLoader: DeckMediaLoader?
    /// Connectors & Skills (`/v1/store/*`), same deck and token.
    var storeClient: StoreClient? { http }
    private var http: HTTPDeckClient?
    private var roster: RosterModel?
    private var approvalsByID: [String: Approval] = [:]
    private var handoffsByID: [String: Handoff] = [:]
    private var tasks: [Task<Void, Never>] = []
    private var stream: Task<Void, Never>?
    private var firstLoad: Task<Void, Never>?
    private var refreshQueued = false
    private var wasInBackground = false
    /// One try of the first load, cut off here: a stalled socket (the tunnel
    /// waking, a deck mid-restart) must not hold "Checking in" for URLSession's 60s.
    static let firstLoadTimeout: TimeInterval = 12

    init(deck: DeckConnection = DeckConnection(), environment: [String: String] = ProcessInfo.processInfo.environment) {
        self.deck = deck
        isReadOnly = environment["DECK_READ_ONLY"] == "1"
        addressHint = environment["DECK_URL_HINT"] ?? environment["DECK_URL"] ?? ""
        #if DEBUG
        // Demo hand-off from the Mac: `SIMCTL_CHILD_DECK_URL` / `…_DECK_TOKEN`
        // on `simctl launch`. Saved like a hand-typed entry, never compiled in.
        if let url = environment["DECK_URL"], let token = environment["DECK_TOKEN"],
           let entry = try? ManualDeckEntry.parse(url: url, token: token) {
            try? deck.save(entry)
        }
        #endif
        phase = deck.saved == nil ? .connect : .live
    }

    /// For renders and previews: a store that already holds its state.
    init(preview snapshot: SidebarSnapshot, agents: [Agent], attention: [AttentionItem],
         usage: ClaudeUsage?, client: DeckClient? = nil, hasLoaded: Bool = true) {
        deck = DeckConnection(defaults: UserDefaults(suiteName: "preview")!, tokens: InMemoryTokenStore())
        isReadOnly = true
        addressHint = ""
        phase = .live
        self.snapshot = snapshot
        self.agents = Dictionary(agents.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        self.attention = attention
        self.usage = usage
        self.client = client
        connection = hasLoaded ? .live : .connecting
        self.hasLoaded = hasLoaded
    }

    var deckLabel: String {
        guard let saved = deck.saved else { return "" }
        return saved.name.isEmpty ? (saved.url.host ?? "") : saved.name
    }

    // MARK: connecting

    func start() {
        guard phase == .live, client == nil, let saved = deck.saved else { return }
        let http = HTTPDeckClient(baseURL: saved.url, tokens: StateSuite().tokenStore(),
                                  performer: deck.requestPerformer(), sse: deck.sseTransport(),
                                  // One /v1/stream for the roster, every open thread and a call:
                                  // each thread used to open its own (measured on the box).
                                  sharesOneStream: true)
        self.http = http
        madeMediaLoader = nil
        client = http
        decisions = http
        let roster = RosterModel(client: http)
        self.roster = roster
        connection = .connecting
        tasks = [Task { [weak self] in await self?.pollAttention() }]
        beginFirstLoad()
        restartStream()
    }

    /// The roster, tried until it answers: each try bounded, retried on a
    /// quick backoff. "Checking in" never hangs on one stalled request.
    private func beginFirstLoad() {
        guard let roster else { return }
        firstLoad?.cancel()
        firstLoad = Task { [weak self] in
            let loaded = await FirstLoad.untilLoaded(
                timeout: Self.firstLoadTimeout, backoff: ReconnectBackoff(),
                sleep: { try? await Task.sleep(nanoseconds: UInt64($0 * 1_000_000_000)) },
                onFailure: { error in
                    await MainActor.run {
                        self?.loadError = (error as? DeckError)?.userFacingText
                            ?? ((error is FirstLoad.TimedOut) ? "Your deck didn't answer in time." : error.localizedDescription)
                    }
                },
                load: { try await roster.load() })
            guard loaded, let self else { return }
            await self.publish(roster)
            self.loadError = nil
            self.hasLoaded = true
            await self.refreshAttention()
            await self.refreshUsage()
        }
    }

    private func restartStream() {
        stream?.cancel()
        stream = Task { [weak self] in await self?.followStream() }
    }

    /// "Retry", or the app back in front: a fresh socket now rather than
    /// waiting for a dead one to time out, and the roster again.
    func retryNow() {
        guard phase == .live, client != nil else { return }
        if connection != .live { connection = .connecting }
        // Replace the shared socket for every listener (open threads resync
        // from their cursors), then listen again without waiting the backoff.
        let http = self.http
        Task { [weak self] in
            await http?.reconnectStream()
            self?.restartStream()
        }
        if hasLoaded {
            Task { await refreshAll() }
        } else {
            beginFirstLoad()
        }
    }

    /// Scene phase: a socket that slept in the background is not trusted.
    func scene(_ phase: ScenePhase) {
        switch phase {
        case .background: wasInBackground = true
        case .active:
            if wasInBackground { wasInBackground = false; retryNow() }
        default: break
        }
    }

    func connect(code: String) async throws {
        _ = try await deck.pair(rawCode: code, device: UIDevice.current.name)
        phase = .live
        start()
    }

    func connect(address: String, token: String) async throws {
        let entry = try ManualDeckEntry.parse(url: address, token: token)
        // Prove it before keeping it: one read of the roster with this token.
        try await ConnectForm.probeRoster(entry)
        try deck.save(entry)
        phase = .live
        start()
    }

    func disconnect() {
        tasks.forEach { $0.cancel() }
        tasks = []
        stream?.cancel(); stream = nil
        firstLoad?.cancel(); firstLoad = nil
        try? deck.forget()
        client = nil; decisions = nil; http = nil; roster = nil; madeMediaLoader = nil
        snapshot = .empty; attention = []; usage = nil; accountMoves = [:]; hasLoaded = false
        connection = .idle
        phase = .connect
    }

    // MARK: keeping up

    func refreshAll() async {
        await refreshRoster()
        await refreshAttention()
        await refreshUsage()
        await refreshMacs()
    }

    func refreshMacs() async {
        guard let http else { return }
        // Optional: a deck without the bridge has no Macs to show.
        let next = (try? await http.macNodes()) ?? []
        if next != macs { macs = next }
    }

    func refreshRoster() async {
        guard let roster else { return }
        do {
            try await roster.load()
            await publish(roster)
            loadError = nil
        } catch {
            loadError = (error as? DeckError)?.userFacingText ?? error.localizedDescription
        }
        hasLoaded = true
    }

    private func publish(_ roster: RosterModel) async {
        let snap = await roster.snapshot
        let payload = await roster.payload
        if snap != snapshot { snapshot = snap }
        let byName = Dictionary(payload.agents.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        if byName != agents { agents = byName }
    }

    func refreshAttention() async {
        guard let client else { return }
        async let a = try? client.approvals()
        async let h = try? client.handoffs()
        let approvals = await a?.approvals ?? []
        let handoffs = await h?.handoffs ?? []
        approvalsByID = Dictionary(approvals.map { ($0.id, $0) }, uniquingKeysWith: { x, _ in x })
        handoffsByID = Dictionary(handoffs.map { ($0.id, $0) }, uniquingKeysWith: { x, _ in x })
        let items = AttentionItem.queue(approvals: approvals, handoffs: handoffs, agents: agents)
        if items != attention { attention = items }
    }

    func refreshUsage() async {
        guard let http else { return }
        // Optional by design: a deck without the route, or one that fails to
        // answer it, simply has no meter.
        let next = (try? await http.usage()) ?? nil
        if next != usage { usage = next }
    }

    /// "Move to account…": each refusal lands on the desk it was about, in the
    /// deck's own words; success leaves nothing (the badge is the proof).
    func moveDesk(_ name: String, to account: String) async {
        guard !isReadOnly else { actionError = "Read-only demo: nothing was sent."; Haptics.warning(); return }
        guard let http else { return }
        accountMoves[name] = .moving
        do {
            try await http.move(agent: name, to: account)
            accountMoves[name] = nil
            Haptics.tap()
            await refreshRoster()
            await refreshUsage()
        } catch let error as AccountMoveError {
            accountMoves[name] = .failed(error.userFacingText)
            Haptics.warning()
        } catch {
            accountMoves[name] = .failed(error.localizedDescription)
            Haptics.warning()
        }
    }

    func dismissAccountMove(_ name: String) { accountMoves[name] = nil }

    /// One stream for the roster: unread counts and desk states are applied in
    /// place; anything that changes previews asks for one coalesced reload.
    private func followStream() async {
        guard let client else { return }
        var backoff = ReconnectBackoff()
        while !Task.isCancelled {
            do {
                for try await event in client.events() {
                    backoff.reset()
                    if connection != .live {
                        connection = .live
                        // Back after a drop (a deck restart): what changed meanwhile.
                        if hasLoaded { queueRosterRefresh() } else { beginFirstLoadIfIdle() }
                    }
                    await apply(event)
                }
            } catch {}
            if Task.isCancelled { return }
            let wait = backoff.next()
            connection = .reconnecting(attempt: backoff.attempt)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
        }
    }

    /// The stream is live but the first load is still between tries: try now.
    private func beginFirstLoadIfIdle() {
        guard !hasLoaded else { return }
        beginFirstLoad()
    }

    private func apply(_ event: DeckEvent) async {
        guard let roster else { return }
        switch event {
        case .unread(let agent, let count):
            await roster.applyUnread(agent: agent, count: count)
            await publish(roster)
        case .agentState(let name, let state, let blocked):
            await roster.applyAgentState(name: name, state: state, blocked: blocked)
            await publish(roster)
        case .message, .agentRenamed, .hello:
            queueRosterRefresh()
        case .decision:
            queueRosterRefresh()
        case .heartbeat:
            break
        }
    }

    private func queueRosterRefresh() {
        guard !refreshQueued else { return }
        refreshQueued = true
        Task { [weak self] in
            try? await Task.sleep(nanoseconds: 600_000_000)
            guard let self else { return }
            self.refreshQueued = false
            await self.refreshRoster()
        }
    }

    /// Approvals and handoffs do not ride the stream; they are polled, as on
    /// the Mac. The usage meter moves slowly and is polled more slowly still.
    private func pollAttention() async {
        var tick = 0
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: 10_000_000_000)
            await refreshAttention()
            tick += 1
            if tick % 6 == 0 { await refreshUsage() }
        }
    }

    // MARK: his answers

    func act(_ action: AttentionAction, on item: AttentionItem) async {
        guard !isReadOnly else { actionError = "Read-only demo: nothing was sent."; Haptics.warning(); return }
        guard let client else { return }
        do {
            switch action.verb {
            case .permission(let reply):
                guard let approval = approvalsByID[item.askID],
                      let option = approval.options.first(where: { $0.reply == reply }) else { return }
                try await client.decideApproval(id: approval.id, option: option)
            case .handoff(let outcome):
                try await client.resolveHandoff(id: item.askID, outcome: outcome)
            }
            if case .permission(.never) = action.verb { Haptics.warning() } else { Haptics.success() }
            attention.removeAll { $0.id == item.id }
            await refreshAttention()
        } catch {
            Haptics.failure()
            actionError = (error as? DeckError)?.userFacingText ?? error.localizedDescription
        }
    }

    /// Whether this deck can pass a sign-in request to the Mac.
    var canAskMac: Bool { !isReadOnly && http != nil }

    /// **"Use my Mac's Chrome login" / "Sign in fresh with passkey on my Mac".**
    /// Files the request and follows it; the card shows each step. Cookies go
    /// from the Mac to the desks and never come here.
    func askMac(_ method: LoginMethod, for item: AttentionItem) async {
        guard canAskMac, let http else { return }
        if remoteSignIns[item.askID]?.isWorking == true { return }
        Haptics.tap()
        let end = await PhoneSignIn(client: http).run(item: item, method: method) { [weak self] phase in
            self?.remoteSignIns[item.askID] = phase
        }
        switch end {
        case .signedIn: Haptics.success(); await refreshAttention()
        case .finishOnMac: Haptics.success()
        default: Haptics.failure()
        }
    }

    func markRead(agent: String) {
        guard !isReadOnly, let client else { return }
        Task { try? await client.markRead(agent: agent, upTo: nil) }
    }

    func row(forAgent name: String) -> SidebarRow? {
        snapshot.allRows.first { $0.agent.name == name }
    }
}

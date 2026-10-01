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
    @Published private(set) var connection: ConnectionState = .idle
    @Published private(set) var loadError: String?
    @Published private(set) var hasLoaded = false
    @Published var actionError: String?

    let deck: DeckConnection
    /// Set by the launch environment for demos and tests: nothing is sent,
    /// answered or marked read. The UI still draws every control.
    let isReadOnly: Bool
    let addressHint: String

    private(set) var client: DeckClient?
    private(set) var decisions: DecisionClient?
    /// K6 calls, over the same deck and token.
    var callClient: CallClient? { http }
    /// The desk's own machine (screen frames and input), same deck and token.
    var screenClient: AgentScreenClient? { http }
    /// A reply in the desk's call voice (`POST /v1/speech`), same deck and token.
    var speechClient: SpeechClient? { http }
    /// Voice messages: upload, transcription on the deck, playback.
    var voiceNoteClient: VoiceNoteClient? { isReadOnly ? nil : http }
    /// Connectors & Skills (`/v1/store/*`), same deck and token.
    var storeClient: StoreClient? { http }
    private var http: HTTPDeckClient?
    private var roster: RosterModel?
    private var approvalsByID: [String: Approval] = [:]
    private var handoffsByID: [String: Handoff] = [:]
    private var tasks: [Task<Void, Never>] = []
    private var refreshQueued = false

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
         usage: ClaudeUsage?, client: DeckClient? = nil) {
        deck = DeckConnection(defaults: UserDefaults(suiteName: "preview")!, tokens: InMemoryTokenStore())
        isReadOnly = true
        addressHint = ""
        phase = .live
        self.snapshot = snapshot
        self.agents = Dictionary(agents.map { ($0.name, $0) }, uniquingKeysWith: { a, _ in a })
        self.attention = attention
        self.usage = usage
        self.client = client
        connection = .live
        hasLoaded = true
    }

    var deckLabel: String {
        guard let saved = deck.saved else { return "" }
        return saved.name.isEmpty ? (saved.url.host ?? "") : saved.name
    }

    // MARK: connecting

    func start() {
        guard phase == .live, client == nil, let saved = deck.saved else { return }
        let http = HTTPDeckClient(baseURL: saved.url, tokens: StateSuite().tokenStore(),
                                  performer: deck.requestPerformer(), sse: deck.sseTransport())
        self.http = http
        client = http
        decisions = http
        let roster = RosterModel(client: http)
        self.roster = roster
        connection = .connecting
        tasks = [
            Task { [weak self] in await self?.refreshAll() },
            Task { [weak self] in await self?.followStream() },
            Task { [weak self] in await self?.pollAttention() },
        ]
    }

    func connect(code: String) async throws {
        _ = try await deck.pair(rawCode: code, device: UIDevice.current.name)
        phase = .live
        start()
    }

    func connect(address: String, token: String) async throws {
        let entry = try ManualDeckEntry.parse(url: address, token: token)
        // Prove it before keeping it: one read of the roster with this token.
        let probe = HTTPDeckClient(baseURL: entry.url, tokens: InMemoryTokenStore(token: entry.token))
        _ = try await probe.roster()
        try deck.save(entry)
        phase = .live
        start()
    }

    func disconnect() {
        tasks.forEach { $0.cancel() }
        tasks = []
        try? deck.forget()
        client = nil; decisions = nil; http = nil; roster = nil
        snapshot = .empty; attention = []; usage = nil; hasLoaded = false
        connection = .idle
        phase = .connect
    }

    // MARK: keeping up

    func refreshAll() async {
        await refreshRoster()
        await refreshAttention()
        await refreshUsage()
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

    /// One stream for the roster: unread counts and desk states are applied in
    /// place; anything that changes previews asks for one coalesced reload.
    private func followStream() async {
        guard let client else { return }
        var attempt = 0
        while !Task.isCancelled {
            do {
                for try await event in client.events() {
                    attempt = 0
                    if connection != .live { connection = .live }
                    await apply(event)
                }
            } catch {}
            if Task.isCancelled { return }
            attempt += 1
            connection = .reconnecting(attempt: attempt)
            try? await Task.sleep(nanoseconds: UInt64(min(pow(2, Double(attempt - 1)), 30) * 1_000_000_000))
        }
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

    func markRead(agent: String) {
        guard !isReadOnly, let client else { return }
        Task { try? await client.markRead(agent: agent, upTo: nil) }
    }

    func row(forAgent name: String) -> SidebarRow? {
        snapshot.allRows.first { $0.agent.name == name }
    }
}

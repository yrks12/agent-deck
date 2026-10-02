import Foundation

/// The transcript of one thread, and the only place that decides what "already
/// have it" means.
///
/// Identity is the message id, ordering is the server's `seq`. A reopened
/// stream replays; a `since=` refetch overlaps at the cursor. Both are normal,
/// and both would put a line in twice if this appended blindly.
public struct ConversationStore: Sendable {
    public private(set) var messages: [Message] = []
    /// id -> index, so ingest is O(n) in what arrives, not O(n·m) in what is held.
    private var index: [String: Int] = [:]

    public init() {}

    /// The resume point handed straight back as `since=`. The array is kept in
    /// cursor order, so the newest held is the last one — never constructed.
    public var cursor: String? { messages.last?.cursor }

    public func contains(_ id: String) -> Bool { index[id] != nil }

    public struct IngestReport: Equatable, Sendable {
        public var inserted: Int
        public var duplicatesDropped: Int

        public init(inserted: Int, duplicatesDropped: Int) {
            self.inserted = inserted
            self.duplicatesDropped = duplicatesDropped
        }
    }

    @discardableResult
    public mutating func ingest(_ incoming: [Message]) -> IngestReport {
        var inserted = 0
        var duplicates = 0
        var needsSort = false

        for message in incoming {
            if let existing = index[message.id] {
                // Same line, seen again — an edit is worth taking, a re-delivery
                // is not worth a second bubble.
                if messages[existing].cursor != message.cursor { needsSort = true }
                messages[existing] = message
                duplicates += 1
            } else {
                messages.append(message)
                inserted += 1
            }
        }

        if inserted > 0 || needsSort {
            // Cursors are fixed-width and string-comparable by contract, so
            // lexical order *is* chronological order, and it is total: two
            // clients paging the same thread see the same sequence.
            messages.sort { ($0.cursor, $0.id) < ($1.cursor, $1.id) }
            reindex()
        }
        return IngestReport(inserted: inserted, duplicatesDropped: duplicates)
    }

    private mutating func reindex() {
        index.removeAll(keepingCapacity: true)
        for (position, message) in messages.enumerated() {
            index[message.id] = position
        }
    }
}

/// One snapshot of a thread, pushed to whoever is drawing it. The view layer
/// consumes these; it never reaches into the sync actor to poll.
public struct SyncUpdate: Sendable {
    public var messages: [Message]
    public var connection: ConnectionState
    public var isReadOnly: Bool
    public var participants: [String]

    public init(
        messages: [Message],
        connection: ConnectionState,
        isReadOnly: Bool,
        participants: [String]
    ) {
        self.messages = messages
        self.connection = connection
        self.isReadOnly = isReadOnly
        self.participants = participants
    }
}

/// Keeps one thread in step with the deck: catch up from the cursor, then watch
/// the stream, and when the stream dies do exactly that again.
///
/// The catch-up sits at the *top* of the loop rather than in an error handler,
/// which is what makes the first connection and every reconnection the same
/// code path — there is no second, less-tested way to fill the gap.
public actor ConversationSync {
    private let client: DeckClient
    /// **Not a `let`.** Every thread id is keyed on the desk's name, and a desk
    /// renames itself at the end of its interview — while the owner is reading
    /// this very thread. See `retarget(to:)`.
    private var threadID: String
    /// Ids this conversation used to have. A frame already in flight under the
    /// old id is the same conversation, and dropping it would lose the line the
    /// desk sent as it renamed.
    private var formerThreadIDs: Set<String> = []
    private let backoff: @Sendable (Int) async throws -> Void

    public private(set) var store = ConversationStore()
    /// Lines `accept` put in that no page or frame has carried. They must not
    /// set the resume point: a reconnect fetching `since=` his own sent line
    /// skips every desk line that landed before it while the stream was down.
    private var unconfirmed: Set<String> = []
    public private(set) var connection: ConnectionState = .idle
    /// Every state the connection has been in, in order. The indicator reads
    /// `connection`; this exists so a test can prove the drop was visible.
    public private(set) var connectionHistory: [ConnectionState] = []
    public private(set) var isReadOnly = false
    public private(set) var participants: [String] = []

    public init(
        client: DeckClient,
        threadID: String,
        backoff: @escaping @Sendable (Int) async throws -> Void = ConversationSync.defaultBackoff
    ) {
        self.client = client
        self.threadID = threadID
        self.backoff = backoff
    }

    /// `ReconnectBackoff`'s schedule: 0.5s, doubling, capped at 10s — a deck
    /// restarted by a deploy is found within ten seconds of answering. It was
    /// capped at 30s, the same rule the phone was fixed off.
    public static func backoffSeconds(attempt: Int) -> TimeInterval {
        min(ReconnectBackoff.cap, ReconnectBackoff.first * pow(2.0, Double(max(1, attempt) - 1)))
    }

    /// Injected so no test ever sleeps.
    public static let defaultBackoff: @Sendable (Int) async throws -> Void = { attempt in
        try await Task.sleep(nanoseconds: UInt64(backoffSeconds(attempt: attempt) * 1_000_000_000))
    }

    public func messages() -> [Message] { store.messages }

    /// Where a reconnect resumes: the newest line the deck itself delivered.
    private var resumeCursor: String? {
        unconfirmed.isEmpty ? store.cursor : store.messages.last { !unconfirmed.contains($0.id) }?.cursor
    }

    /// A line the deck confirmed outside the stream — its answer to a send.
    /// Held like a frame, so the next update cannot draw the thread without it.
    public func accept(_ message: Message) {
        if store.contains(message.id) == false { unconfirmed.insert(message.id) }
        store.ingest([message])
        publish()
    }

    /// The thread this sync is following, which is not necessarily the one it
    /// was opened with.
    public var currentThreadID: String { threadID }

    /// **Follow a desk that has renamed itself, without losing the
    /// conversation.**
    ///
    /// The transcript already held is kept exactly as it is — a rename changes
    /// what the desk is called and nothing about what was said — so there is no
    /// reload, no `.loading`, and no lost scroll position. Only the id used to
    /// route frames and to fetch the next page changes.
    ///
    /// Done **inside the actor, on the frame itself**, rather than by the store
    /// calling back in. The deck sends the rename and then the desk's first
    /// message under the new id on the same connection, microseconds apart; a
    /// retarget that had to round-trip through the main actor first would drop
    /// that message on the floor.
    public func retarget(to newThreadID: String) {
        guard newThreadID != threadID, !newThreadID.isEmpty else { return }
        formerThreadIDs.insert(threadID)
        threadID = newThreadID
    }

    private var observers: [UUID: AsyncStream<SyncUpdate>.Continuation] = [:]
    private var eventObservers: [UUID: AsyncStream<DeckEvent>.Continuation] = [:]

    /// Frames that are not about this thread — unread counts, agent states, the
    /// `hello` snapshot.
    ///
    /// The deck runs **one** `/v1/stream` and fans everything down it, so the
    /// client opens one connection too. Whoever holds the open thread also
    /// carries the sidebar's traffic rather than a second socket being opened
    /// for it.
    public func deckEvents() -> AsyncStream<DeckEvent> {
        AsyncStream { continuation in
            let key = UUID()
            eventObservers[key] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeEventObserver(key) }
            }
        }
    }

    private func removeEventObserver(_ key: UUID) {
        eventObservers[key] = nil
    }

    /// A stream of snapshots for the view layer. Call it before `run`; it closes
    /// when `run` returns, so a closed thread view cannot leak a task.
    public func updates() -> AsyncStream<SyncUpdate> {
        AsyncStream { continuation in
            let key = UUID()
            observers[key] = continuation
            continuation.onTermination = { [weak self] _ in
                Task { await self?.removeObserver(key) }
            }
        }
    }

    private func removeObserver(_ key: UUID) {
        observers[key] = nil
    }

    private func publish() {
        Diagnostics.count("sync.publish")
        let update = SyncUpdate(
            messages: store.messages,
            connection: connection,
            isReadOnly: isReadOnly,
            participants: participants
        )
        for continuation in observers.values { continuation.yield(update) }
    }

    private func closeObservers() {
        for continuation in observers.values { continuation.finish() }
        observers.removeAll()
        for continuation in eventObservers.values { continuation.finish() }
        eventObservers.removeAll()
    }

    /// Runs until the feed ends cleanly, the task is cancelled, or the reconnect
    /// budget runs out.
    public func run(maxReconnects: Int = .max) async throws {
        defer { closeObservers() }
        var attempt = 0

        while !Task.isCancelled {
            Diagnostics.count("sync.connectAttempt")
            move(to: .connecting)
            // Listening BEFORE the fetch: a line said while the page is in
            // flight is in neither the page nor a stream opened after it, so it
            // stayed missing until he reopened the chat. The stream buffers it;
            // the overlap with the page is deduplicated by id.
            let events = client.events()
            // Resume from what we hold. `nil` on the first pass means "the whole
            // thread"; after a drop it is the cursor, so the gap is filled and
            // the overlap is deduplicated by the store.
            let page = try await client.messages(threadID: threadID, since: resumeCursor)
            store.ingest(page.messages)
            unconfirmed.subtract(page.messages.map(\.id))
            isReadOnly = page.isReadOnly
            participants = page.participants
            move(to: .live)
            publish()

            do {
                for try await event in events {
                    // Live again: the next drop starts the backoff from the
                    // beginning, not from wherever the last outage left it.
                    attempt = 0
                    apply(event)
                }
                return  // the deck closed the stream on purpose
            } catch is CancellationError {
                return
            } catch {
                attempt += 1
                move(to: .reconnecting(attempt: attempt))
                guard attempt <= maxReconnects else { throw error }
                try await backoff(attempt)
            }
        }
    }

    /// Whether a live frame belongs in the thread on screen.
    ///
    /// Three ways in, and the third is §6.2: this thread's own id, no id at
    /// all, or a `peer:` id one end of which is this direct thread's desk.
    /// Without the third, a dispatch that goes out while he is watching is
    /// dropped and the conversation stops moving until it is reopened — which
    /// is the whole experience being built. The `peer:` pairs this desk is not
    /// in stay out, so two reports talking to each other never enter their
    /// manager's chat.
    private func belongsHere(_ message: Message) -> Bool {
        if message.threadID == threadID || message.threadID.isEmpty { return true }
        if formerThreadIDs.contains(message.threadID) { return true }
        guard let desk = ThreadID.desk(ofDirect: threadID) else { return false }
        return ThreadID.peer(in: message.threadID, otherThan: desk) != nil
    }

    private func apply(_ event: DeckEvent) {
        Diagnostics.count("sync.event")
        switch event {
        case .message(let message, let frameReadOnly) where belongsHere(message):
            // The frame repeats read_only so it can be routed alone; trust it
            // over a stale page if they disagree — but only when the frame is
            // *about* this thread. A relayed line is a `peer:` frame and
            // `read_only: true`, and taking that would delete his composer
            // because somebody else's desk was mentioned in his conversation.
            if message.threadID == threadID || message.threadID.isEmpty
                || formerThreadIDs.contains(message.threadID) {
                isReadOnly = frameReadOnly
            }
            store.ingest([message])
            unconfirmed.remove(message.id)
            publish()
            // Forwarded as well as drawn. It is about this thread, but it is
            // also the only thing that can move that desk's `last_activity_by`
            // — §3.0 says the `agent_state` frame does not carry the field and
            // to take it from the newest message in the thread already open.
            // Consuming it here and telling nobody is how a desk that has just
            // answered him goes on reading "you spoke last".
            for continuation in eventObservers.values { continuation.yield(event) }
        case .heartbeat:
            // Proof of life only; the watchdog in the transport handles silence.
            break
        case .agentRenamed(let oldName, let agent):
            // Followed here first, then handed on. If this is our desk, every
            // frame after this one carries the new thread id, and the sidebar
            // still needs to hear about it either way.
            if ThreadID.desk(ofDirect: threadID) == oldName {
                retarget(to: agent.threadID)
                publish()
            }
            for continuation in eventObservers.values { continuation.yield(event) }
        case .message, .hello, .agentState, .unread, .decision:
            // Not about this thread — hand it to whoever is drawing the sidebar.
            for continuation in eventObservers.values { continuation.yield(event) }
        }
    }

    private func move(to state: ConnectionState) {
        guard connection != state else { return }
        Diagnostics.count("sync.connectionChange")
        connection = state
        connectionHistory.append(state)
        // A dropped stream must reach the indicator immediately, not at the end
        // of the next successful fetch.
        if case .reconnecting = state { publish() }
    }
}

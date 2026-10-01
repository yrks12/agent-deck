import SwiftUI
import DeckKit

/// **One open conversation.** `ConversationSync` (DeckKit) fetches, streams,
/// resumes from the cursor after a drop and de-duplicates; the drawing order —
/// day breaks, captions, relayed-traffic rollups, the face under a turn — is
/// `ThreadTimeline.items`, the same walk the Mac transcript does.
@MainActor
final class ThreadModel: ObservableObject {
    @Published private(set) var items: [TranscriptItem] = []
    @Published private(set) var connection: ConnectionState = .idle
    @Published private(set) var isReadOnlyThread = false
    @Published private(set) var loaded = false
    @Published private(set) var sending = false
    @Published var error: String?
    @Published var expandedRollups: Set<String> = []

    let threadID: String
    let desk: String
    private let client: DeckClient?
    private let decisions: DecisionClient?
    private let demoReadOnly: Bool
    private var messages: [Message] = []
    private var overrides: [String: Decision] = [:]
    private var newFrom: String?
    private var unreadAtOpen: Int
    private var tasks: [Task<Void, Never>] = []
    private var displayName: (String) -> String
    /// Every change to the conversation, for the reply speaker.
    var onMessages: ((String, [Message]) -> Void)?

    init(threadID: String, client: DeckClient?, decisions: DecisionClient?, demoReadOnly: Bool,
         unread: Int = 0, displayName: @escaping (String) -> String = { Agent.displayName(forWireName: $0) }) {
        self.threadID = threadID
        self.desk = ThreadID.desk(ofDirect: threadID) ?? ""
        self.client = client
        self.decisions = decisions
        self.demoReadOnly = demoReadOnly
        self.unreadAtOpen = unread
        self.displayName = displayName
    }

    /// For renders: a thread that already holds its messages.
    convenience init(preview threadID: String, messages: [Message]) {
        self.init(threadID: threadID, client: nil, decisions: nil, demoReadOnly: true)
        self.messages = messages
        connection = .live
        loaded = true
        rebuild()
    }

    func start() {
        guard tasks.isEmpty, let client else { return }
        let sync = ConversationSync(client: client, threadID: threadID)
        tasks = [
            Task { [weak self] in
                for await update in await sync.updates() {
                    self?.apply(update)
                }
            },
            Task { [weak self] in
                do { try await sync.run() } catch {
                    self?.error = (error as? DeckError)?.userFacingText ?? error.localizedDescription
                }
            },
        ]
    }

    func stop() {
        tasks.forEach { $0.cancel() }
        tasks = []
    }

    private func apply(_ update: SyncUpdate) {
        messages = update.messages
        connection = update.connection
        isReadOnlyThread = update.isReadOnly
        if newFrom == nil, unreadAtOpen > 0, !messages.isEmpty {
            newFrom = ThreadTimeline.firstUnread(in: messages, unread: unreadAtOpen)
            unreadAtOpen = 0
        }
        loaded = true
        rebuild()
        onMessages?(desk, messages)
    }

    /// His voice message was transcribed and queued: draw it now.
    func voiceNoteSent(_ sent: Message) {
        if !messages.contains(where: { $0.id == sent.id }) {
            messages.append(sent)
            rebuild()
        }
        Haptics.tap()
    }

    private func rebuild() {
        let settled = ThreadTimeline.settleDecisions(messages, overrides: overrides)
        let rows = Transcript.rows(settled, page: threadID, desk: desk, displayName: displayName)
        let entries = ThreadTimeline.entries(rows: rows, toolCalls: [])
        let drawn = ThreadTimeline.items(entries, desk: desk, newFrom: newFrom).flatMap { item -> [TranscriptItem] in
            if case .rollup(let rollup) = item, expandedRollups.contains(rollup.id) {
                return [item] + ThreadTimeline.expanded(rollup, desk: desk)
            }
            return [item]
        }
        if drawn != items { items = drawn }
    }

    func toggle(rollup id: String) {
        if expandedRollups.contains(id) { expandedRollups.remove(id) } else { expandedRollups.insert(id) }
        rebuild()
    }

    // MARK: his side

    var canSend: Bool { !isReadOnlyThread && client != nil }

    func send(_ text: String) async -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return false }
        guard !demoReadOnly else { error = "Read-only demo: nothing was sent."; Haptics.warning(); return false }
        guard let client else { return false }
        sending = true
        defer { sending = false }
        do {
            let sent = try await client.send(threadID: threadID, text: trimmed)
            if !messages.contains(where: { $0.id == sent.id }) {
                messages.append(sent)
                rebuild()
            }
            Haptics.tap()
            return true
        } catch {
            Haptics.failure()
            self.error = (error as? DeckError)?.userFacingText ?? error.localizedDescription
            return false
        }
    }

    func answer(_ decision: Decision, value: String) async {
        guard !demoReadOnly else { error = "Read-only demo: nothing was answered."; Haptics.warning(); return }
        guard let decisions else { return }
        do {
            try await decisions.answerDecision(id: decision.id, value: value)
            var settled = decision
            settled.state = .answered
            settled.answer = value
            overrides[decision.id] = settled
            rebuild()
            Haptics.success()
        } catch {
            Haptics.failure()
            self.error = (error as? DeckError)?.userFacingText ?? error.localizedDescription
        }
    }
}

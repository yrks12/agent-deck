import SwiftUI
import Combine
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
    /// The message he is answering: the strip above the composer.
    @Published private(set) var replyDraft = ReplyDraft()
    /// The row a tapped quote scrolls to; a new value per tap.
    @Published private(set) var scrollTarget: QuoteJumpRequest?
    /// The deck holds lines older than the first one drawn.
    @Published private(set) var hasEarlier = false
    /// A page of older lines is on its way.
    @Published private(set) var loadingEarlier = false
    /// A tapped quote whose original is older than anything drawn: jumped to
    /// once the page holding it is drawn.
    private var pendingJump: MessageQuote?

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
    /// The open sync. A sent line goes into it too: every update replaces
    /// `messages`, so a line kept only here vanished at the next one.
    private var sync: ConversationSync?
    private var displayName: (String) -> String
    /// Every change to the conversation, for the reply speaker.
    var onMessages: ((String, [Message]) -> Void)?

    /// What he has attached and not sent yet; nil where nothing can be sent.
    let tray: AttachmentTray?

    init(threadID: String, client: DeckClient?, decisions: DecisionClient?, demoReadOnly: Bool,
         unread: Int = 0, uploader: AttachmentUploading? = nil,
         displayName: @escaping (String) -> String = { Agent.displayName(forWireName: $0) }) {
        self.threadID = threadID
        self.desk = ThreadID.desk(ofDirect: threadID) ?? ""
        self.client = client
        self.decisions = decisions
        self.demoReadOnly = demoReadOnly
        self.unreadAtOpen = unread
        self.displayName = displayName
        self.tray = uploader.map { AttachmentTray(threadID: threadID, uploader: $0) }
        // The send button reads the tray: an upload finishing must redraw it.
        trayWatch = tray?.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
    }
    private var trayWatch: AnyCancellable?

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
        self.sync = sync
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
        sync = nil
        loadingEarlier = false
        pendingJump = nil
    }

    /// "Show earlier messages": one page further back. It is drawn through the
    /// sync's update stream like every other line.
    func loadEarlier(until messageID: String? = nil) {
        guard let sync, !loadingEarlier else { return }
        loadingEarlier = true
        Task { [weak self] in
            if let messageID {
                _ = try? await sync.loadEarlier(until: messageID)
            } else {
                _ = try? await sync.loadEarlier()
            }
            self?.loadingEarlier = false
        }
    }

    private func apply(_ update: SyncUpdate) {
        messages = update.messages
        connection = update.connection
        isReadOnlyThread = update.isReadOnly
        if newFrom == nil, unreadAtOpen > 0, !messages.isEmpty {
            newFrom = ThreadTimeline.firstUnread(in: messages, unread: unreadAtOpen)
            unreadAtOpen = 0
        }
        hasEarlier = update.hasEarlier
        loaded = true
        rebuild()
        onMessages?(desk, messages)
        if let quote = pendingJump, messages.contains(where: { $0.id == quote.id }) {
            pendingJump = nil
            jump(to: quote)
        }
    }

    /// His voice message was transcribed and queued: draw it now.
    func voiceNoteSent(_ sent: Message) {
        if let sync { Task { await sync.accept(sent) } }
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

    /// His words, and the path of each attachment the deck now holds
    /// (`AttachmentLines`): the desk opens those paths with Read.
    // MARK: quote-replies

    /// A swipe right or the long-press menu's Reply.
    func beginReply(_ message: Message) {
        if replyDraft.begin(message, displayName: displayName) { Haptics.tap() }
    }

    func cancelReply() { replyDraft.cancel() }

    /// A tap on the quote in a bubble: open the rollup the original is folded
    /// into when it is, then ask the screen to scroll to its row.
    func jump(to quote: MessageQuote) {
        switch QuoteJump.destination(of: quote.id, in: items) {
        case .row(let row):
            scrollTarget = QuoteJumpRequest(messageID: row)
        case .insideRollup(let rollupID, let row):
            expandedRollups.insert(rollupID)
            rebuild()
            scrollTarget = QuoteJumpRequest(messageID: row)
        case .notLoaded:
            // Older than anything drawn: page back to it, then jump.
            guard hasEarlier else { Haptics.warning(); return }
            pendingJump = quote
            loadEarlier(until: quote.id)
        }
    }

    func send(_ text: String) async -> Bool {
        if let tray, !tray.isEmpty, !tray.isReady { return false }
        let trimmed = AttachmentLines.compose(text, tray?.uploaded ?? [])
        guard !trimmed.isEmpty else { return false }
        guard !demoReadOnly else { error = "Read-only demo: nothing was sent."; Haptics.warning(); return false }
        guard let client else { return false }
        sending = true
        defer { sending = false }
        do {
            let sent = try await client.send(threadID: threadID, text: trimmed,
                                             replyTo: replyDraft.replyToID)
            tray?.clear()
            replyDraft.cancel()
            await sync?.accept(sent)
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

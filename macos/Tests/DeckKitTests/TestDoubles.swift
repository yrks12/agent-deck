import AppKit
import Foundation
import XCTest
@testable import DeckKit

/// A `DeckClient` whose every answer is scripted and whose every call is
/// recorded. No sockets, no timers, no sleeps: the tests here are about
/// ordering and bookkeeping, and both are decidable from the call log alone.
///
/// **Every mutable field here is behind `lock`, and that is not decoration.**
/// This is `@unchecked Sendable`, and it used to be safe only by accident: the
/// app made one client call at a time, from one thread, so an unguarded
/// `calls.append` never overlapped with anything. The moment the store began
/// polling `/v1/approvals` on its own task, two cooperative threads reached
/// this object at once — `ApprovalsModel`'s actor executor on one side and the
/// conversation sync on the other — and `swift test` started dying with
/// `EXC_BAD_ACCESS` inside `Array.append` under `ScriptedDeckClient.approvals()`,
/// intermittently, *after* reporting every test green. A double that is not
/// thread-safe cannot be used to test concurrency, and the concurrency is the
/// thing being tested.
final class ScriptedDeckClient: DeckClient, @unchecked Sendable {
    private let lock = NSLock()

    /// `rethrows`, so a scripted refusal is thrown from inside the critical
    /// section and the lock is still released by `defer`.
    private func locked<T>(_ body: () throws -> T) rethrows -> T {
        lock.lock()
        defer { lock.unlock() }
        return try body()
    }

    /// What `events()` should do on each successive open.
    enum FeedScript {
        /// Emit these, then fail — the stream dropping mid-conversation.
        case emitThenDrop([DeckEvent])
        /// Emit these, then end cleanly — the sync loop returns.
        case emitThenFinish([DeckEvent])
    }

    struct DropError: Error, Equatable {}

    private var _calls: [DeckCall] = []
    private var _rosterPayload = RosterPayload(agents: [], threads: [], sectionOrder: [])
    private var _rosterError: DeckError?
    private var _pages: [MessagePage] = []
    private var _feeds: [FeedScript] = []
    private var _sendResult: Message?
    private var _sendError: DeckError?
    private var _sentReplyTo: [String?] = []
    private var _routinesToReturn: [Routine] = []
    private var _routinesError: DeckError?
    private var _routineToReturn: Routine?
    private var _routineDrafts: [RoutineDraft] = []
    private var _createError: DeckError?
    private var _createdDrafts: [AgentDraft] = []
    private var _interviewResult: Agent?
    private var _interviewPretrust: PretrustOutcome?
    private var _interviewError: DeckError?
    private var _interviewDrafts: [InterviewDraft] = []
    private var _onInterview: (@Sendable () async -> Void)?
    private var _approvalsPage = ApprovalsPage(approvals: [], unreadable: 0)
    private var _approvalsError: DeckError?
    private var _decideError: DeckError?
    private var _decision: ApprovalDecision?
    private var _decidedApprovals: [(String, String)] = []
    private var _handoffsPage = HandoffsPage(handoffs: [], unreadable: 0)
    private var _handoffsError: DeckError?
    private var _resolveHandoffError: DeckError?
    private var _resolvedHandoffs: [(String, String)] = []
    private var _moveError: AccountMoveError?
    private var _moves: [(String, String)] = []
    private var _accountList: [DeckAccount]? = []

    /// What `move(agent:to:)` refuses with; `nil` moves.
    var moveError: AccountMoveError? {
        get { locked { _moveError } }
        set { locked { _moveError = newValue } }
    }
    /// Every `(agent, account)` the client was asked to move.
    var moves: [(agent: String, account: String)] { locked { _moves.map { (agent: $0.0, account: $0.1) } } }
    /// `GET /v1/accounts`; `nil` is a deck without the route.
    var accountList: [DeckAccount]? {
        get { locked { _accountList } }
        set { locked { _accountList = newValue } }
    }
    func recordMove(agent: String, to account: String) throws {
        try locked {
            if let error = _moveError { throw error }
            _moves.append((agent, account))
        }
    }

    var calls: [DeckCall] { locked { _calls } }
    var rosterPayload: RosterPayload {
        get { locked { _rosterPayload } }
        set { locked { _rosterPayload = newValue } }
    }
    var rosterError: DeckError? {
        get { locked { _rosterError } }
        set { locked { _rosterError = newValue } }
    }
    /// Answers for `messages(threadID:since:limit:)`, consumed front to back.
    var pages: [MessagePage] {
        get { locked { _pages } }
        set { locked { _pages = newValue } }
    }
    /// Answers for `events()`, consumed front to back.
    var feeds: [FeedScript] {
        get { locked { _feeds } }
        set { locked { _feeds = newValue } }
    }
    /// `reply_to` of every send, in order; nil for a message that answers nothing.
    var sentReplyTo: [String?] { locked { _sentReplyTo } }
    var sendResult: Message? {
        get { locked { _sendResult } }
        set { locked { _sendResult = newValue } }
    }
    var sendError: DeckError? {
        get { locked { _sendError } }
        set { locked { _sendError = newValue } }
    }
    var routinesToReturn: [Routine] {
        get { locked { _routinesToReturn } }
        set { locked { _routinesToReturn = newValue } }
    }
    var routinesError: DeckError? {
        get { locked { _routinesError } }
        set { locked { _routinesError = newValue } }
    }
    var routineToReturn: Routine? {
        get { locked { _routineToReturn } }
        set { locked { _routineToReturn = newValue } }
    }
    var routineDrafts: [RoutineDraft] { locked { _routineDrafts } }
    var createError: DeckError? {
        get { locked { _createError } }
        set { locked { _createError = newValue } }
    }
    var createdDrafts: [AgentDraft] {
        get { locked { _createdDrafts } }
        set { locked { _createdDrafts = newValue } }
    }
    /// The provisional desk `POST /v1/agents/interview` answers with.
    var interviewResult: Agent? {
        get { locked { _interviewResult } }
        set { locked { _interviewResult = newValue } }
    }
    /// `nil` unless a test wants to script the create response's `pretrust`.
    var interviewPretrust: PretrustOutcome? {
        get { locked { _interviewPretrust } }
        set { locked { _interviewPretrust = newValue } }
    }
    var interviewError: DeckError? {
        get { locked { _interviewError } }
        set { locked { _interviewError = newValue } }
    }
    var interviewDrafts: [InterviewDraft] { locked { _interviewDrafts } }
    /// Run while the interview request is in flight, so a test can look at what
    /// the screen is doing *during* the create rather than after it.
    var onInterview: (@Sendable () async -> Void)? {
        get { locked { _onInterview } }
        set { locked { _onInterview = newValue } }
    }
    var approvalsPage: ApprovalsPage {
        get { locked { _approvalsPage } }
        set { locked { _approvalsPage = newValue } }
    }
    var approvalsError: DeckError? {
        get { locked { _approvalsError } }
        set { locked { _approvalsError = newValue } }
    }
    var decideError: DeckError? {
        get { locked { _decideError } }
        set { locked { _decideError = newValue } }
    }
    /// Overrides what `POST /v1/approvals/{id}` answers with. `nil` means the
    /// deck's own behaviour, derived from the option that was sent.
    var decision: ApprovalDecision? {
        get { locked { _decision } }
        set { locked { _decision = newValue } }
    }
    /// (approval id, decision) pairs, in the order they were answered.
    var decidedApprovals: [(String, String)] { locked { _decidedApprovals } }
    /// `GET /v1/handoffs` — every step on this deck that **no permission can
    /// lift**. Scripted here rather than inherited: `DeckClient` defaults this
    /// to an empty page so an older transport degrades instead of crashing,
    /// and a double sitting on that default answers "nothing is stuck"
    /// whatever the test set up. Every test that used this double was
    /// measuring the default rather than a deck.
    var handoffsPage: HandoffsPage {
        get { locked { _handoffsPage } }
        set { locked { _handoffsPage = newValue } }
    }
    var handoffsError: DeckError? {
        get { locked { _handoffsError } }
        set { locked { _handoffsError = newValue } }
    }
    var resolveHandoffError: DeckError? {
        get { locked { _resolveHandoffError } }
        set { locked { _resolveHandoffError = newValue } }
    }
    /// (handoff id, outcome) pairs. `done` and `skipped` are different
    /// instructions to the desk, so which one travelled is the fact worth
    /// keeping.
    var resolvedHandoffs: [(String, String)] { locked { _resolvedHandoffs } }

    private func record(_ call: DeckCall) { locked { _calls.append(call) } }

    /// The next roster calls stall this long first: a socket from before a
    /// deck restart, or a VPN tunnel still waking.
    var rosterStallsOnce: TimeInterval {
        get { locked { _rosterStall } }
        set { locked { _rosterStall = newValue } }
    }
    private var _rosterStall: TimeInterval = 0

    func roster() async throws -> RosterPayload {
        record(.roster)
        let stall = locked { () -> TimeInterval in defer { _rosterStall = 0 }; return _rosterStall }
        if stall > 0 { try await Task.sleep(nanoseconds: UInt64(stall * 1_000_000_000)) }
        return try locked {
            if let error = _rosterError { throw error }
            return _rosterPayload
        }
    }

    func threads() async throws -> [ThreadSummary] {
        record(.threads)
        return locked { _rosterPayload.threads }
    }

    func agent(named name: String) async throws -> Agent {
        record(.agent(name))
        return try locked {
            guard let found = _rosterPayload.agents.first(where: { $0.name == name }) else {
                throw DeckError.unknownAgent
            }
            return found
        }
    }

    func updateAgent(_ agent: Agent) async throws -> Agent {
        record(.updateAgent(agent.name))
        return agent
    }

    func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
        record(.messages(threadID: threadID, since: since))
        return try locked {
            guard !_pages.isEmpty else { throw DeckError.unknownThread }
            return _pages.removeFirst()
        }
    }

    func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
        record(.send(threadID: threadID))
        return try locked {
            _sentReplyTo.append(replyTo)
            if let error = _sendError { throw error }
            guard var result = _sendResult else { throw DeckError.unknownThread }
            // The deck stores what it was sent and answers with that line.
            result.text = text
            result.author = DeckOwner.name
            result.role = .owner
            return result
        }
    }

    func markRead(agent: String, upTo: String?) async throws {
        record(.markRead(agent: agent))
    }

    func routines() async throws -> [Routine] {
        record(.routines)
        return try locked {
            if let error = _routinesError { throw error }
            return _routinesToReturn
        }
    }

    func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
        record(.createRoutine(agent: draft.agentName))
        return try locked {
            if let error = _routinesError { throw error }
            _routineDrafts.append(draft)
            guard let made = _routineToReturn else {
                throw DeckError.http(500, reason: "no scripted routine")
            }
            _routinesToReturn.append(made)
            return made
        }
    }

    func setRoutine(id: String, enabled: Bool) async throws -> Routine {
        record(.updateRoutine(id: id))
        return try locked {
            if let error = _routinesError { throw error }
            guard let made = _routineToReturn else {
                throw DeckError.http(500, reason: "no scripted routine")
            }
            return made
        }
    }

    func deleteRoutine(id: String) async throws {
        record(.deleteRoutine(id: id))
        try locked {
            if let error = _routinesError { throw error }
            _routinesToReturn.removeAll { $0.id == id }
        }
    }

    func createAgent(_ draft: AgentDraft) async throws -> Agent {
        record(.createAgent(draft.name))
        return try locked {
            if let error = _createError { throw error }
            _createdDrafts.append(draft)
            return Agent(name: draft.name, title: draft.title, detail: draft.detail)
        }
    }

    func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
        record(.startInterview)
        // Awaited OUTSIDE the lock: the hook is what a test uses to look at the
        // screen mid-flight, and holding a lock across a suspension point is
        // how a double deadlocks the thing it is standing in for.
        await onInterview?()
        return try locked {
            if let error = _interviewError { throw error }
            _interviewDrafts.append(draft)
            guard let made = _interviewResult else {
                throw DeckError.http(500, reason: "no scripted desk")
            }
            return InterviewOutcome(agent: made, pretrust: _interviewPretrust)
        }
    }

    func approvals() async throws -> ApprovalsPage {
        record(.approvals)
        return try locked {
            if let error = _approvalsError { throw error }
            return _approvalsPage
        }
    }

    @discardableResult
    func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision {
        record(.decideApproval(id: id))
        return try locked {
            if let error = _decideError { throw error }
            _decidedApprovals.append((id, option.reply.rawValue))
            // §12: `once` writes no rule, the other two write the one that was
            // on the button, and a refusal restarts nobody.
            return _decision ?? ApprovalDecision(
                answered: option.reply.rawValue,
                rule: option.rule,
                resumed: option.reply != .never
            )
        }
    }

    func handoffs() async throws -> HandoffsPage {
        record(.handoffs)
        return try locked {
            if let error = _handoffsError { throw error }
            return _handoffsPage
        }
    }

    @discardableResult
    func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution {
        record(.resolveHandoff(id: id))
        return try locked {
            if let error = _resolveHandoffError { throw error }
            _resolvedHandoffs.append((id, outcome.rawValue))
            _handoffsPage.handoffs.removeAll { $0.id == id }
            // The deck's own answer: resuming after a handoff is an ordinary
            // user message into the session, so it really was told.
            return HandoffResolution(outcome: "resolved", resumed: true)
        }
    }

    func events() -> AsyncThrowingStream<DeckEvent, Error> {
        let script = locked { _feeds.isEmpty ? FeedScript.emitThenFinish([]) : _feeds.removeFirst() }
        return AsyncThrowingStream { continuation in
            switch script {
            case .emitThenDrop(let events):
                events.forEach { continuation.yield($0) }
                continuation.finish(throwing: DropError())
            case .emitThenFinish(let events):
                events.forEach { continuation.yield($0) }
                continuation.finish()
            }
        }
    }

    /// The `since=` cursor handed to every history fetch, in order.
    var historyCursors: [String?] {
        calls.compactMap {
            if case .messages(_, let since) = $0 { return .some(since) }
            return nil
        }
    }

    var sendCallCount: Int {
        calls.filter { if case .send = $0 { return true } else { return false } }.count
    }
}

// MARK: - builders

/// Cursors are opaque to the client, so the tests use short sortable strings
/// rather than pretending to know the server's 18-digit format.
func makeMessage(
    _ id: String,
    cursor: String? = nil,
    text: String = "hello",
    thread: String = "t1",
    author: String = "hemingway",
    role: MessageRole = .agent,
    ts: TimeInterval = 1_700_000_000
) -> Message {
    Message(
        id: id,
        cursor: cursor ?? id,
        threadID: thread,
        author: author,
        role: role,
        sentAt: Date(timeIntervalSince1970: ts),
        text: text
    )
}

func makeAgent(_ name: String, section: String = "Work", title: String = "Researcher") -> Agent {
    Agent(name: name, title: title, detail: "", section: section)
}

func makeThread(
    _ id: String,
    agent: String,
    unread: Int = 0,
    at seconds: Double = 0,
    preview: ThreadPreview = ThreadPreview(text: "…"),
    readOnly: Bool = false,
    participants: [String]? = nil
) -> ThreadSummary {
    ThreadSummary(
        id: id,
        agentName: agent,
        participants: participants ?? [agent],
        isReadOnly: readOnly,
        unreadCount: unread,
        lastActivity: Date(timeIntervalSince1970: 1_700_000_000 + seconds),
        preview: preview
    )
}

/// A `RequestPerformer` that answers from a canned table and keeps every
/// request, so the HTTP adapter can be inspected without a network.
final class StubPerformer: RequestPerformer, @unchecked Sendable {
    private(set) var requests: [URLRequest] = []
    var status: Int = 200
    /// Keyed by path; falls back to `defaultBody`.
    var bodies: [String: Data] = [:]
    var defaultBody = Data("{}".utf8)
    /// Response headers every answer carries.
    var headers: [String: String]?

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        requests.append(request)
        let path = request.url?.path ?? ""
        let response = HTTPURLResponse(
            url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers
        )!
        return (bodies[path] ?? defaultBody, response)
    }

    var paths: [String] { requests.map { $0.url?.path ?? "" } }
    var authHeaders: [String?] { requests.map { $0.value(forHTTPHeaderField: "Authorization") } }
}

/// **The height a hosted pane actually laid out to**, measured on the view its
/// rows were placed in rather than on the height it *asks* for.
///
/// `fittingSize` was this suite's "did anything draw at all?" signal, and it
/// stopped being one the day a column was forbidden to over-report its height.
/// A `ScrollView` that correctly answers *"I will take whatever you give me"*
/// has an ideal height of **zero** — which reads exactly like a blank pane, and
/// turned two good tests red against a healthy app:
///
/// ```
/// XCTAssertGreaterThan failed: ("0.0") is not greater than ("100.0") —
/// the conversation … laid out to (165.5, 0.0) — that is a blank pane
/// ```
///
/// The scrolled content is the honest measurement: a conversation with rows in
/// it hands its clip view a document hundreds of points tall, and a pane that
/// drew nothing has no scroll view at all. Falls back to `fittingSize` for a
/// pane that legitimately has no scroller — an empty state is drawn without
/// one. See `ColumnHeight` and `AColumnFitsTheWindowItIsInTests`.
extension NSView {
    func laidOutSize() -> CGSize {
        var scrolled: CGSize?
        func walk(_ view: NSView) {
            if let document = (view as? NSClipView)?.documentView,
               document.frame.height > (scrolled?.height ?? 0) {
                scrolled = document.frame.size
            }
            view.subviews.forEach(walk)
        }
        walk(self)
        return scrolled ?? fittingSize
    }
}


extension ScriptedDeckClient: AccountMover {
    func accounts() async throws -> [DeckAccount]? { accountList }
    func move(agent: String, to account: String) async throws { try recordMove(agent: agent, to: account) }
}

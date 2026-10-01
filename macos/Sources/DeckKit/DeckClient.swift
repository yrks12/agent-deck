import Foundation

/// One call the client can make. The tests count these, which is how "the
/// sidebar costs one request" stays true as the code grows.
public enum DeckCall: Hashable, Sendable {
    case roster
    case threads
    case agent(String)
    case updateAgent(String)
    case messages(threadID: String, since: String?)
    case send(threadID: String)
    case markRead(agent: String)
    case approvals
    case decideApproval(id: String)
    case handoffs
    case resolveHandoff(id: String)
    case createAgent(String)
    case startInterview
    case routines
    case createRoutine(agent: String)
    case updateRoutine(id: String)
    case deleteRoutine(id: String)
}

/// A frame off `GET /v1/stream`. The deck sends no event names — the kind is a
/// `type` field inside the JSON.
public enum DeckEvent: Hashable, Sendable {
    /// Always the first frame: a full snapshot, so a fresh connection needs no
    /// second request.
    case hello(RosterSnapshot)
    /// `read_only` is repeated on every message frame so it can be routed
    /// without holding the thread list.
    case message(Message, readOnly: Bool)
    /// `blocked` is tri-state (`BlockedUpdate`), not a plain optional: the
    /// frame can omit the key entirely (`.unspecified` — an older deck, or a
    /// frame class that never carries it; leave whatever is already known
    /// alone), carry an explicit `null` (`.cleared` — the retraction), or
    /// carry an object (`.value`). See §8 and `BlockedUpdate`'s doc comment.
    case agentState(name: String, state: AgentState, blocked: BlockedUpdate)
    case unread(agent: String, count: Int)
    /// **A desk that has named itself.** `old_name` is the placeholder the
    /// interview hired; `agent` is the whole new row, so nothing has to be
    /// refetched to draw it.
    ///
    /// This is not a rare frame. "+" hires `new-hire-<hex>`, that desk asks
    /// what the job is, and renaming itself is how it says it knows — so every
    /// agent the owner creates sends one, at the exact moment he is reading its
    /// thread. Dropping it detaches that conversation: the name is the identity
    /// of the roster row, the thread id, the selection and every later
    /// `agent_state`, so a client that never hears this one frame spends the
    /// rest of the session watching a desk that no longer exists.
    case agentRenamed(oldName: String, agent: Agent)
    /// K4: a decision was asked, or its state changed (answered, skipped).
    case decision(threadID: String, decision: Decision)
    case heartbeat
}

/// The `hello` payload: §3 and §6 verbatim.
public struct RosterSnapshot: Hashable, Sendable {
    public var agents: [Agent]
    public var threads: [ThreadSummary]

    public init(agents: [Agent], threads: [ThreadSummary]) {
        self.agents = agents
        self.threads = threads
    }

    public func asRosterPayload() -> RosterPayload {
        var payload = AgentsResponse.roster(from: agents)
        // Peer threads only exist in the thread list, so fold them in.
        payload.threads.append(contentsOf: threads.filter { $0.kind == .peer })
        return payload
    }
}

/// How the app is currently attached to the deck. Drives the indicator in the
/// toolbar and nothing else.
public enum ConnectionState: Equatable, Sendable {
    case idle
    case connecting
    case live
    case reconnecting(attempt: Int)

    public var label: String {
        switch self {
        case .idle: return "Offline"
        case .connecting: return "Connecting"
        case .live: return "Connected"
        case .reconnecting: return "Reconnecting"
        }
    }

    public var isHealthy: Bool { self == .live }
}

/// The only way anything in this app reaches a deck.
///
/// Two implementations ship: `FixtureDeckClient` (canned, offline) and
/// `HTTPDeckClient` (the real `/v1` routes). Nothing above this protocol knows
/// which one it has.
public protocol DeckClient: Sendable {
    /// `GET /v1/agents` — the whole sidebar, one request. Row state (unread,
    /// preview, timestamp, thread id) rides on the agent row by design.
    func roster() async throws -> RosterPayload
    /// `GET /v1/threads` — only needed when the peer threads themselves are.
    func threads() async throws -> [ThreadSummary]
    /// `GET /v1/agents/{name}`
    func agent(named name: String) async throws -> Agent
    /// `PATCH /v1/agents/{name}`
    func updateAgent(_ agent: Agent) async throws -> Agent
    /// `GET /v1/threads/{id}/messages?since=&limit=`
    func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage
    /// `POST /v1/threads/{id}/messages`
    func send(threadID: String, text: String) async throws -> Message
    /// `POST /v1/agents/{name}/read`. `upTo` is a **message id**; nil means
    /// "everything I can currently see".
    func markRead(agent: String, upTo: String?) async throws
    /// `POST /v1/agents` — hire a desk. Refusals carry a `reason` the form
    /// turns into a sentence; there is no useful generic message here.
    func createAgent(_ draft: AgentDraft) async throws -> Agent
    /// `POST /v1/agents/interview` — the front door. You say what you want the
    /// desk for; the deck stands up a provisional one and answers with it, so
    /// the conversation can open immediately and the desk can name itself in
    /// it. `InterviewOutcome.agent` is one element of §3's `agents` array,
    /// exactly as `POST /v1/agents` returns; `.pretrust` says whether the
    /// window that just opened is sitting on a trust dialog nobody answered.
    ///
    /// Not in `client-api.md` at the time of writing — see the adapter.
    func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome
    /// `GET /v1/approvals` — every tool call waiting on a human, across all
    /// agents. Entries that cannot state their rule come back only as a count.
    func approvals() async throws -> ApprovalsPage
    /// `POST /v1/approvals/{id}`. The option carries the rule that was on
    /// screen, and that rule is sent, so the deck cannot grant something
    /// broader than what was shown.
    ///
    /// **Returns what the deck actually did**, which this client used to throw
    /// away: the rule that was written, and §12's `resumed` — whether the desk
    /// was told in words to carry on. Without those two, a tap that grants a
    /// standing permission on his Mac leaves nothing on screen to say so.
    @discardableResult
    func decideApproval(id: String, option: ApprovalOption) async throws -> ApprovalDecision
    /// `GET /v1/routines`
    func routines() async throws -> [Routine]
    /// `POST /v1/routines`. The body is a spec and a timezone; the deck
    /// computes `next_run_at` and this client never sends one.
    func createRoutine(_ draft: RoutineDraft) async throws -> Routine
    /// `PATCH /v1/routines/{id}` — enable or disable, nothing else.
    func setRoutine(id: String, enabled: Bool) async throws -> Routine
    /// `DELETE /v1/routines/{id}`
    func deleteRoutine(id: String) async throws
    /// `GET /v1/handoffs` — every block on this deck that **no permission can
    /// lift**, because the desk cannot act at all: a 2FA code, a CAPTCHA, an
    /// SMS confirmation, `gh auth login`. Shaped like `/v1/approvals` on
    /// purpose — the same `agent` / `asked_by` / `desk_known` join and options
    /// carrying their own sentences — so one tray draws both.
    func handoffs() async throws -> HandoffsPage
    /// `POST /v1/handoffs/{id}` — `done` or `skipped`, and they are different
    /// instructions rather than different words. See `HandoffOutcome`.
    ///
    /// Unlike an approval, the loop genuinely closes here: resuming after a
    /// handoff is an ordinary user message into the session, so `resumed` in
    /// the reply is a real answer about whether the desk was told.
    @discardableResult
    func resolveHandoff(id: String, outcome: HandoffOutcome) async throws -> HandoffResolution
    /// `GET /v1/stream` — one open connection, ended by throwing.
    func events() -> AsyncThrowingStream<DeckEvent, Error>
}

/// **K4: answering a decision card.** A separate protocol, like
/// `AgentScreenClient`, so a transport that predates the route simply does not
/// conform and the card says so instead of pretending the tap landed.
public protocol DecisionClient: Sendable {
    /// `POST /v1/decisions/{id}` `{"value": …}` — an option's value or his own
    /// words. The deck posts it into the thread as his message.
    func answerDecision(id: String, value: String) async throws
}

/// The 201 of `POST /v1/calls` (K6), and — C3 — the live voice the deck
/// minted for it, or why it could not.
public struct CallStart: Hashable, Decodable, Sendable {
    public var callID: String
    public var threadID: String
    public var voice: DeskVoice?
    /// A phone call with the desk's voice: the Realtime session to dial.
    public var realtime: RealtimeOffer?
    /// Why there is no `realtime` (no key on the deck, the mint failed). The
    /// app says it once and falls back to on-Mac speech.
    public var realtimeError: String?

    public init(callID: String, threadID: String, voice: DeskVoice? = nil,
                realtime: RealtimeOffer? = nil, realtimeError: String? = nil) {
        self.callID = callID
        self.threadID = threadID
        self.voice = voice
        self.realtime = realtime
        self.realtimeError = realtimeError
    }

    enum CodingKeys: String, CodingKey {
        case voice, realtime
        case callID = "call_id"
        case threadID = "thread_id"
        case realtimeError = "realtime_error"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        callID = try c.decode(String.self, forKey: .callID)
        threadID = try c.decode(String.self, forKey: .threadID)
        voice = try? c.decodeIfPresent(DeskVoice.self, forKey: .voice)
        // A malformed offer must not cost him the call: it falls back instead.
        do {
            realtime = try c.decodeIfPresent(RealtimeOffer.self, forKey: .realtime)
            realtimeError = try? c.decodeIfPresent(String.self, forKey: .realtimeError)
        } catch {
            realtime = nil
            realtimeError = "the deck's realtime offer could not be read"
        }
    }
}

/// **K6: a live call is a channel on the desk's own thread.** Utterances go
/// through the ordinary send route with `channel: "voice"` and the call's id.
public protocol CallClient: Sendable {
    func startCall(agent: String) async throws -> CallStart
    func sendVoice(threadID: String, text: String, callID: String) async throws -> Message
    func endCall(id: String) async throws
    /// What was said on a live call, as each line completes, so the desk and
    /// the next call remember it: `POST /v1/calls/{id}/transcript`.
    func postTranscript(callID: String, lines: [CallLine]) async throws
}

/// One line of a live call: his words (transcribed) or the agent's.
public struct CallLine: Equatable, Sendable {
    public enum Role: String, Sendable { case caller, agent }
    public var role: Role
    public var text: String

    public init(role: Role, text: String) {
        self.role = role
        self.text = text
    }
}

extension CallClient {
    /// A deck that predates the route: the call works, nothing is remembered.
    public func postTranscript(callID: String, lines: [CallLine]) async throws {}
}

extension DeckClient {
    public func messages(threadID: String, since: String?) async throws -> MessagePage {
        try await messages(threadID: threadID, since: since, limit: 50)
    }

    /// **A transport that has not been taught this route yet answers "nothing
    /// waiting", and says so out loud in the diagnostics.**
    ///
    /// Not a `fatalError` and not a throw: an adapter written before this route
    /// existed must keep working, and a tray that refused to draw *approvals*
    /// because it could not ask about *handoffs* would be a worse product than
    /// one that draws half. But an empty answer that means "did not ask" and an
    /// empty answer that means "nothing is stuck" are not the same fact, and
    /// only one of them is safe to be quiet about — hence the counter.
    public func handoffs() async throws -> HandoffsPage {
        Diagnostics.count("client.handoffsNotWired")
        return HandoffsPage(handoffs: [], unreadable: 0)
    }

    @discardableResult
    public func resolveHandoff(
        id: String, outcome: HandoffOutcome
    ) async throws -> HandoffResolution {
        throw DeckError.transport("this deck connection cannot answer handed-over steps")
    }
}

extension AgentsResponse {
    /// Shared by the HTTP roster call and the stream's `hello` snapshot.
    static func roster(from agents: [Agent]) -> RosterPayload {
        var order: [String] = []
        var threads: [ThreadSummary] = []
        for agent in agents {
            if !order.contains(agent.section) { order.append(agent.section) }
            threads.append(
                ThreadSummary(
                    id: agent.threadID,
                    kind: .direct,
                    title: agent.name,
                    agentName: agent.name,
                    participants: [DeckOwner.name, agent.name],
                    isReadOnly: false,
                    unreadCount: agent.unread,
                    lastActivity: agent.lastActivityAt,
                    preview: ThreadPreview(text: agent.preview)
                )
            )
        }
        return RosterPayload(agents: agents, threads: threads, sectionOrder: order)
    }
}

extension DeckEvent {
    /// Turns a wire frame into a domain event. Unknown types are ignored rather
    /// than fatal: a newer deck must not break an older client.
    public init?(sse: SSEEvent) {
        let data = Data(sse.data.utf8)

        struct Envelope: Decodable {
            let type: String
            let name: String?
            let state: String?
            let unread: Int?
            let thread_id: String?
            let read_only: Bool?
            let message: Message?
            let agents: [Agent]?
            let threads: [ThreadSummary]?
            let old_name: String?
            let agent: Agent?
            let decision: Decision?
            /// Tri-state on purpose (`BlockedUpdate`, not `Blocked?`): a plain
            /// optional cannot tell "the key was absent" apart from "the key
            /// was present and `null`", and §8 needs both — see its doc.
            let blocked: BlockedUpdate

            enum CodingKeys: String, CodingKey {
                case type, name, state, unread, thread_id, read_only, message, agents, threads, blocked
                case old_name, agent, decision
            }

            init(from decoder: Decoder) throws {
                let container = try decoder.container(keyedBy: CodingKeys.self)
                type = try container.decode(String.self, forKey: .type)
                name = try container.decodeIfPresent(String.self, forKey: .name)
                state = try container.decodeIfPresent(String.self, forKey: .state)
                unread = try container.decodeIfPresent(Int.self, forKey: .unread)
                thread_id = try container.decodeIfPresent(String.self, forKey: .thread_id)
                read_only = try container.decodeIfPresent(Bool.self, forKey: .read_only)
                message = try container.decodeIfPresent(Message.self, forKey: .message)
                agents = try container.decodeIfPresent([Agent].self, forKey: .agents)
                threads = try container.decodeIfPresent([ThreadSummary].self, forKey: .threads)
                old_name = try container.decodeIfPresent(String.self, forKey: .old_name)
                agent = try container.decodeIfPresent(Agent.self, forKey: .agent)
                decision = try? container.decodeIfPresent(Decision.self, forKey: .decision)
                if !container.contains(.blocked) {
                    blocked = .unspecified
                } else if try container.decodeNil(forKey: .blocked) {
                    blocked = .cleared
                } else {
                    blocked = .value(try container.decode(Blocked.self, forKey: .blocked))
                }
            }
        }

        guard let frame = try? DeckCoding.decoder.decode(Envelope.self, from: data) else {
            return nil
        }

        switch frame.type {
        case "hello":
            self = .hello(
                RosterSnapshot(agents: frame.agents ?? [], threads: frame.threads ?? [])
            )
        case "message":
            guard let message = frame.message else { return nil }
            self = .message(message, readOnly: frame.read_only ?? false)
        case "agent_state":
            guard let name = frame.name else { return nil }
            self = .agentState(name: name, state: AgentState(wire: frame.state ?? "IDLE"), blocked: frame.blocked)
        case "unread":
            guard let name = frame.name, let count = frame.unread else { return nil }
            self = .unread(agent: name, count: count)
        case "agent_renamed":
            // Both halves are required. A rename with no old name cannot be
            // matched to a row, and one with no agent has nothing to put there
            // — either way, guessing would move the wrong desk.
            guard let oldName = frame.old_name, let agent = frame.agent else { return nil }
            self = .agentRenamed(oldName: oldName, agent: agent)
        case "decision":
            guard let threadID = frame.thread_id, let decision = frame.decision else { return nil }
            self = .decision(threadID: threadID, decision: decision)
        case "heartbeat":
            self = .heartbeat
        default:
            return nil
        }
    }
}

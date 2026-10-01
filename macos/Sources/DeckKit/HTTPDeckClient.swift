import Foundation

/// Whoever actually moves the bytes. Injected so tests can inspect the requests
/// this adapter builds without opening a socket.
public protocol RequestPerformer: Sendable {
    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionPerformer: RequestPerformer {
    private let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    public func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await session.data(for: request)
            guard let http = response as? HTTPURLResponse else {
                throw DeckError.transport("not an HTTP response")
            }
            return (data, http)
        } catch let error as DeckError {
            throw error
        } catch {
            throw DeckError.transport(error.localizedDescription)
        }
    }
}

/// The long-lived `GET /v1/stream` connection, separated from one-shot requests
/// because its lifetime is the opposite shape.
public protocol SSETransport: Sendable {
    func lines(for request: URLRequest) -> AsyncThrowingStream<String, Error>
}

public struct URLSessionSSETransport: SSETransport {
    private let session: URLSession

    public init(session: URLSession = .shared) {
        self.session = session
    }

    /// The socket, framed into the chunks `SSEParser` expects.
    ///
    /// **Not `AsyncBytes.lines`.** Foundation's line sequence silently drops
    /// *empty* lines, and an empty line is the entire way Server-Sent Events
    /// say "that frame is over". Framing with it delivered every `data:` line
    /// and no terminator, so the parser held every frame open forever waiting
    /// for one that had been eaten a layer below it — and because the bytes
    /// themselves kept arriving, the silence watchdog stayed quiet and the
    /// indicator kept reading **Connected** while not one event was ever
    /// produced. That is the thread that never updates: the deck answers, the
    /// socket carries it, and nothing above this line ever hears it.
    ///
    /// So the newline is kept. A chunk is one line *including* its terminator,
    /// and a blank line arrives as `"\n"` — which is what ends a frame.
    /// Splitting on `0x0A` is safe for UTF-8: a newline byte can never be part
    /// of a multi-byte scalar.
    public func lines(for request: URLRequest) -> AsyncThrowingStream<String, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let (bytes, response) = try await session.bytes(for: request)
                    if let http = response as? HTTPURLResponse, http.statusCode >= 300 {
                        throw DeckError(status: http.statusCode, body: Data())
                    }
                    Diagnostics.count("http.streamOpened")
                    var line: [UInt8] = []
                    for try await byte in bytes {
                        line.append(byte)
                        guard byte == Self.newline else { continue }
                        continuation.yield(String(decoding: line, as: UTF8.self))
                        line.removeAll(keepingCapacity: true)
                    }
                    // A deck that closed mid-line leaves a partial one. Hand it
                    // over rather than swallowing it: the parser holds an
                    // unterminated line back on its own, and dropping it here
                    // would lose a frame that a reconnect then refetches anyway.
                    if !line.isEmpty {
                        continuation.yield(String(decoding: line, as: UTF8.self))
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    private static let newline = UInt8(10)
}

/// Thrown when the stream has gone quiet for longer than the deck's heartbeat
/// allows. Not an error the user sees — it is the signal to reconnect.
public struct StreamWentQuiet: Error, Equatable {
    public let after: TimeInterval
    public init(after: TimeInterval) { self.after = after }
}

/// A thin adapter over the `/v1` routes documented in `docs/client-api.md`.
/// It holds no state beyond its configuration.
public struct HTTPDeckClient: DeckClient {
    /// The deck emits a heartbeat after every 15s of silence; its own guidance
    /// is to reconnect if nothing at all arrives in ~35s.
    public static let silenceBudget: TimeInterval = 35

    private let baseURL: URL
    private let tokens: TokenStore
    private let performer: RequestPerformer
    private let sse: SSETransport

    public init(
        baseURL: URL,
        tokens: TokenStore,
        performer: RequestPerformer = URLSessionPerformer(),
        sse: SSETransport = URLSessionSSETransport()
    ) {
        self.baseURL = baseURL
        self.tokens = tokens
        self.performer = performer
        self.sse = sse
    }

    // MARK: routes

    public func roster() async throws -> RosterPayload {
        // One route. Unread, preview, timestamp and thread id all ride on the
        // agent row, so there is nothing to follow up per agent.
        let response: AgentsResponse = try await get("/v1/agents")
        return response.asRosterPayload()
    }

    public func threads() async throws -> [ThreadSummary] {
        let response: ThreadsResponse = try await get("/v1/threads")
        return response.threads
    }

    public func agent(named name: String) async throws -> Agent {
        try await get("/v1/agents/\(name)")
    }

    public func updateAgent(_ agent: Agent) async throws -> Agent {
        var request = try makeRequest(path: "/v1/agents/\(agent.name)", method: "PATCH")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // `AgentPatch`, never the whole agent: sending `reports_to` at all is a
        // 409, and `name` is the identity rather than a setting.
        request.httpBody = try AgentPatch(agent).encoded()
        return try await decode(request)
    }

    public func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
        var query = [URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200)))]
        if let since {
            // Echoed straight back. The client never builds or parses a cursor.
            query.append(URLQueryItem(name: "since", value: since))
        }
        let request = try makeRequest(
            path: "/v1/threads/\(threadID)/messages", method: "GET", query: query
        )
        return try await decode(request)
    }

    public func send(threadID: String, text: String) async throws -> Message {
        try await post(threadID: threadID, ["text": text, "as": DeckOwner.name, "channel": "text"])
    }

    /// K2: this app only ever sends as him — `as` is said, never defaulted, so
    /// the deck cannot mistake it for the engineer or a routine.
    private func post(threadID: String, _ body: [String: String]) async throws -> Message {
        var request = try makeRequest(path: "/v1/threads/\(threadID)/messages", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let response: SendResponse = try await decode(request)
        // `delivered: false` means queued, not lost. Retrying would double-send.
        return response.message
    }

    public func markRead(agent: String, upTo: String?) async throws {
        var request = try makeRequest(path: "/v1/agents/\(agent)/read", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let body: [String: String] = upTo.map { ["up_to": $0] } ?? [:]
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        _ = try await raw(request)
    }

    public func createAgent(_ draft: AgentDraft) async throws -> Agent {
        if let problem = draft.problem {
            // The form's own rules are not the deck's job to enforce twice.
            throw DeckError.transport(problem)
        }
        var request = try makeRequest(path: "/v1/agents", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try draft.encodedBody()
        let response: CreatedAgentResponse = try await decode(request)
        return response.agent
    }

    /// The adapter for the front door, `POST /v1/agents/interview` (§11.1).
    ///
    /// The body is `{role_hint, engine?, reports_to?}`. `cwd` is deliberately
    /// omitted, which the contract is explicit about: with no folder stated the
    /// deck creates `~/.claude/agent-bus/workspaces/<name>/` and runs the
    /// session there, and `no_such_cwd` becomes impossible.
    ///
    /// The 201 carries `provisional`, `name`, `thread_id`, `agent` **and**
    /// `pretrust`. Only `agent` and `pretrust` are read: `agent` already holds
    /// the name and the thread id, and decoding the same fact twice is how the
    /// two copies end up disagreeing.
    public func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
        if let problem = draft.problem { throw DeckError.transport(problem) }
        var request = try makeRequest(path: "/v1/agents/interview", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: draft.wireBody)
        let response: InterviewResponse = try await decode(request)
        return InterviewOutcome(agent: response.agent, pretrust: response.pretrust)
    }

    public func routines() async throws -> [Routine] {
        let response: RoutinesResponse = try await get("/v1/routines")
        return response.routines
    }

    public func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
        if let problem = draft.problem { throw DeckError.transport(problem) }
        var request = try makeRequest(path: "/v1/routines", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try draft.encodedBody()
        let response: RoutineResponse = try await decode(request)
        return response.routine
    }

    public func setRoutine(id: String, enabled: Bool) async throws -> Routine {
        var request = try makeRequest(path: "/v1/routines/\(id)", method: "PATCH")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // Only the flag. A pause must not be a chance to rewrite the schedule.
        request.httpBody = try JSONSerialization.data(withJSONObject: ["enabled": enabled])
        let response: RoutineResponse = try await decode(request)
        return response.routine
    }

    public func deleteRoutine(id: String) async throws {
        let request = try makeRequest(path: "/v1/routines/\(id)", method: "DELETE")
        _ = try await raw(request)
    }

    public func approvals() async throws -> ApprovalsPage {
        try await get("/v1/approvals")
    }

    @discardableResult
    public func decideApproval(
        id: String, option: ApprovalOption
    ) async throws -> ApprovalDecision {
        guard option.isAvailable else {
            // The deck already said this reply is not on offer here, and
            // sending it anyway is a 409 that writes no rule and leaves the
            // question live. Refuse before the socket rather than after.
            throw DeckError.alwaysNotAvailable(detail: option.summary)
        }
        var request = try makeRequest(path: "/v1/approvals/\(segment(id))", method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        // §12: exactly one key. The deck decides what the reply grants; the
        // client's job is to have shown the summary before the tap.
        request.httpBody = try JSONSerialization.data(
            withJSONObject: ["reply": option.reply.rawValue]
        )
        // The 200 carries `ask.answered`, the `rule` that was written and
        // `resumed`. It used to be discarded, so the app could not tell him
        // what his tap had granted or whether the desk had moved.
        return try await decode(request)
    }

    /// `GET /v1/handoffs` — every step on this deck that no permission can
    /// unblock, because the desk cannot take it at all: a 2FA code, a CAPTCHA,
    /// an SMS confirmation, `gh auth login`, signing into Google.
    ///
    /// Wired here and not left on the protocol default, which returns an empty
    /// page. That default exists so a client pointed at an older deck degrades
    /// instead of crashing — and it is also indistinguishable from a transport
    /// nobody connected, which is what shipped: the tray drew approvals and no
    /// handoffs against a deck that was serving them, and nothing failed.
    public func handoffs() async throws -> HandoffsPage {
        try await get("/v1/handoffs")
    }

    /// `GET /v1/owner/alerts?since=` — what needs him, or was said to him,
    /// since the cursor (`OwnerAlerts.swift`). No cursor is a first poll: the
    /// deck answers nothing and a cursor at the head.
    public func ownerAlerts(since: String?) async throws -> OwnerAlertPage {
        let query = since.map { [URLQueryItem(name: "since", value: $0)] }
        return try await decode(makeRequest(path: "/v1/owner/alerts", method: "GET", query: query))
    }

    /// `POST /v1/handoffs/{id}` — his verb, and only his verb.
    ///
    /// `done` and `skipped` are different instructions, not different words:
    /// `done` means the desk goes back and re-checks that the step actually
    /// worked; `skipped` means it abandons that path for good and reports what
    /// it can no longer finish. `server/handoff.py` states plainly that
    /// collapsing them turns a skipped handoff into an infinite retry loop, so
    /// what travels is read straight off the outcome he tapped.
    ///
    /// The deck reads exactly one key, `reply` — the same shape as an approval.
    @discardableResult
    public func resolveHandoff(
        id: String, outcome: HandoffOutcome
    ) async throws -> HandoffResolution {
        var request = try makeRequest(path: "/v1/handoffs/\(segment(id))", method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(
            withJSONObject: ["reply": outcome.rawValue]
        )
        return try await decode(request)
    }

    public func events() -> AsyncThrowingStream<DeckEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    let request = try makeRequest(
                        path: "/v1/stream", method: "GET", accept: "text/event-stream"
                    )
                    var parser = SSEParser()
                    // The stream has no Last-Event-ID and is not replayable, so
                    // silence is indistinguishable from a dead socket. The
                    // watchdog turns it into a reconnect instead of a hang.
                    for try await chunk in Self.withSilenceWatchdog(
                        sse.lines(for: request), budget: Self.silenceBudget
                    ) {
                        Diagnostics.count("http.streamChunk")
                        for frame in parser.consume(chunk) {
                            if let event = DeckEvent(sse: frame) {
                                continuation.yield(event)
                            }
                        }
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }

    /// Fails the stream if nothing at all arrives within `budget`.
    ///
    /// A TCP connection can stay open long after the other end has stopped
    /// talking, and this stream carries no ids to resume from — so a quiet
    /// socket has to be treated as a dead one and reconnected, not waited on.
    static func withSilenceWatchdog(
        _ upstream: AsyncThrowingStream<String, Error>,
        budget: TimeInterval
    ) -> AsyncThrowingStream<String, Error> {
        final class LastHeard: @unchecked Sendable {
            private let lock = NSLock()
            private var at = Date()
            func touch() { lock.lock(); at = Date(); lock.unlock() }
            var elapsed: TimeInterval { lock.lock(); defer { lock.unlock() }; return -at.timeIntervalSinceNow }
        }

        return AsyncThrowingStream { continuation in
            let lastHeard = LastHeard()
            let pump = Task {
                do {
                    for try await chunk in upstream {
                        lastHeard.touch()
                        continuation.yield(chunk)
                    }
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            let watchdog = Task {
                while !Task.isCancelled {
                    try await Task.sleep(nanoseconds: UInt64(budget / 3 * 1_000_000_000))
                    if lastHeard.elapsed > budget {
                        continuation.finish(throwing: StreamWentQuiet(after: budget))
                        return
                    }
                }
            }
            continuation.onTermination = { _ in
                pump.cancel()
                watchdog.cancel()
            }
        }
    }

    // MARK: plumbing

    /// One id, safe to paste into a path.
    ///
    /// `URLComponents.path` encodes `|` and `:` for us but leaves `/` alone,
    /// because in a path a slash is structure rather than data. An ask id and
    /// a handoff id are **data** — the deck mints them and the app never parses
    /// them — so one containing a slash would silently address a different
    /// route instead of failing. Escaped here, once, for both.
    private func segment(_ id: String) -> String {
        id.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(
            CharacterSet(charactersIn: "-._~"))) ?? id
    }

    private func makeRequest(
        path: String,
        method: String,
        query: [URLQueryItem]? = nil,
        accept: String = "application/json",
        escaped: Bool = false
    ) throws -> URLRequest {
        guard let token = try tokens.token(), !token.isEmpty else {
            // Refused here rather than sent anonymously: an unauthenticated
            // request to this deck is never a useful thing to have done.
            throw DeckError.missingToken
        }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw DeckError.transport("bad base URL")
        }
        // Thread ids contain `|` and `:`; URLComponents encodes the path for
        // us. `escaped` says the caller has already encoded a segment itself
        // (see `segment(_:)`), and assigning `path` there would encode the `%`
        // again — `a b/c` came out as `a%2520b%252Fc`, measured.
        if escaped {
            components.percentEncodedPath = path
        } else {
            components.path = path
        }
        components.queryItems = query
        guard let url = components.url else {
            throw DeckError.transport("could not build \(path)")
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(accept, forHTTPHeaderField: "Accept")
        return request
    }

    private func get<T: Decodable>(_ path: String) async throws -> T {
        try await decode(makeRequest(path: path, method: "GET"))
    }

    private func decode<T: Decodable>(_ request: URLRequest) async throws -> T {
        let data = try await raw(request)
        do {
            return try DeckCoding.decoder.decode(T.self, from: data)
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }

    @discardableResult
    private func raw(_ request: URLRequest) async throws -> Data {
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        DeckOwner.learn(response.value(forHTTPHeaderField: DeckOwner.header))
        guard (200..<300).contains(response.statusCode) else {
            // The status narrows it; `reason` decides it.
            throw DeckError(status: response.statusCode, body: data)
        }
        return data
    }
}

// MARK: - K4 decisions and K6 calls

extension HTTPDeckClient: DecisionClient, CallClient {
    public func answerDecision(id: String, value: String) async throws {
        try await postJSON("/v1/decisions/\(segment(id))", ["value": value])
    }

    public func startCall(agent: String) async throws -> CallStart {
        var request = try makeRequest(path: "/v1/calls", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["agent": agent])
        return try await decode(request)
    }

    public func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        try await post(threadID: threadID, [
            "text": text, "as": DeckOwner.name, "channel": "voice", "call_id": callID])
    }

    public func endCall(id: String) async throws {
        try await postJSON("/v1/calls/\(segment(id))/end", [:])
    }

    public func postTranscript(callID: String, lines: [CallLine]) async throws {
        var request = try makeRequest(path: "/v1/calls/\(segment(callID))/transcript", method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: [
            "lines": lines.map { ["role": $0.role.rawValue, "text": $0.text] },
        ])
        try await raw(request)
    }

    private func postJSON(_ escapedPath: String, _ body: [String: String]) async throws {
        var request = try makeRequest(path: escapedPath, method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        try await raw(request)
    }
}

// MARK: - Spoken replies and voice messages

private struct VoiceNoteResponse: Decodable {
    let message: Message
    let transcript: String?
}

extension HTTPDeckClient: SpeechClient, VoiceNoteClient {
    /// `POST /v1/speech`: the words in the desk's call voice, as mp3.
    public func speech(text: String, agent: String) async throws -> Data {
        var request = try makeRequest(path: "/v1/speech", method: "POST", accept: "audio/mpeg")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["text": text, "agent": agent])
        return try await raw(request)
    }

    /// `POST /v1/threads/{id}/voice-notes`: the recording is the body.
    public func sendVoiceNote(threadID: String, audio: Data) async throws -> VoiceNoteSent {
        var request = try makeRequest(path: "/v1/threads/\(threadID)/voice-notes", method: "POST")
        request.setValue("audio/mp4", forHTTPHeaderField: "Content-Type")
        request.httpBody = audio
        request.timeoutInterval = 90
        let response: VoiceNoteResponse = try await decode(request)
        return VoiceNoteSent(message: response.message,
                             transcript: response.transcript ?? response.message.text)
    }

    /// `GET /v1/voice-notes/{id}`: his recording, for the play button.
    public func voiceNoteAudio(id: String) async throws -> Data {
        try await raw(makeRequest(path: "/v1/voice-notes/\(segment(id))", method: "GET",
                                  accept: "audio/mp4", escaped: true))
    }
}

// MARK: - Claude usage meter (C4)

extension HTTPDeckClient: ClaudeUsageSource {
    /// `GET /v1/usage`. `nil` when the deck does not serve the route (404):
    /// the meter is optional and its absence is not a failure.
    public func usage() async throws -> ClaudeUsage? {
        let request = try makeRequest(path: "/v1/usage", method: "GET")
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        if response.statusCode == 404 { return nil }
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError(status: response.statusCode, body: data)
        }
        do {
            return try DeckCoding.decoder.decode(ClaudeUsage.self, from: data)
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }
}

// MARK: - "<Agent>'s screen" (§15)

/// The three screen routes, kept apart from the rest of the adapter because
/// they refuse in their own vocabulary.
///
/// `ScreenRefusal` rather than `DeckError`: `computer_not_running`,
/// `no_frame` and `docker_unavailable` are three different things for him to
/// do something about, and none of them is a thing `DeckError` can say. The
/// refusal is read off `reason` and never off the status — 409 alone covers
/// four of them.
extension HTTPDeckClient: AgentScreenClient {

    public func screenStatus(agent: String) async throws -> AgentScreenStatus {
        let request = try screenRequest(path: "/v1/agents/\(agent)/screen", method: "GET")
        let (data, _) = try await screenPerform(request)
        do {
            return try DeckCoding.decoder.decode(AgentScreenStatus.self, from: data)
        } catch {
            throw ScreenRefusal.transport("the deck's screen status did not decode: "
                                          + String(describing: error))
        }
    }

    /// One JPEG, with the two headers that make it honest.
    ///
    /// **`X-Frame-Age` is read, and a missing one is not invented.**
    /// `server/screen.py`: *"a frame without an age is a lie — a checkout page
    /// looks the same a second old and twenty minutes dead."* A header this
    /// client could not find means the age is unknown, which the panel draws
    /// as its own state; defaulting it to zero would put the lie back.
    ///
    /// Nothing caches this. The route says `no-store` because it is a
    /// photograph of a signed-in browser, and `.reloadIgnoringLocalCacheData`
    /// is this side of the same rule.
    public func screenFrame(agent: String) async throws -> AgentScreenFrame {
        var request = try screenRequest(path: "/v1/agents/\(agent)/screen.jpg",
                                        method: "GET", accept: "image/jpeg")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await screenPerform(request)
        let age = response.value(forHTTPHeaderField: "X-Frame-Age").flatMap(TimeInterval.init)
        let display = response.value(forHTTPHeaderField: "X-Frame-Display") ?? ""
        return AgentScreenFrame(jpeg: data, serverAge: age, display: display,
                                receivedAt: Date())
    }

    /// One gesture per call, sent exactly as it was made.
    ///
    /// The coordinates are already in display space — `AgentComputerModel` does
    /// the transform and refuses a point that is not on the picture, so nothing
    /// here clamps, rounds or second-guesses. An off-screen coordinate that
    /// reaches this line is a bug worth seeing as the deck's own 400.
    public func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        var request = try screenRequest(path: "/v1/agents/\(agent)/screen/input",
                                        method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try DeckCoding.encoder.encode(input)
        } catch {
            throw ScreenRefusal.transport("could not encode that gesture")
        }
        _ = try await screenPerform(request)
    }

    private func screenRequest(path: String, method: String,
                               accept: String = "application/json") throws -> URLRequest {
        do {
            return try makeRequest(path: path, method: method, accept: accept)
        } catch DeckError.missingToken {
            throw ScreenRefusal.missingToken
        } catch {
            throw ScreenRefusal.transport(String(describing: error))
        }
    }

    private func screenPerform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        Diagnostics.count("http.request")
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await performer.perform(request)
        } catch let error as DeckError {
            if case .transport(let detail) = error { throw ScreenRefusal.transport(detail) }
            throw ScreenRefusal.transport(error.userFacingText)
        } catch {
            throw ScreenRefusal.transport(error.localizedDescription)
        }
        guard (200..<300).contains(response.statusCode) else {
            throw ScreenRefusal(status: response.statusCode, body: data)
        }
        return (data, response)
    }
}

// MARK: - Connectors & Skills (`docs/connectors.md`)

extension HTTPDeckClient: StoreClient {
    public func storeCatalog(kind: String?, query: String?, trust: StoreTrustScope,
                             limit: Int, offset: Int) async throws -> StoreCatalogPage {
        var items: [URLQueryItem] = []
        if let kind { items.append(URLQueryItem(name: "kind", value: kind)) }
        if let query, !query.isEmpty { items.append(URLQueryItem(name: "q", value: query)) }
        items.append(URLQueryItem(name: "trust", value: trust.rawValue))
        items.append(URLQueryItem(name: "limit", value: String(max(limit, 1))))
        items.append(URLQueryItem(name: "offset", value: String(max(offset, 0))))
        return try await decode(makeRequest(path: "/v1/store/catalog", method: "GET", query: items))
    }

    public func storeItem(id: String) async throws -> StoreItem {
        try await decode(makeRequest(
            path: "/v1/store/item", method: "GET", query: [URLQueryItem(name: "id", value: id)]))
    }

    /// The body carries typed keys. It is encoded straight into the request
    /// and never logged; `StoreInstallRequest` describes itself without them.
    public func storeInstall(_ install: StoreInstallRequest) async throws -> StoreInstallResult {
        try await decode(storePost("/v1/store/install", install))
    }

    public func storeUpdate(id: String, desks: StoreDesks) async throws -> StoreInstallResult {
        try await decode(storePost("/v1/store/update", StoreTargetBody(id: id, desks: desks)))
    }

    public func storeUninstall(id: String, desks: StoreDesks) async throws -> StoreUninstallResult {
        try await decode(storePost("/v1/store/uninstall", StoreTargetBody(id: id, desks: desks)))
    }

    public func storeInstalled(desk: String?) async throws -> StoreInstalledPage {
        let query = desk.map { [URLQueryItem(name: "desk", value: $0)] }
        return try await decode(makeRequest(path: "/v1/store/installed", method: "GET", query: query))
    }

    public func storeRefresh() async throws -> Bool {
        struct Refreshing: Decodable { let refreshing: Bool? }
        let answer: Refreshing = try await decode(storePost("/v1/store/refresh", [String: String]()))
        return answer.refreshing ?? false
    }

    private struct StoreTargetBody: Encodable {
        let id: String
        let desks: StoreDesks
    }

    private func storePost<Body: Encodable>(_ path: String, _ body: Body) throws -> URLRequest {
        var request = try makeRequest(path: path, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try DeckCoding.encoder.encode(body)
        return request
    }
}

// MARK: - a sign-in made on this Mac

extension HTTPDeckClient: LoginSharingClient {
    /// `POST /v1/logins` — write-only: the deck puts the cookies into every
    /// desk's browser and its login vault and answers with counts. The array
    /// travels exactly as Chrome reported it, spliced in rather than parsed,
    /// and nothing here logs or keeps it.
    public func shareLogins(cookieJSON: Data) async throws -> LoginShare {
        var request = try makeRequest(path: "/v1/logins", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data(#"{"source":"mac","cookies":"#.utf8) + cookieJSON + Data("}".utf8)
        return try await decode(request)
    }
}

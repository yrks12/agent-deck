import Foundation

/// Whoever actually moves the bytes. Injected so tests can inspect the requests
/// this adapter builds without opening a socket.
public protocol RequestPerformer: Sendable {
    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

public struct URLSessionPerformer: RequestPerformer {
    /// Readable so a socket opened beside it keeps the same TLS pin.
    public let session: URLSession

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
    /// The deck says `hello` the moment a stream connects (`server/api.py`), so
    /// a stream that has said nothing at all after this long never connected:
    /// try again rather than wait out the whole silence budget on "Finding".
    public static let firstByteBudget: TimeInterval = 8

    private let baseURL: URL
    private let tokens: TokenStore
    private let performer: RequestPerformer
    private let sse: SSETransport
    /// Set when every `events()` on this client (and its copies) shares one
    /// connection: the app's roster, open threads and call listen together.
    private let shared: SharedDeckEvents?

    public init(
        baseURL: URL,
        tokens: TokenStore,
        performer: RequestPerformer = URLSessionPerformer(),
        sse: SSETransport = URLSessionSSETransport(),
        sharesOneStream: Bool = false
    ) {
        self.baseURL = baseURL
        self.tokens = tokens
        self.performer = performer
        self.sse = sse
        if sharesOneStream {
            let own = HTTPDeckClient(baseURL: baseURL, tokens: tokens, performer: performer, sse: sse)
            shared = SharedDeckEvents(open: { own.events() })
        } else {
            shared = nil
        }
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

    public func messages(threadID: String, before: String, limit: Int) async throws -> MessagePage {
        let query = [
            URLQueryItem(name: "limit", value: String(min(max(limit, 1), 200))),
            // The cursor of the oldest line held, echoed back as the deck gave it.
            URLQueryItem(name: "before", value: before),
        ]
        let request = try makeRequest(
            path: "/v1/threads/\(threadID)/messages", method: "GET", query: query
        )
        return try await decode(request)
    }

    public func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
        var body = ["text": text, "as": DeckOwner.name, "channel": "text"]
        // Only on a reply: a plain send's body is unchanged for every deck.
        if let replyTo, !replyTo.isEmpty { body["reply_to"] = replyTo }
        return try await post(threadID: threadID, body)
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
    /// `active`: this app is in front and he is using it. The deck then
    /// holds every push until he leaves (presence, `server/push_policy.py`).
    public func ownerAlerts(since: String?, active: Bool = false) async throws -> OwnerAlertPage {
        var query: [URLQueryItem] = []
        if let since { query.append(URLQueryItem(name: "since", value: since)) }
        if active { query.append(URLQueryItem(name: "active", value: "1")) }
        return try await decode(makeRequest(path: "/v1/owner/alerts", method: "GET",
                                            query: query.isEmpty ? nil : query))
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

    /// Replace the shared connection (no-op on a client that does not share).
    public func reconnectStream() async {
        await shared?.reconnect()
    }

    public func events() -> AsyncThrowingStream<DeckEvent, Error> {
        if let shared { return shared.events() }
        return AsyncThrowingStream { continuation in
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
                        sse.lines(for: request), budget: Self.silenceBudget, firstByte: Self.firstByteBudget
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

    /// Fails the stream if nothing at all arrives within `budget` — or, before
    /// the first chunk, within `firstByte`.
    ///
    /// A TCP connection can stay open long after the other end has stopped
    /// talking, and this stream carries no ids to resume from — so a quiet
    /// socket has to be treated as a dead one and reconnected, not waited on.
    static func withSilenceWatchdog(
        _ upstream: AsyncThrowingStream<String, Error>,
        budget: TimeInterval,
        firstByte: TimeInterval? = nil
    ) -> AsyncThrowingStream<String, Error> {
        final class LastHeard: @unchecked Sendable {
            private let lock = NSLock()
            private var at = Date()
            private var heard = false
            func touch() { lock.lock(); at = Date(); heard = true; lock.unlock() }
            var elapsed: TimeInterval { lock.lock(); defer { lock.unlock() }; return -at.timeIntervalSinceNow }
            var hasHeard: Bool { lock.lock(); defer { lock.unlock() }; return heard }
        }
        let opening = min(budget, firstByte ?? budget)

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
                    let allowed = lastHeard.hasHeard ? budget : opening
                    try await Task.sleep(nanoseconds: UInt64(allowed / 3 * 1_000_000_000))
                    let limit = lastHeard.hasHeard ? budget : opening
                    if lastHeard.elapsed > limit {
                        continuation.finish(throwing: StreamWentQuiet(after: limit))
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
        DeckOwner.learn(from: response)
        guard (200..<300).contains(response.statusCode) else {
            // The status narrows it; `reason` decides it.
            throw DeckError(status: response.statusCode, body: data)
        }
        return data
    }
}

// MARK: - A desk calling him (docs/calls.md)

extension HTTPDeckClient: IncomingCallClient {
    public func answerRing(id: String) async throws -> CallStart {
        var request = try makeRequest(path: "/v1/calls/incoming/\(segment(id))/answer",
                                      method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        return try await decode(request)
    }

    public func declineRing(id: String) async throws {
        var request = try makeRequest(path: "/v1/calls/incoming/\(segment(id))/decline",
                                      method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = Data("{}".utf8)
        try await raw(request)
    }

    public func callSettings() async throws -> CallOwnerSettings {
        try await decode(makeRequest(path: "/v1/calls/settings", method: "GET"))
    }

    public func updateCallSettings(_ changes: [String: CallSettingValue]) async throws -> CallOwnerSettings {
        var request = try makeRequest(path: "/v1/calls/settings", method: "PATCH")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: changes.mapValues(\.json))
        return try await decode(request)
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

// MARK: - His screenshots and files (`OutgoingAttachments.swift`)

private final class UploadProgress: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    let report: @Sendable (Double) -> Void
    init(_ report: @escaping @Sendable (Double) -> Void) { self.report = report }
    func urlSession(_ session: URLSession, task: URLSessionTask, didSendBodyData bytesSent: Int64,
                    totalBytesSent: Int64, totalBytesExpectedToSend: Int64) {
        guard totalBytesExpectedToSend > 0 else { return }
        report(Double(totalBytesSent) / Double(totalBytesExpectedToSend))
    }
}

extension URLSessionPerformer: ProgressRequestPerformer {
    public func upload(_ request: URLRequest, body: Data,
                       progress: @escaping @Sendable (Double) -> Void) async throws -> (Data, HTTPURLResponse) {
        var request = request
        request.httpBody = nil
        do {
            let (data, response) = try await session.upload(for: request, from: body, delegate: UploadProgress(progress))
            guard let http = response as? HTTPURLResponse else { throw DeckError.transport("not an HTTP response") }
            return (data, http)
        } catch let error as DeckError {
            throw error
        } catch is CancellationError {
            throw CancellationError()
        } catch let error as URLError where error.code == .cancelled {
            throw CancellationError()
        } catch {
            throw DeckError.transport(error.localizedDescription)
        }
    }
}

extension HTTPDeckClient: AttachmentUploading {
    /// `POST /v1/threads/{id}/attachments`: the file is the body.
    public func uploadAttachment(threadID: String, data: Data, filename: String, mimeType: String,
                                 progress: @escaping @Sendable (Double) -> Void) async throws -> UploadedAttachment {
        var request = try makeRequest(path: "/v1/threads/\(threadID)/attachments", method: "POST")
        request.setValue(mimeType, forHTTPHeaderField: "Content-Type")
        request.setValue(AttachmentWire.encodedName(filename), forHTTPHeaderField: AttachmentWire.filenameHeader)
        request.httpBody = data
        request.timeoutInterval = 120
        Diagnostics.count("http.request")
        let body: Data, response: HTTPURLResponse
        if let tracked = performer as? ProgressRequestPerformer {
            (body, response) = try await tracked.upload(request, body: data, progress: progress)
        } else {
            (body, response) = try await performer.perform(request)
        }
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError(status: response.statusCode, body: body)
        }
        progress(1)
        do {
            return try DeckCoding.decoder.decode(UploadedAttachment.self, from: body)
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }

    /// `GET /v1/attachments/{id}/{name}`: a picture he sent, for his bubble.
    public func attachmentData(url: String) async throws -> Data {
        try await raw(makeRequest(path: url, method: "GET", accept: "*/*", escaped: true))
    }
}

extension HTTPDeckClient: AttachmentRangeFetching {
    /// `GET /v1/attachments/{id}/{name}` with `Range`: part of a held video.
    public func attachmentRange(url: String, offset: Int64, length: Int?) async throws -> MediaRange {
        var request = try makeRequest(path: url, method: "GET", accept: "*/*", escaped: true)
        let end = length.map { "\(offset + Int64(max($0, 1)) - 1)" } ?? ""
        request.setValue("bytes=\(offset)-\(end)", forHTTPHeaderField: "Range")
        request.timeoutInterval = 120
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError(status: response.statusCode, body: data)
        }
        let total = response.statusCode == 206
            ? (response.value(forHTTPHeaderField: "Content-Range").flatMap(MediaRange.total(fromContentRange:)))
            : Int64(data.count)
        return MediaRange(data: data, total: total, contentType: response.value(forHTTPHeaderField: "Content-Type"))
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
        DeckOwner.learn(from: response)
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

// MARK: - Money (`GET /v1/money`)

extension HTTPDeckClient: MoneySource {
    /// `nil` on a 404: an older deck has no Money route, and that is a state.
    public func money(refresh: Bool) async throws -> MoneyReport? {
        let request = try makeRequest(path: "/v1/money", method: "GET",
                                      query: refresh ? [URLQueryItem(name: "refresh", value: "1")] : nil)
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        DeckOwner.learn(from: response)
        if response.statusCode == 404 { return nil }
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError(status: response.statusCode, body: data)
        }
        do {
            return try DeckCoding.decoder.decode(MoneyReport.self, from: data)
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }
}

// MARK: - Claude accounts (docs/plans/2026-10-01-two-accounts.md)

extension HTTPDeckClient: AccountMover {
    /// `GET /v1/accounts`. `nil` on a 404: a deck without accounts is a state.
    public func accounts() async throws -> [DeckAccount]? {
        let request = try makeRequest(path: "/v1/accounts", method: "GET")
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        DeckOwner.learn(from: response)
        if response.statusCode == 404 { return nil }
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError(status: response.statusCode, body: data)
        }
        do {
            return try DeckCoding.decoder.decode(AccountList.self, from: data).accounts
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }

    /// `POST /v1/agents/{name}/account`. Refusals keep their own meaning:
    /// 409 `not_idle`, 404 `no_account`, 502 `move_failed`.
    public func move(agent: String, to account: String) async throws {
        var request = try makeRequest(path: "/v1/agents/\(segment(agent))/account", method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["account": account])
        Diagnostics.count("http.request")
        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await performer.perform(request)
        } catch let error as DeckError {
            throw AccountMoveError.other(error)
        }
        DeckOwner.learn(from: response)
        guard (200..<300).contains(response.statusCode) else {
            throw AccountMoveError(status: response.statusCode, body: data)
        }
    }
}

// MARK: - Signing in to a Claude account

extension HTTPDeckClient: AccountLoginClient {
    public func startLogin(id: String, label: String) async throws -> AccountLoginStart {
        let data = try await loginCall("/v1/accounts/login", method: "POST", body: ["id": id, "label": label], call: .start)
        do { return try DeckCoding.decoder.decode(AccountLoginStart.self, from: data) }
        catch { throw AccountLoginError.other(.decoding(String(describing: error))) }
    }

    public func submitLoginCode(loginID: String, code: String) async throws {
        _ = try await loginCall("/v1/accounts/login/\(segment(loginID))/code", method: "POST",
                                body: ["code": code], call: .followUp)
    }

    public func loginStatus(loginID: String) async throws -> AccountLoginStatus {
        let data = try await loginCall("/v1/accounts/login/\(segment(loginID))", method: "GET", body: nil, call: .followUp)
        do { return try DeckCoding.decoder.decode(AccountLoginStatus.self, from: data) }
        catch { throw AccountLoginError.other(.decoding(String(describing: error))) }
    }

    private func loginCall(_ path: String, method: String, body: [String: String]?,
                           call: AccountLoginError.Call) async throws -> Data {
        var request: URLRequest
        do { request = try makeRequest(path: path, method: method, escaped: true) }
        catch let error as DeckError { throw AccountLoginError.other(error) }
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        Diagnostics.count("http.request")
        let data: Data
        let response: HTTPURLResponse
        do { (data, response) = try await performer.perform(request) }
        catch let error as DeckError { throw AccountLoginError.other(error) }
        DeckOwner.learn(from: response)
        guard (200..<300).contains(response.statusCode) else {
            throw AccountLoginError(status: response.statusCode, body: data, call: call)
        }
        return data
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
extension HTTPDeckClient: AgentScreenStreaming {

    /// `WS /v1/agents/{name}/screen/stream`, with the same bearer header as
    /// every route. A deck without the route (or without a WebSocket
    /// implementation) fails the handshake, the first `next()` throws, and
    /// the model goes on polling.
    public func openScreenStream(agent: String) async throws -> ScreenStreamSession {
        if MacScreenName.nodeId(agent) != nil {
            // A Mac pushes frames to the deck; its viewer polls `screen.jpg`.
            throw ScreenRefusal.transport("a Mac's live view has no stream")
        }
        var request = try screenRequest(path: "/v1/agents/\(agent)/screen/stream",
                                        method: "GET")
        guard let url = request.url,
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { throw ScreenRefusal.transport("could not build the stream URL") }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        request.url = parts.url
        request.setValue(nil, forHTTPHeaderField: "Accept")
        return URLSessionScreenStream(request: request)
    }
}

// MARK: - the desk's live terminal

extension HTTPDeckClient: DeskTerminalStreaming {

    /// `WS /v1/agents/{name}/terminal/stream?window=&cols=&rows=`, bearer
    /// header on the upgrade exactly as on the screen stream.
    public func terminalStreamRequest(agent: String, window: TerminalWindow,
                                      cols: Int, rows: Int) throws -> URLRequest {
        let grid = TerminalStreamWire.clamp(cols: cols, rows: rows)
        // His Mac (`mac:<node>`): the deck relays to a shell the Mac dials in
        // with (`/v1/nodes/<id>/terminal/stream`); `from` names this viewer on
        // the Mac's banner.
        let mac = MacScreenName.nodeId(agent) != nil
        var request = try screenRequest(path: MacScreenName.base(agent) + "/terminal/stream",
                                        method: "GET")
        guard let url = request.url,
              var parts = URLComponents(url: url, resolvingAgainstBaseURL: false)
        else { throw ScreenRefusal.transport("could not build the terminal URL") }
        parts.scheme = parts.scheme == "https" ? "wss" : "ws"
        parts.queryItems = [URLQueryItem(name: mac ? "from" : "window", value: mac ? Self.viewerName : window.rawValue),
                            URLQueryItem(name: "cols", value: String(grid.cols)),
                            URLQueryItem(name: "rows", value: String(grid.rows))]
        request.url = parts.url
        request.setValue(nil, forHTTPHeaderField: "Accept")
        return request
    }

    #if os(iOS)
    static let viewerName = "iphone"
    #else
    static let viewerName = "mac"
    #endif

    public func openTerminalStream(agent: String, window: TerminalWindow,
                                   cols: Int, rows: Int) async throws -> TerminalStreamSession {
        let request = try terminalStreamRequest(agent: agent, window: window, cols: cols, rows: rows)
        // The pinned session when there is one: a socket on `.shared` would
        // fail the very TLS pin every other route passes.
        let session = (performer as? URLSessionPerformer)?.session ?? .shared
        return URLSessionTerminalStream(request: request, session: session)
    }
}

extension HTTPDeckClient: AgentScreenClient {

    public func screenStatus(agent: String) async throws -> AgentScreenStatus {
        let request = try screenRequest(path: MacScreenName.base(agent) + "/screen", method: "GET",
                                        query: MacScreenName.query(agent))
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
        var request = try screenRequest(path: MacScreenName.base(agent) + "/screen.jpg",
                                        method: "GET", query: MacScreenName.query(agent), accept: "image/jpeg")
        request.cachePolicy = .reloadIgnoringLocalCacheData
        let (data, response) = try await screenPerform(request)
        let age = response.value(forHTTPHeaderField: "X-Frame-Age").flatMap(TimeInterval.init)
        let display = response.value(forHTTPHeaderField: "X-Frame-Display") ?? ""
        let displayId = response.value(forHTTPHeaderField: "X-Frame-Display-Id").flatMap { Int($0) }
        return AgentScreenFrame(jpeg: data, serverAge: age, display: display,
                                receivedAt: Date(), displayId: displayId)
    }

    /// One gesture per call, sent exactly as it was made.
    ///
    /// The coordinates are already in display space — `AgentComputerModel` does
    /// the transform and refuses a point that is not on the picture, so nothing
    /// here clamps, rounds or second-guesses. An off-screen coordinate that
    /// reaches this line is a bug worth seeing as the deck's own 400.
    public func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        var request = try screenRequest(path: MacScreenName.base(agent) + "/screen/input",
                                        method: "POST", query: MacScreenName.query(agent))
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            request.httpBody = try DeckCoding.encoder.encode(input)
        } catch {
            throw ScreenRefusal.transport("could not encode that gesture")
        }
        _ = try await screenPerform(request)
    }

    private func screenRequest(path: String, method: String, query: [URLQueryItem]? = nil,
                               accept: String = "application/json") throws -> URLRequest {
        do {
            return try makeRequest(path: path, method: method, query: query, accept: accept)
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

    public func storeConnect(id: String, desks: StoreDesks) async throws -> StoreConnectStart {
        try await decode(storePost("/v1/store/connect", StoreTargetBody(id: id, desks: desks)))
    }

    /// The callback URL carries the code and state; it goes in this body and
    /// nowhere else.
    public func storeConnectComplete(callbackURL: String) async throws -> StoreInstallResult {
        try await decode(storePost("/v1/store/connect/complete", ["callback_url": callbackURL]))
    }

    public func storeConnectStatus(state: String) async throws -> StoreConnectStatus {
        try await decode(makeRequest(path: "/v1/store/connect/status", method: "GET",
                                     query: [URLQueryItem(name: "state", value: state)]))
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
        try await post(Self.loginsBody(cookieJSON: cookieJSON, via: nil))
    }

    public func shareLogins(cookieJSON: Data, via: String) async throws -> LoginShare {
        try await post(Self.loginsBody(cookieJSON: cookieJSON, via: via))
    }

    /// `POST /v1/logins`'s body. The cookies are spliced in untouched, never
    /// parsed or logged. PURE.
    static func loginsBody(cookieJSON: Data, via: String?) -> Data {
        let tag = via.map { #","via":"\#($0 == "chrome" ? "chrome" : "fresh")""# } ?? ""
        return Data(#"{"source":"mac"\#(tag),"cookies":"#.utf8) + cookieJSON + Data("}".utf8)
    }

    private func post(_ body: Data) async throws -> LoginShare {
        var request = try makeRequest(path: "/v1/logins", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = body
        return try await decode(request)
    }
}

// MARK: - the phone asks the Mac to sign a desk in

private struct LoginRequestEnvelope: Decodable { let request: LoginRequest? }

extension HTTPDeckClient: LoginRequestClient {
    /// `POST /v1/logins/requests` — what the phone sends. No cookie, ever.
    public func fileLoginRequest(desk: String, origin: String, method: LoginMethod,
                                 handoff: String?) async throws -> LoginRequest {
        var body = ["desk": desk, "origin": origin, "method": method.rawValue]
        if let handoff { body["handoff"] = handoff }
        var request = try makeRequest(path: "/v1/logins/requests", method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        let envelope: LoginRequestEnvelope = try await decode(request)
        guard let row = envelope.request else { throw DeckError.decoding("no request in the answer") }
        return row
    }

    public func loginRequest(id: String) async throws -> LoginRequest {
        let envelope: LoginRequestEnvelope = try await decode(makeRequest(
            path: "/v1/logins/requests/\(segment(id))", method: "GET", escaped: true))
        guard let row = envelope.request else { throw DeckError.decoding("no request in the answer") }
        return row
    }

    /// The Mac's long-poll. The deck holds it up to `wait` seconds.
    public func nextLoginRequest(node: String, wait: TimeInterval) async throws -> LoginRequest? {
        var request = try makeRequest(path: "/v1/logins/requests/next", method: "GET", query: [
            URLQueryItem(name: "node", value: node),
            URLQueryItem(name: "wait", value: String(Int(wait))),
        ])
        request.timeoutInterval = wait + 20
        let envelope: LoginRequestEnvelope = try await decode(request)
        return envelope.request
    }

    /// The Mac's report: a status, a count and a sentence — never a cookie.
    public func reportLoginRequest(id: String, status: String, outcome: String?, desks: Int,
                                   detail: String?) async throws {
        var body: [String: Any] = ["status": status, "desks": desks]
        if let outcome { body["outcome"] = outcome }
        if let detail { body["detail"] = detail }
        var request = try makeRequest(path: "/v1/logins/requests/\(segment(id))/result",
                                      method: "POST", escaped: true)
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        try await raw(request)
    }
}

// MARK: - His Macs (the live view's entry point)

/// One row of `GET /v1/nodes`: enough for "Your Mac" on the phone.
public struct MacNodeSummary: Decodable, Equatable, Sendable, Identifiable {
    public let nodeId: String
    public let name: String
    public let online: Bool
    public let controlLive: Bool

    public var id: String { nodeId }
    /// The name the desk screen viewer reads this Mac's live view by.
    public var screenName: String { MacScreenName.agent(nodeId: nodeId) }

    private struct Control: Decodable { let live: Bool? }
    private enum CodingKeys: String, CodingKey { case name, online, control, nodeId = "node_id" }

    public init(nodeId: String, name: String, online: Bool, controlLive: Bool) {
        self.nodeId = nodeId
        self.name = name
        self.online = online
        self.controlLive = controlLive
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = try c.decode(String.self, forKey: .nodeId)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? "Mac"
        online = try c.decodeIfPresent(Bool.self, forKey: .online) ?? false
        controlLive = (try? c.decodeIfPresent(Control.self, forKey: .control))??.live ?? false
    }
}

public protocol MacNodesListing: Sendable {
    func macNodes() async throws -> [MacNodeSummary]
}

extension HTTPDeckClient: MacNodesListing {
    public func macNodes() async throws -> [MacNodeSummary] {
        struct Listing: Decodable { let nodes: [MacNodeSummary] }
        let listing: Listing = try await get("/v1/nodes")
        return listing.nodes
    }
}

// MARK: - Standing approvals (§23)

extension HTTPDeckClient: StandingApprovalsClient {
    private struct PolicyList: Decodable { let policies: Lossy<StandingPolicy>? }
    private struct PolicyAnswer: Decodable { let policy: StandingPolicy }
    private struct AuditList: Decodable { let audit: Lossy<StandingAuditLine>? }

    public func standingApprovals(status: String?, desk: String?) async throws -> [StandingPolicy] {
        var query: [URLQueryItem] = []
        if let status, !status.isEmpty { query.append(URLQueryItem(name: "status", value: status)) }
        if let desk, !desk.isEmpty { query.append(URLQueryItem(name: "desk", value: desk)) }
        let list: PolicyList = try await standing(makeRequest(
            path: "/v1/standing-approvals", method: "GET", query: query.isEmpty ? nil : query))
        return list.policies?.elements ?? []
    }

    public func createStanding(_ draft: StandingDraft) async throws -> StandingPolicy {
        try await policy("/v1/standing-approvals", "POST", Self.standingBody(draft, create: true))
    }

    public func updateStanding(id: String, _ draft: StandingDraft) async throws -> StandingPolicy {
        try await policy("/v1/standing-approvals/\(segment(id))", "PATCH", Self.standingBody(draft, create: false))
    }

    public func approveStanding(id: String) async throws -> StandingPolicy {
        try await policy("/v1/standing-approvals/\(segment(id))/approve", "POST", nil)
    }

    public func revokeStanding(id: String) async throws -> StandingPolicy {
        try await policy("/v1/standing-approvals/\(segment(id))", "DELETE", nil)
    }

    public func standingAudit(desk: String?, policyID: String?, limit: Int) async throws -> [StandingAuditLine] {
        var query = [URLQueryItem(name: "limit", value: String(min(max(limit, 1), 1000)))]
        if let desk, !desk.isEmpty { query.append(URLQueryItem(name: "desk", value: desk)) }
        if let policyID, !policyID.isEmpty { query.append(URLQueryItem(name: "policy_id", value: policyID)) }
        let list: AuditList = try await standing(makeRequest(
            path: "/v1/standing-approvals/audit", method: "GET", query: query))
        return list.audit?.elements ?? []
    }

    /// The create/PATCH body. A PATCH sends every editable field (never `kind`,
    /// which the deck does not let an edit change), `null` where there is none.
    static func standingBody(_ d: StandingDraft, create: Bool) throws -> Data {
        var body: [String: Any] = [
            "desk": d.desk, "tool": d.tool, "pattern": d.pattern, "note": d.note,
            "limits": try JSONSerialization.jsonObject(with: DeckCoding.encoder.encode(d.limits)),
            "expires_at": d.expiresAt.map { $0.timeIntervalSince1970 } ?? NSNull(),
        ]
        if create { body["kind"] = d.kind.wire }
        return try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
    }

    private func policy(_ escapedPath: String, _ method: String, _ body: Data?) async throws -> StandingPolicy {
        var request = try makeRequest(path: escapedPath, method: method, escaped: true)
        if let body {
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            request.httpBody = body
        }
        let answer: PolicyAnswer = try await standing(request)
        return answer.policy
    }

    /// Like `decode`, but a refusal keeps its §23 `reason`: `revoked` and
    /// `missing_field` mean something else on other routes, so they are read
    /// as standing-approval refusals only here.
    private func standing<T: Decodable>(_ request: URLRequest) async throws -> T {
        Diagnostics.count("http.request")
        let (data, response) = try await performer.perform(request)
        DeckOwner.learn(from: response)
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError.standing(status: response.statusCode, body: data)
        }
        do {
            return try DeckCoding.decoder.decode(T.self, from: data)
        } catch {
            throw DeckError.decoding(String(describing: error))
        }
    }
}

// MARK: - Standing approval from a card (§23)

extension HTTPDeckClient: StandingCardClient {
    /// Posts to the route the deck put on the card. Only that one shape is
    /// accepted: the bearer never goes to a path the card made up.
    public func standFromCard(route: String, _ body: StandingFromCard) async throws -> StandingFromCardResult {
        guard route.hasPrefix("/v1/approvals/"), route.hasSuffix("/standing"), !route.contains("..") else {
            throw DeckError.standingRefused(.badPolicy, detail: "the card's route is not a standing route")
        }
        var object: [String: Any] = [
            "limits": try JSONSerialization.jsonObject(with: DeckCoding.encoder.encode(body.limits)),
        ]
        if let at = body.expiresAt { object["expires_at"] = at.timeIntervalSince1970 }
        if !body.note.isEmpty { object["note"] = body.note }
        var request = try makeRequest(path: route, method: "POST")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
        return try await standing(request)
    }
}

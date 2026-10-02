import Foundation
import Combine

/// **A phone call with a desk.** "i meant to talk with the agent in a phone
/// call on the app and he will do and check stuff while i talk with him."
///
/// The voice on the line is an OpenAI Realtime session the deck minted for
/// this call; the *work* stays with the desk. When he asks for something the
/// voice calls `send_to_desk(text)`, which posts his request into the desk's
/// own thread (K2: as him, channel `voice`, this call's id) — the same place a
/// typed message goes — and the desk gets on with it. Every new line the desk
/// writes while the call is up is handed back to the voice as
/// "[<Desk> update] …" so it can tell him how it is going, while he keeps
/// talking.
///
/// **A thin voice over the desk** (owner, 2026-10-01: "On the chat, Atlas
/// performs way better than in a call"). Measured on the box: two thirds of
/// what he said on a call was answered by the voice model on its own, and
/// between a hand-off and the desk's answer it filled the wait with its own
/// guesses. So: the voice acks in a few words and stops; a second response is
/// asked for only when it said nothing at all, and then only for the ack. What
/// it speaks next is the desk's line, under response instructions that forbid
/// adding to it. A decision card the desk raises is read out as choices, and
/// his spoken pick answers it (`answer_card`, K4's `POST /v1/decisions/{id}`).
///
/// Barge-in: the server's VAD says `input_audio_buffer.speech_started` the
/// moment he talks, and whatever the agent was saying stops on this Mac at
/// once (the server cancels the rest of the response itself).
@MainActor
public final class RealtimeCallSession: ObservableObject {
    public enum Phase: String, Sendable {
        /// Dialling: the socket is opening.
        case connecting
        /// Line open, nobody talking.
        case listening
        /// He is talking (server VAD).
        case hearing
        /// He stopped; the voice is working out what to say.
        case thinking
        /// The agent's voice is playing.
        case speaking
        case ended
    }

    @Published public private(set) var phase: Phase = .connecting {
        didSet { if oldValue != phase { VoiceTrace.note("call.phase", "\(oldValue.rawValue)->\(phase.rawValue)") } }
    }
    /// His loudness 0...1; stops updating while he is silent.
    @Published public private(set) var micLevel: Float = 0
    /// The agent's loudness 0...1 as its voice plays.
    @Published public private(set) var agentLevel: Float = 0
    /// Requests handed to the desk that have not come back yet.
    @Published public private(set) var deskRequestsInFlight = 0
    /// What the agent is saying, as it says it (the audio transcript).
    @Published public private(set) var caption = ""
    @Published public private(set) var isMuted = false
    /// The last thing that went wrong, in one sentence.
    @Published public private(set) var problem: String?

    public var isWorking: Bool { deskRequestsInFlight > 0 }

    public let desk: String
    public let displayName: String
    public let callID: String
    public let threadID: String

    /// The call dropped on its own (socket closed, fatal error). Not called
    /// after `hangUp()`.
    public var onDropped: ((String) -> Void)?

    private let offer: RealtimeOffer
    private let transport: RealtimeTransport
    private let audio: CallAudio
    private let calls: CallClient
    private var receiver: Task<Void, Never>?
    /// The same receive loop, kept after `hangUp()` clears `receiver`, so a
    /// test can wait for the line to finish closing instead of polling.
    private var receiving: Task<Void, Never>?
    /// What time it is, for judging the minted pass. Injected so a test's pass
    /// is judged by the test's clock, not by how long the suite took to reach it.
    private let now: () -> Date
    private var responseActive = false
    /// A `response.create` asked for while one was running: sent at its end.
    /// The strongest one asked for wins (`Speak`), so the desk's rules are
    /// never traded for a bare one.
    private var responsePending: Speak?
    private var handledCalls: Set<String> = []
    /// The response now running has said something out loud.
    private var currentResponseSpoke = false
    /// Tool calls made by the response now running.
    private var callsThisResponse: [String] = []
    /// send_to_desk calls whose response already said the ack.
    private var acked: Set<String> = []
    /// Decision cards read out on this call and not yet answered, by id.
    private var cards: [String: Decision] = [:]
    private var pendingAudio = Data()
    /// The agent's own voice in the mic is not him: see `BargeInGate`.
    private var gate = BargeInGate()
    /// The reply being spoken, as streamed; `caption` is what of it is shown.
    private var transcript = ""
    /// The last transcript post, so the next one goes after it.
    private var transcriptTail: Task<Void, Never>?
    /// ~100 ms of 24 kHz PCM16 per append, not 47 tiny frames a second.
    static let appendBytes = 4_800
    private var hungUp = false
    /// Everything sent goes through one queue, in order: an item and the
    /// `response.create` that answers it must not swap places.
    private var outbox: AsyncStream<String>.Continuation?
    private var sender: Task<Void, Never>?

    public init(offer: RealtimeOffer, desk: String, displayName: String, callID: String, threadID: String,
                transport: RealtimeTransport, audio: CallAudio, calls: CallClient,
                now: @escaping () -> Date = Date.init) {
        self.offer = offer
        self.now = now
        self.desk = desk
        self.displayName = displayName.isEmpty ? desk : displayName
        self.callID = callID
        self.threadID = threadID
        self.transport = transport
        self.audio = audio
        self.calls = calls
    }

    // MARK: - Dial and hang up

    /// Open the socket, set the audio formats, open the mic. False when the
    /// line could not be opened — the caller falls back to on-Mac speech.
    @discardableResult
    public func start() async -> Bool {
        VoiceTrace.note("call.realtime.connect", "host=\(offer.wsURL.host ?? "?") model=\(offer.model ?? "?")")
        if let expires = offer.expiresAt, expires < now() {
            problem = "The call's voice pass had already expired."
            VoiceTrace.fail("call.realtime.expired", "expires_at=\(expires)")
            return false
        }
        do {
            try await transport.connect(url: offer.wsURL, secret: offer.clientSecret)
            try await send(Self.sessionUpdate())
        } catch {
            problem = "Couldn't reach the voice service."
            VoiceTrace.fail("call.realtime.connect_failed", "\(error)")
            transport.close()
            return false
        }
        audio.onMicChunk = { [weak self] chunk in self?.micChunk(chunk) }
        audio.onMicLevel = { [weak self] level in self?.micLevelChanged(level) }
        audio.onOutputLevel = { [weak self] level in self?.outputLevel(level) }
        do {
            try audio.start()
        } catch {
            problem = "The microphone would not open."
            VoiceTrace.fail("call.realtime.audio_failed", "\(error)")
            transport.close()
            return false
        }
        let (stream, continuation) = AsyncStream<String>.makeStream()
        outbox = continuation
        let transport = self.transport
        sender = Task {
            for await text in stream {
                do { try await transport.send(text) } catch {
                    VoiceTrace.fail("call.realtime.send_failed", "\(error)")
                }
            }
        }
        phase = .listening
        receiver = Task { [weak self] in await self?.receiveLoop() }
        receiving = receiver
        return true
    }

    public func hangUp() {
        guard !hungUp else { return }
        hungUp = true
        receiver?.cancel()
        receiver = nil
        outbox?.finish()
        outbox = nil
        audio.stop()
        transport.close()
        micLevel = 0
        agentLevel = 0
        phase = .ended
        VoiceTrace.note("call.realtime.hangup", "call=\(callID)")
    }

    /// Returns once the receive loop has stopped — after a drop, once
    /// `onDropped` has run.
    func untilTheLineCloses() async {
        await receiving?.value
    }

    public func setMuted(_ muted: Bool) {
        isMuted = muted
        if muted { pendingAudio.removeAll(); gate.reset(); micLevel = 0 }
    }

    // MARK: - The desk's news

    /// A new line from the desk while the call is up: the voice hears it and
    /// tells him.
    public func deskUpdate(_ text: String) {
        guard !hungUp else { return }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        let clipped = trimmed.count > 1_200 ? String(trimmed.prefix(1_200)) + "…" : trimmed
        VoiceTrace.note("call.realtime.desk_update", "chars=\(trimmed.count)")
        say("[\(displayName) update] \(clipped)")
        requestResponse(.desk)
    }

    /// A decision card the desk raised while the call is up: read out as
    /// choices, answered by his voice through `answer_card`.
    public func deskAsk(_ card: Decision) {
        guard !hungUp, card.state != .answered, cards[card.id] == nil else { return }
        cards[card.id] = card
        let options = card.options.map(\.label).joined(separator: "; ")
        let own = card.allowCustom ? " He may also answer in his own words." : ""
        VoiceTrace.note("call.realtime.desk_ask", "card=\(card.id) options=\(card.options.count)")
        say("[\(displayName) asks you to pick] (card \(card.id)) \(card.prompt) Options: \(options).\(own)")
        requestResponse(.desk)
    }

    private func say(_ text: String) {
        sendNow([
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [["type": "input_text", "text": text]],
            ],
        ])
    }

    // MARK: - What a response may say

    /// What a `response.create` is for. Ordered: a queued request keeps the
    /// strongest rules asked for.
    enum Speak: Int, Comparable {
        /// The model's own turn, under the session's instructions.
        case free
        /// Only the few-word ack: the desk has his request.
        case ack
        /// Only what the desk just sent.
        case desk

        static func < (a: Speak, b: Speak) -> Bool { a.rawValue < b.rawValue }
    }

    func rules(_ kind: Speak) -> String? {
        switch kind {
        case .free:
            return nil
        case .ack:
            return "The desk has his request and is working on it. Say only a two-to-four word "
                + "acknowledgement in the language he is speaking, like \"On it.\" — nothing else. "
                + "Do not answer it, guess, or add anything."
        case .desk:
            return "Speak to him now what \(displayName) just sent: the newest \"[\(displayName) update]\" "
                + "or \"[\(displayName) asks you to pick]\" messages you have not spoken yet. Use the "
                + "language he is speaking on this call, condensed to at most three short spoken sentences. "
                + "Say only what it says. Add no facts, numbers, names or promises of your own, and never "
                + "contradict it. For a pick, ask the question and read every option by its label; when he "
                + "answers, call answer_card with that card's id and his choice. No markdown, links, paths "
                + "or code. Warm, brief, direct."
        }
    }

    // MARK: - Events

    private func receiveLoop() async {
        while !Task.isCancelled {
            do {
                let text = try await transport.receive()
                handle(text)
            } catch {
                guard !hungUp, !Task.isCancelled else { return }
                VoiceTrace.fail("call.realtime.dropped", "\(error)")
                problem = "The call dropped."
                let reason = "\(error)"
                hangUp()
                onDropped?(reason)
                return
            }
        }
    }

    /// One server event. Internal so tests drive it directly.
    func handle(_ text: String) {
        guard let data = text.data(using: .utf8),
              let event = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = event["type"] as? String else { return }
        switch type {
        case "session.created", "session.updated":
            VoiceTrace.note("call.realtime.\(type)")
        case "input_audio_buffer.speech_started":
            // Barge-in: he is talking, the agent stops — on this Mac, now.
            // While the agent plays the server only hears what `BargeInGate`
            // let through, so this is him, not the speaker's echo.
            let ms = event["audio_start_ms"] as? Int ?? -1
            VoiceTrace.note("call.realtime.speech_started",
                            "at_ms=\(ms) playing=\(audio.isPlaying) gate_open=\(gate.isOpen)")
            if audio.isPlaying || phase == .speaking { VoiceTrace.note("call.realtime.barge_in") }
            audio.flushPlayback()
            agentLevel = 0
            setCaption("")
            phase = .hearing
        case "input_audio_buffer.speech_stopped":
            VoiceTrace.note("call.realtime.speech_stopped", "at_ms=\(event["audio_end_ms"] as? Int ?? -1)")
            phase = .thinking
        case "response.created":
            responseActive = true
            currentResponseSpoke = false
            callsThisResponse = []
            setCaption("")
        case "response.output_audio.delta", "response.audio.delta":
            guard let b64 = event["delta"] as? String, let pcm = Data(base64Encoded: b64) else { return }
            audio.play(pcm)
            if phase != .hearing { phase = .speaking }
        case "response.output_audio_transcript.delta", "response.audio_transcript.delta":
            if let delta = event["delta"] as? String {
                if !delta.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { currentResponseSpoke = true }
                transcript += delta
                let shown = CallCaption.shown(transcript)
                if shown != caption { caption = shown }
            }
        case "conversation.item.input_audio_transcription.completed":
            record(.caller, event["transcript"] as? String)
        case "response.output_audio_transcript.done", "response.audio_transcript.done":
            record(.agent, event["transcript"] as? String)
        case "response.function_call_arguments.done":
            functionCall(callID: event["call_id"] as? String, name: event["name"] as? String,
                         arguments: event["arguments"] as? String)
        case "response.done":
            responseActive = false
            if let response = event["response"] as? [String: Any],
               let output = response["output"] as? [[String: Any]] {
                for item in output where item["type"] as? String == "function_call" {
                    if let id = item["call_id"] as? String, !callsThisResponse.contains(id) {
                        callsThisResponse.append(id)
                    }
                    functionCall(callID: item["call_id"] as? String, name: item["name"] as? String,
                                 arguments: item["arguments"] as? String)
                }
            }
            if currentResponseSpoke { acked.formUnion(callsThisResponse) }
            if let pending = responsePending {
                responsePending = nil
                responseActive = true
                sendNow(responseCreate(pending))
            }
            if !audio.isPlaying, phase == .speaking || phase == .thinking { phase = .listening }
        case "error":
            let error = event["error"] as? [String: Any]
            let code = error?["code"] as? String ?? "?"
            let message = error?["message"] as? String ?? ""
            VoiceTrace.fail("call.realtime.error", "code=\(code) \(message)")
            if code == "conversation_already_has_active_response" {
                responsePending = max(responsePending ?? .free, .free)
            }
        default:
            break
        }
    }

    // MARK: - What was said

    /// Each finished line goes to the deck as it happens, so the desk's
    /// thread and the next call remember this one ("voice call does not
    /// remember any of previous calls"). A deck without the route just
    /// forgets; the call is untouched.
    private func record(_ role: CallLine.Role, _ text: String?) {
        guard let text = text?.trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty else { return }
        let calls = self.calls, call = callID, previous = transcriptTail
        // In the order they were said: each post waits for the one before.
        transcriptTail = Task {
            await previous?.value
            do { try await calls.postTranscript(callID: call, lines: [CallLine(role: role, text: text)]) } catch {
                VoiceTrace.fail("call.realtime.transcript_failed", "\(error)")
            }
        }
    }

    // MARK: - send_to_desk

    private func functionCall(callID toolCall: String?, name: String?, arguments: String?) {
        guard let toolCall, !handledCalls.contains(toolCall) else { return }
        handledCalls.insert(toolCall)
        if !callsThisResponse.contains(toolCall) { callsThisResponse.append(toolCall) }
        if currentResponseSpoke { acked.insert(toolCall) }
        let args = arguments.flatMap { $0.data(using: .utf8) }
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
        if name == "answer_card" {
            answerCard(toolCall, args)
            return
        }
        guard name == "send_to_desk" else {
            VoiceTrace.fail("call.realtime.unknown_tool", name ?? "nil")
            returnToolOutput(toolCall, ["ok": false, "error": "unknown tool \(name ?? "")"])
            return
        }
        guard let text = (args?["text"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !text.isEmpty else {
            returnToolOutput(toolCall, ["ok": false, "error": "text is required"])
            return
        }
        deskRequestsInFlight += 1
        VoiceTrace.note("call.realtime.send_to_desk", "chars=\(text.count) thread=\(threadID)")
        let calls = self.calls, thread = threadID, call = callID
        Task { [weak self] in
            do {
                _ = try await calls.sendVoice(threadID: thread, text: text, callID: call)
                VoiceTrace.note("call.realtime.send_to_desk.ok")
                self?.deskRequestDone(toolCall, ["ok": true])
            } catch {
                VoiceTrace.fail("call.realtime.send_to_desk.failed", "\(error)")
                let why = (error as? DeckError)?.userFacingText ?? "the deck did not answer"
                self?.deskRequestDone(toolCall, ["ok": false, "error": why])
            }
        }
    }

    /// Handed over: the ack was said in the response that called the tool, so
    /// nothing more is asked for — the next thing he hears is the desk. If it
    /// said nothing, one response asks for the ack and only the ack.
    private func deskRequestDone(_ toolCall: String, _ output: [String: Any]) {
        deskRequestsInFlight = max(0, deskRequestsInFlight - 1)
        guard !hungUp else { return }
        let ok = output["ok"] as? Bool == true
        returnToolOutput(toolCall, output, then: !ok ? .free : acked.contains(toolCall) ? nil : .ack)
    }

    // MARK: - answer_card

    private func answerCard(_ toolCall: String, _ args: [String: Any]?) {
        let id = (args?["card_id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        let choice = (args?["choice"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard let card = cards[id] else {
            returnToolOutput(toolCall, ["ok": false, "error": "no open card \(id) on this call"])
            return
        }
        guard let value = Self.value(for: choice, on: card) else {
            let labels = card.options.map(\.label).joined(separator: ", ")
            returnToolOutput(toolCall, ["ok": false, "error": "pick one of: \(labels)"])
            return
        }
        guard let decisions = calls as? DecisionClient else {
            returnToolOutput(toolCall, ["ok": false, "error": "this deck cannot take a card answer; tap it in the app"])
            return
        }
        let picked = card.options.first { $0.value == value }?.label ?? value
        VoiceTrace.note("call.realtime.answer_card", "card=\(id)")
        Task { [weak self] in
            do {
                try await decisions.answerDecision(id: id, value: value)
                self?.cards[id] = nil
                self?.returnToolOutput(toolCall, ["ok": true, "picked": picked])
            } catch {
                VoiceTrace.fail("call.realtime.answer_card.failed", "\(error)")
                let why = (error as? DeckError)?.userFacingText ?? "the deck did not answer"
                self?.returnToolOutput(toolCall, ["ok": false, "error": why])
            }
        }
    }

    /// His spoken choice as the card's answer: an option named by label or
    /// value (case and end punctuation aside), else his own words when the
    /// card takes them, else nil.
    static func value(for choice: String, on card: Decision) -> String? {
        func norm(_ s: String) -> String {
            s.lowercased().trimmingCharacters(in: .whitespacesAndNewlines.union(.punctuationCharacters))
        }
        let said = norm(choice)
        guard !said.isEmpty else { return nil }
        if let option = card.options.first(where: { norm($0.label) == said || norm($0.value) == said }) {
            return option.value
        }
        return card.allowCustom ? choice : nil
    }

    private func returnToolOutput(_ toolCall: String, _ output: [String: Any], then next: Speak? = .free) {
        guard !hungUp else { return }
        let body = (try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]))
            .map { String(decoding: $0, as: UTF8.self) } ?? "{}"
        sendNow([
            "type": "conversation.item.create",
            "item": ["type": "function_call_output", "call_id": toolCall, "output": body],
        ])
        if let next { requestResponse(next) }
    }

    /// One response at a time: asking while one runs is an error, so it waits.
    private func requestResponse(_ kind: Speak = .free) {
        if responseActive {
            responsePending = max(responsePending ?? kind, kind)
        } else {
            responseActive = true
            sendNow(responseCreate(kind))
        }
    }

    func responseCreate(_ kind: Speak) -> [String: Any] {
        guard let rules = rules(kind) else { return ["type": "response.create"] }
        return ["type": "response.create", "response": ["instructions": rules]]
    }

    // MARK: - Audio

    private func micChunk(_ chunk: Data) {
        guard !isMuted, !hungUp, phase != .connecting else { return }
        pendingAudio.append(gate.pass(chunk, agentPlaying: audio.isPlaying))
        guard pendingAudio.count >= Self.appendBytes else { return }
        let audio = pendingAudio
        pendingAudio.removeAll(keepingCapacity: true)
        sendNow(["type": "input_audio_buffer.append", "audio": audio.base64EncodedString()])
    }

    /// His level only moves the face while he could be talking: while the
    /// agent speaks the face follows the agent, and redrawing for the echo in
    /// the mic only cost CPU.
    private func micLevelChanged(_ level: Float) {
        let shown = isMuted || phase == .speaking || audio.isPlaying ? 0 : level
        if micLevel != shown { micLevel = shown }
    }

    private func setCaption(_ text: String) {
        transcript = text
        if caption != text { caption = text }
    }

    private func outputLevel(_ level: Float) {
        agentLevel = level
        if level == 0, !audio.isPlaying, phase == .speaking {
            phase = responseActive ? .thinking : .listening
        }
    }

    // MARK: - Wire

    /// The minted session already carries the desk's instructions, voice and
    /// tools; this only pins what this Mac sends and plays.
    static func sessionUpdate() -> [String: Any] {
        [
            "type": "session.update",
            "session": [
                "type": "realtime",
                "audio": [
                    "input": [
                        "format": ["type": "audio/pcm", "rate": 24_000],
                        "turn_detection": ["type": "server_vad", "create_response": true, "interrupt_response": true],
                    ],
                    "output": ["format": ["type": "audio/pcm", "rate": 24_000]],
                ],
            ],
        ]
    }

    private func send(_ object: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: object)
        try await transport.send(String(decoding: data, as: UTF8.self))
    }

    private func sendNow(_ object: [String: Any]) {
        guard !hungUp, let outbox,
              let data = try? JSONSerialization.data(withJSONObject: object) else { return }
        outbox.yield(String(decoding: data, as: UTF8.self))
    }
}

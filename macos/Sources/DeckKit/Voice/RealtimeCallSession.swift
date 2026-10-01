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
    private var responsePending = false
    private var handledCalls: Set<String> = []
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
        sendNow([
            "type": "conversation.item.create",
            "item": [
                "type": "message",
                "role": "user",
                "content": [["type": "input_text", "text": "[\(displayName) update] \(clipped)"]],
            ],
        ])
        requestResponse()
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
            setCaption("")
        case "response.output_audio.delta", "response.audio.delta":
            guard let b64 = event["delta"] as? String, let pcm = Data(base64Encoded: b64) else { return }
            audio.play(pcm)
            if phase != .hearing { phase = .speaking }
        case "response.output_audio_transcript.delta", "response.audio_transcript.delta":
            if let delta = event["delta"] as? String {
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
                    functionCall(callID: item["call_id"] as? String, name: item["name"] as? String,
                                 arguments: item["arguments"] as? String)
                }
            }
            if responsePending {
                responsePending = false
                sendNow(["type": "response.create"])
            }
            if !audio.isPlaying, phase == .speaking || phase == .thinking { phase = .listening }
        case "error":
            let error = event["error"] as? [String: Any]
            let code = error?["code"] as? String ?? "?"
            let message = error?["message"] as? String ?? ""
            VoiceTrace.fail("call.realtime.error", "code=\(code) \(message)")
            if code == "conversation_already_has_active_response" {
                responsePending = true
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
        guard name == "send_to_desk" else {
            VoiceTrace.fail("call.realtime.unknown_tool", name ?? "nil")
            returnToolOutput(toolCall, ["ok": false, "error": "unknown tool \(name ?? "")"])
            return
        }
        let args = arguments.flatMap { $0.data(using: .utf8) }
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] }
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

    private func deskRequestDone(_ toolCall: String, _ output: [String: Any]) {
        deskRequestsInFlight = max(0, deskRequestsInFlight - 1)
        guard !hungUp else { return }
        returnToolOutput(toolCall, output)
    }

    private func returnToolOutput(_ toolCall: String, _ output: [String: Any]) {
        let body = (try? JSONSerialization.data(withJSONObject: output, options: [.sortedKeys]))
            .map { String(decoding: $0, as: UTF8.self) } ?? "{}"
        sendNow([
            "type": "conversation.item.create",
            "item": ["type": "function_call_output", "call_id": toolCall, "output": body],
        ])
        requestResponse()
    }

    /// One response at a time: asking while one runs is an error, so it waits.
    private func requestResponse() {
        if responseActive {
            responsePending = true
        } else {
            responseActive = true
            sendNow(["type": "response.create"])
        }
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

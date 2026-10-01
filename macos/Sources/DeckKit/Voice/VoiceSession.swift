import Foundation
import Combine

/// **Talking to a desk out loud.** "i need a way to talk live with it."
///
/// Two ways in, one machine:
/// - **Hold to talk** (the mic in the composer): hold, speak, let go. The first
///   press opens a call quietly (K6 needs a `call_id` for the voice channel)
///   and later presses reuse it; the call bar shows it, and it hangs itself up
///   after `quietHangUp` of nothing happening.
/// - **Call** (the header menu): hands-free. The mic stays open, a pause of
///   `silenceWindow` ends what he said, and it keeps listening while the desk
///   thinks, so he can talk over it.
///
/// Either way: what he said goes out on K2's `voice` channel with the call's
/// id, "Asking <Name>…" is said locally the moment he finishes (before any
/// network), and every new line from the desk while the call is up is spoken
/// in the desk's voice — the first two sentences, never code, paths or links
/// (`Speakable`). Talking stops the desk mid-word (barge-in).
///
/// State: `idle → listening → sending → waiting → speaking → idle`. On a call
/// `listening` also means "between turns, mic open".
@MainActor
public final class VoiceSession: ObservableObject {
    public enum State: String, Sendable {
        case idle, listening, sending, waiting, speaking
    }

    public struct Call: Equatable, Sendable {
        public var id: String
        public var threadID: String
        public var desk: String
        public var startedAt: Date
        /// Open mic (the header's Call) or hold-to-talk (the composer's mic).
        public var handsFree: Bool
        /// K6's 201 `voice`, when it named one.
        public var voice: DeskVoice?
        /// C3: the live voice the deck minted for this call, if it could.
        public var realtime: RealtimeOffer? = nil
        public var realtimeError: String? = nil
        /// How to say the called desk's name, wherever he is reading.
        public var displayName: String = ""

        public var name: String { displayName.isEmpty ? desk : displayName }
    }

    /// One call at a time: calling another desk mid-call asks first.
    public struct CallSwitch: Equatable, Sendable {
        public var from: String
        public var to: String
        public var question: String { "End call with \(from) and call \(to)?" }
    }

    /// Everything a phone call needs to be dialled.
    public struct RealtimeDial {
        public var offer: RealtimeOffer
        public var desk: String
        public var displayName: String
        public var callID: String
        public var threadID: String
        public var calls: CallClient
    }

    @Published public private(set) var state: State = .idle {
        didSet { if oldValue != state { VoiceTrace.note("session.state", "\(oldValue.rawValue)->\(state.rawValue)") } }
    }
    /// His loudness 0...1 while the Mac listens (Apple speech). Updates stop
    /// when he is silent, so the ring costs nothing at rest.
    @Published public private(set) var micLevel: Float = 0
    /// The desk's voice, word by word, while the Mac speaks for it.
    @Published public private(set) var speakingLevel: Float = 0
    /// C3: the phone call, when the deck could dial a live voice for it.
    @Published public private(set) var realtime: RealtimeCallSession?
    @Published public private(set) var call: Call?
    @Published public private(set) var isMuted = false
    /// He asked to call another desk while on a call: waiting for his yes.
    @Published public private(set) var pendingSwitch: CallSwitch?
    /// One plain sentence for the composer when he cannot be heard or the
    /// call could not start. Typing is never affected.
    @Published public private(set) var notice: String?
    /// What the recogniser has heard so far, while he talks.
    @Published public private(set) var heard = ""

    /// The desk this session is pointed at (wire name) and how to say its name.
    public private(set) var desk: String?
    public private(set) var displayName = ""

    /// A pause this long ends an utterance on a hands-free call.
    public var silenceWindow: TimeInterval = 1.2
    /// Waiting longer than this with nothing from the desk goes back to idle.
    public var waitLimit: TimeInterval = 180
    /// A hold-to-talk call with nothing happening for this long is hung up.
    public var quietHangUp: TimeInterval = 180

    private let input: SpeechInput
    private let output: SpeechOutput
    /// Built on the first call, not when a thread opens.
    private let makeCalls: () -> CallClient?
    private lazy var calls: CallClient? = makeCalls()
    /// Builds the phone call's session (real socket and audio in the app,
    /// fakes in tests). Nil: this build only has on-Mac speech.
    private let makeRealtime: ((RealtimeDial) -> RealtimeCallSession)?
    /// "Spoken replies": off, hold-to-talk answers stay text. A hands-free
    /// call is a call and always speaks.
    private let spokenReplies: () -> Bool

    private var rosterVoice: DeskVoice?
    private var deskWorking = false
    private var seen: Set<String> = []
    /// The called desk's `seen`, kept while he reads other desks: what it
    /// said meanwhile is news when he comes back.
    private var callSeen: Set<String> = []
    private var queue: [(line: SpokenLine, isAcknowledgement: Bool)] = []
    private var current: (line: SpokenLine, isAcknowledgement: Bool)?
    /// Replies spoken since his last utterance.
    private var answered = 0
    /// Something he said went out and its answer is not over yet.
    private var inTurn = false
    private var wantsHold = false
    private var holding = false
    private var starting = false
    private var silenceTimer: Task<Void, Never>?
    private var waitTimer: Task<Void, Never>?
    private var quietTimer: Task<Void, Never>?
    /// The deck being told a dropped call is over (`POST /v1/calls/{id}/end`).
    private(set) var ending: Task<Void, Never>?

    public init(input: SpeechInput, output: SpeechOutput, calls: @escaping () -> CallClient?,
                realtime: ((RealtimeDial) -> RealtimeCallSession)? = nil,
                spokenReplies: @escaping () -> Bool = { SpokenReplies.isOn() }) {
        self.spokenReplies = spokenReplies
        self.input = input
        self.output = output
        self.makeCalls = calls
        self.makeRealtime = realtime
        output.onFinish = { [weak self] in self?.lineFinished() }
        output.onLevel = { [weak self] level in
            guard let self, self.speakingLevel != level else { return }
            self.speakingLevel = level
        }
        input.onLevel = { [weak self] level in
            guard let self, self.micLevel != level else { return }
            self.micLevel = level
        }
    }

    // MARK: - Feed

    /// The open conversation, handed over every time it changes. The first
    /// sight of a desk marks its history as heard: opening a thread never
    /// reads yesterday aloud.
    public func sync(desk newDesk: String?, displayName: String, voice: DeskVoice?,
                     working: Bool, messages: [Message]) {
        if newDesk != desk {
            // A phone call belongs to the app, not to the thread on screen:
            // "when i move to other agent the call gets disconnected".
            let phone = call?.handsFree == true ? call : nil
            if let phone, desk == phone.desk { callSeen = seen }
            leaveDesk()
            desk = newDesk
            self.displayName = displayName
            rosterVoice = voice
            deskWorking = working
            guard let phone, newDesk == phone.desk else {
                seen = Set(messages.map(\.id))
                return
            }
            seen = callSeen
        }
        // Only a real change publishes: this runs on every transcript update.
        if self.displayName != displayName { self.displayName = displayName }
        rosterVoice = voice
        deskWorking = working

        let onCalledDesk = call.map { $0.desk == desk } ?? false
        for message in messages where !seen.contains(message.id) {
            seen.insert(message.id)
            guard onCalledDesk else { continue }
            if let realtime, isFromDesk(message) {
                // On a phone call the voice relays the desk's progress itself.
                realtime.deskUpdate(message.text)
                continue
            }
            guard call != nil, isFromDesk(message),
                  let words = Speakable.text(message.text) else { continue }
            guard speaksAloud else {
                // Text only: the answer still ends his turn.
                answered += 1
                VoiceTrace.note("session.reply_text_only")
                continue
            }
            enqueue(SpokenLine(text: words, desk: message.author.lowercased(), voice: call?.voice ?? rosterVoice),
                    isAcknowledgement: false)
        }
        if state == .waiting, !working, answered > 0, current == nil, queue.isEmpty { settle() }
    }

    /// Read at the moment there is something to say, so the switch wins.
    private var speaksAloud: Bool { call?.handsFree == true || spokenReplies() }

    private func isFromDesk(_ message: Message) -> Bool {
        guard let desk else { return false }
        return message.role == .agent && message.kind == .text
            && message.author.caseInsensitiveCompare(desk) == .orderedSame
    }

    // MARK: - Hold to talk

    public func pressMic() async {
        guard let desk else { return }
        if let open = call, open.handsFree, open.desk != desk {
            notice = "You're on a call with \(open.name) — hang up to talk here. Typing still works."
            return
        }
        stopSpeaking()                                  // barge-in
        if call?.handsFree == true { toggleMute(); return }
        wantsHold = true
        let permission = await input.permission()
        guard case .granted = permission else {
            if case .denied(let sentence) = permission { notice = sentence }
            wantsHold = false
            return
        }
        notice = nil
        // Let go while the prompt was up: a tap, not a hold.
        guard wantsHold, !holding else { return }
        input.cancelsEcho = false
        do {
            try input.start { [weak self] words in self?.didHear(words) }
        } catch {
            notice = (error as? DeckError)?.userFacingText ?? VoicePermission.noMicrophone
            wantsHold = false
            return
        }
        holding = true
        heard = ""
        quietTimer?.cancel()
        state = .listening
    }

    public func releaseMic() async {
        wantsHold = false
        guard holding else { return }
        holding = false
        let words = await input.finish()
        await deliver(words)
    }

    // MARK: - Calls

    /// The header's Call: a phone call with the desk. A live voice when the
    /// deck could mint one (C3), else open-mic on-Mac speech.
    public func startCall() async {
        guard let desk, !starting else { return }
        VoiceTrace.note("call.request", "desk=\(desk)")
        if var open = call {
            if open.desk != desk {
                pendingSwitch = CallSwitch(from: open.name, to: displayName.isEmpty ? desk : displayName)
                return
            }
            guard !open.handsFree else { return }
            open.handsFree = true
            call = open
            beginListening()
            return
        }
        switch await input.permission() {
        case .denied(let sentence): notice = sentence; return
        case .granted: notice = nil
        }
        guard let made = await openCall(desk: desk, handsFree: true) else { return }
        if await dialRealtime(made) { return }
        beginListening()
    }

    /// "End call with Atlas and call Acme?" — yes.
    public func confirmSwitch() async {
        guard pendingSwitch != nil else { return }
        pendingSwitch = nil
        await hangUp()
        await startCall()
    }

    /// … no: the call he is on carries on.
    public func cancelSwitch() {
        pendingSwitch = nil
    }

    /// True when the phone call is up on the live voice.
    private func dialRealtime(_ made: Call) async -> Bool {
        guard let offer = made.realtime else {
            if let why = made.realtimeError {
                VoiceTrace.note("call.realtime.unavailable", why)
                notice = "Live voice isn't available (\(why)) — using this Mac's speech instead. Typing still works."
            }
            return false
        }
        guard let makeRealtime, let calls else { return false }
        let session = makeRealtime(RealtimeDial(offer: offer, desk: made.desk, displayName: made.name,
                                                callID: made.id, threadID: made.threadID, calls: calls))
        realtime = session
        state = .listening
        guard await session.start() else {
            if realtime === session { realtime = nil }
            state = .idle
            notice = "Couldn't connect the live voice — \(session.problem ?? "no answer") Using this Mac's speech instead."
            return false
        }
        // He hung up, or left the desk, while it was dialling.
        guard call?.id == made.id, realtime === session else {
            session.hangUp()
            return true
        }
        session.onDropped = { [weak self, weak session] _ in
            guard let self, let session, self.realtime === session else { return }
            self.notice = "The call to \(self.call?.name ?? self.displayName) dropped. Typing still works."
            // Off this Mac now, in the same turn as the drop; only the deck's
            // POST waits.
            guard let open = self.closeCall() else { return }
            self.ending = Task { await self.endOnDeck(open) }
        }
        return true
    }

    public func hangUp() async {
        guard let open = closeCall() else { return }
        await endOnDeck(open)
    }

    /// Everything on this Mac: the line, the mic, the timers. Nil when no
    /// call was up.
    private func closeCall() -> Call? {
        guard let open = call else { return nil }
        call = nil
        pendingSwitch = nil
        VoiceTrace.note("call.hangup", "call=\(open.id)")
        realtime?.hangUp()
        realtime = nil
        stopSpeaking()
        silenceTimer?.cancel(); waitTimer?.cancel(); quietTimer?.cancel()
        input.cancel()
        holding = false
        isMuted = false
        heard = ""
        state = .idle
        return open
    }

    private func endOnDeck(_ open: Call) async {
        do {
            try await calls?.endCall(id: open.id)
        } catch {
            // 409 already ended, or the deck is away: the call is over on this
            // side either way, and the deck closes it on its own.
        }
    }

    public func toggleMute() {
        guard call?.handsFree == true else { return }
        isMuted.toggle()
        if let realtime {
            realtime.setMuted(isMuted)
            return
        }
        if isMuted {
            silenceTimer?.cancel()
            input.cancel()
            heard = ""
            if state == .listening { state = .idle }
        } else {
            beginListening()
        }
    }

    /// A pause long enough on a hands-free call: what he said so far goes.
    public func endUtterance() async {
        silenceTimer?.cancel()
        guard call?.handsFree == true, !isMuted else { return }
        let words = await input.finish()
        await deliver(words)
        beginListening()
    }

    // MARK: - Internals

    private func beginListening() {
        guard call?.handsFree == true, !isMuted, realtime == nil else { return }
        input.cancelsEcho = true
        do {
            try input.start { [weak self] words in self?.didHear(words) }
        } catch {
            notice = (error as? DeckError)?.userFacingText ?? VoicePermission.noMicrophone
            return
        }
        if state == .idle { state = .listening }
    }

    private func didHear(_ words: String) {
        guard !isMuted, holding || call?.handsFree == true else { return }
        guard !words.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { heard = words; return }
        // Echo guard, behind the engine's own echo cancellation: the speaker's
        // words coming back through the mic are not him talking.
        if let speaking = current?.line.text, Self.isEcho(words, of: speaking) { return }
        heard = words
        if current != nil || !queue.isEmpty {
            stopSpeaking()                              // barge-in
            state = .listening
        }
        guard call?.handsFree == true, !holding else { return }
        silenceTimer?.cancel()
        let window = silenceWindow
        silenceTimer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(window * 1_000_000_000))
            guard !Task.isCancelled else { return }
            await self?.endUtterance()
        }
    }

    static func isEcho(_ heard: String, of spoken: String) -> Bool {
        func plain(_ text: String) -> String {
            text.lowercased().unicodeScalars
                .filter { CharacterSet.alphanumerics.contains($0) || $0 == " " }
                .map(String.init).joined()
                .split(separator: " ").joined(separator: " ")
        }
        let said = plain(heard)
        return !said.isEmpty && plain(spoken).contains(said)
    }

    private func deliver(_ words: String) async {
        let text = words.trimmingCharacters(in: .whitespacesAndNewlines)
        heard = ""
        guard !text.isEmpty, let desk else {
            VoiceTrace.note("session.deliver", "nothing heard")
            if call?.handsFree != true, desk != nil {
                notice = "I didn't catch anything — hold the mic while you talk. Typing still works."
            }
            settle()
            return
        }
        // Said before anything touches the network: he hears within a second
        // that he was heard, however long the desk takes.
        if speaksAloud {
            enqueue(SpokenLine(text: "Asking \(call?.name ?? (displayName.isEmpty ? desk : displayName))…", desk: "agent deck",
                               language: "en"),
                    isAcknowledgement: true)
        }
        state = .sending
        quietTimer?.cancel()
        let open: Call
        if let existing = call {
            open = existing
        } else if let made = await openCall(desk: desk, handsFree: false) {
            open = made
        } else {
            stopSpeaking()
            inTurn = false
            settle()
            return
        }
        do {
            guard let calls else { throw DeckError.transport("no deck") }
            let sent = try await calls.sendVoice(threadID: open.threadID, text: text, callID: open.id)
            VoiceTrace.note("session.sent", "call=\(open.id) chars=\(text.count)")
            seen.insert(sent.id)
            answered = 0
            inTurn = true
            if state == .sending { state = .waiting }
            armWaitTimer()
        } catch {
            VoiceTrace.fail("session.send_failed", "\(error)")
            notice = "That didn't reach \(displayName) — \((error as? DeckError)?.userFacingText ?? "the deck did not answer"). Typing still works."
            stopSpeaking()
            inTurn = false
            settle()
        }
    }

    private func openCall(desk: String, handsFree: Bool) async -> Call? {
        guard let calls else {
            VoiceTrace.fail("call.start_failed", "no call client")
            notice = "This deck connection can't take calls. Typing still works."
            return nil
        }
        starting = true
        defer { starting = false }
        let name = displayName
        do {
            let started = try await calls.startCall(agent: desk)
            // He moved to another desk while a hold-to-talk call was dialling.
            // A phone call is the app's: it carries on wherever he went.
            guard handsFree || self.desk == desk else {
                try? await calls.endCall(id: started.callID)
                return nil
            }
            let voice = started.voice.flatMap { $0.id.isEmpty ? nil : $0 }
            let made = Call(id: started.callID, threadID: started.threadID, desk: desk,
                            startedAt: Date(), handsFree: handsFree, voice: voice,
                            realtime: started.realtime, realtimeError: started.realtimeError, displayName: name)
            VoiceTrace.note("call.started", "call=\(started.callID) realtime=\(started.realtime != nil)")
            call = made
            return made
        } catch {
            VoiceTrace.fail("call.start_failed", "\(error)")
            notice = "Couldn't start a call with \(displayName.isEmpty ? desk : displayName) — \((error as? DeckError)?.userFacingText ?? "the deck did not answer"). Typing still works."
            return nil
        }
    }

    private func enqueue(_ line: SpokenLine, isAcknowledgement: Bool) {
        queue.append((line, isAcknowledgement))
        if current == nil { speakNext() }
    }

    private func speakNext() {
        guard current == nil, !queue.isEmpty else { return }
        let next = queue.removeFirst()
        current = next
        if !next.isAcknowledgement {
            answered += 1
            waitTimer?.cancel()
            state = .speaking
        }
        output.speak(next.line)
    }

    private func lineFinished() {
        current = nil
        if !queue.isEmpty { speakNext(); return }
        if state == .speaking { settle() }
    }

    private func stopSpeaking() {
        queue.removeAll()
        guard current != nil else { return }
        current = nil
        output.stop()
    }

    /// Nothing left to say: wait for more, listen, or rest.
    private func settle() {
        // Still his turn's answer to come: the desk is working, or has not
        // said anything yet.
        if inTurn, call != nil, deskWorking || answered == 0 {
            state = .waiting
            armWaitTimer()
            return
        }
        inTurn = false
        waitTimer?.cancel()
        if call?.handsFree == true, !isMuted {
            state = .listening
        } else {
            state = .idle
            armQuietHangUp()
        }
    }

    private func armWaitTimer() {
        waitTimer?.cancel()
        let limit = waitLimit
        waitTimer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(limit * 1_000_000_000))
            guard !Task.isCancelled, let self, self.state == .waiting else { return }
            self.inTurn = false
            self.settle()
        }
    }

    private func armQuietHangUp() {
        quietTimer?.cancel()
        guard let open = call, !open.handsFree else { return }
        let limit = quietHangUp
        quietTimer = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(limit * 1_000_000_000))
            guard !Task.isCancelled, let self, self.call?.id == open.id, self.state == .idle else { return }
            await self.hangUp()
        }
    }

    /// Another desk, or no desk: a hold-to-talk call is over; a phone call
    /// carries on (`CallPill` shows it from wherever he is).
    private func leaveDesk() {
        wantsHold = false
        if holding { holding = false; input.cancel() }
        notice = nil
        pendingSwitch = nil
        if call?.handsFree == true { return }
        stopSpeaking()
        answered = 0
        inTurn = false
        if call != nil {
            Task { await hangUp() }
        } else {
            state = .idle
        }
    }
}

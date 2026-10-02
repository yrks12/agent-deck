import Foundation
import Combine

/// Which device a call's audio runs on. The Mac has no audio session to
/// configure; the phone must ask the OS for one and give it back after.
public enum CallPlatform: Sendable {
    case mac, phone

    public static var current: CallPlatform {
        #if os(iOS)
        return .phone
        #else
        return .mac
        #endif
    }
}

/// **The phone's audio session for a call, decided here so it is tested.**
///
/// `.playAndRecord` + `.voiceChat`: the mode turns on the phone's own echo
/// canceller, so the agent's voice out of the speaker is not heard as him —
/// the same job voice processing does on the Mac, under the same barge gate.
/// Bluetooth (HFP) so AirPods carry the call, mic and all. Speaker or
/// earpiece: with `.defaultToSpeaker` set the earpiece cannot be reached, so
/// the earpiece plan drops it.
public struct CallAudioSessionPlan: Equatable, Sendable {
    public enum Category: String, Sendable { case playAndRecord }
    public enum Mode: String, Sendable { case voiceChat }
    public enum Option: String, Sendable { case defaultToSpeaker, allowBluetooth }
    public enum Output: String, Sendable { case speaker, receiver }

    public var category: Category = .playAndRecord
    public var mode: Mode = .voiceChat
    public var options: Set<Option>
    public var output: Output

    /// Nil on the Mac: its call audio is exactly what it was.
    public static func forCall(on platform: CallPlatform, speaker: Bool) -> CallAudioSessionPlan? {
        guard platform == .phone else { return nil }
        return CallAudioSessionPlan(options: speaker ? [.defaultToSpeaker, .allowBluetooth] : [.allowBluetooth],
                                    output: speaker ? .speaker : .receiver)
    }
}

/// The OS audio session, behind a protocol so the call's rules are tested
/// with a fake. `PhoneAudioSession` is the iPhone's.
@MainActor
public protocol CallAudioSessionControl: AnyObject {
    /// Configure and activate. Called again to change speaker/earpiece.
    func activate(_ plan: CallAudioSessionPlan) throws
    /// Give the audio back to other apps.
    func deactivate()
}

/// What the OS did to the call's audio.
public enum CallInterruption: Equatable, Sendable {
    /// A phone call rang, Siri started, an alarm went off.
    case began
    case ended(shouldResume: Bool)
    /// The OS restarted its audio services: every engine is dead.
    case mediaServicesReset
    /// Headphones or Bluetooth went away: audio falls back to the phone.
    case routeLost
}

/// **What an interruption does to the call.** A phone call or Siri puts it on
/// hold and it carries on after; a hold long enough for the voice's pass to
/// go stale, another app keeping the audio, or the audio services restarting
/// ends it — cleanly, with a sentence, never a silent dead line.
public struct CallInterruptionPolicy: Sendable {
    public enum Action: Equatable, Sendable {
        case hold, resume, keep
        case hangUp(String)
    }

    /// A hold longer than this ends the call when it comes back.
    public static let maxHold: TimeInterval = 120

    private var heldSince: Date?

    public init() {}

    public mutating func handle(_ event: CallInterruption, at now: Date, onCall: Bool) -> Action {
        guard onCall else { heldSince = nil; return .keep }
        switch event {
        case .began:
            if heldSince == nil { heldSince = now }
            return .hold
        case .ended(let shouldResume):
            guard let since = heldSince else { return .keep }
            heldSince = nil
            guard shouldResume else { return .hangUp("Another app kept the phone's audio, so the call ended.") }
            if now.timeIntervalSince(since) > Self.maxHold {
                return .hangUp("The call was on hold too long and ended.")
            }
            return .resume
        case .mediaServicesReset:
            heldSince = nil
            return .hangUp("The phone's audio restarted, so the call ended.")
        case .routeLost:
            return .keep
        }
    }
}

/// One call at a time: what tapping Call on a desk does.
public enum PhoneCallDecision: Equatable, Sendable {
    case dial
    /// He is already on a call with this desk: bring the call screen up.
    case showCall
    /// "End call with Atlas and call Acme?"
    case askFirst(VoiceSession.CallSwitch)

    public static func decide(current: VoiceSession.Call?, desk: String, name: String) -> PhoneCallDecision {
        guard let current else { return .dial }
        if current.desk == desk { return .showCall }
        return .askFirst(VoiceSession.CallSwitch(from: current.name, to: name.isEmpty ? desk : name))
    }
}

extension CallPill {
    /// On the phone the call screen is full-screen; whenever he minimises it
    /// — whatever screen he is on — the pill carries the call.
    @MainActor
    public static func phone(_ session: VoiceSession, callScreenShown: Bool) -> CallPill? {
        guard !callScreenShown, let call = session.call, call.handsFree else { return nil }
        return CallPill(desk: call.desk, name: call.name, startedAt: call.startedAt,
                        isMuted: session.isMuted, isLive: session.realtime != nil)
    }
}

/// **The phone's call, owned by the app.** "the call follows you across
/// screens": on the Mac the thread on screen feeds the voice session; on the
/// phone the call has its own feed of the called desk's thread, so no screen
/// — roster, another desk, the lock screen — can starve the voice of the
/// desk's progress or end the call by navigating.
///
/// Adds only what a phone needs around the shared `VoiceSession`: the OS
/// audio session, interruptions, speaker/earpiece, full screen vs pill, and
/// the one-call question. The call itself is the Mac's, unchanged.
@MainActor
public final class PhoneCallCenter: ObservableObject {
    public let session: VoiceSession
    /// Full-screen call view up (else the pill shows).
    @Published public var isCallScreenShown = false
    @Published public private(set) var pendingSwitch: VoiceSession.CallSwitch?
    /// A phone call or Siri has the audio.
    @Published public private(set) var isOnHold = false
    @Published public private(set) var speakerOn: Bool
    /// One sentence when the phone ended or could not start the call.
    @Published public var notice: String?

    /// Restarts the call's audio engine after an interruption (the app wires
    /// it to `EngineCallAudio.resumeAfterInterruption()`).
    public var resumeAudio: (() -> Void)?

    public var pill: CallPill? { CallPill.phone(session, callScreenShown: isCallScreenShown) }

    private struct Target { var desk: String; var name: String; var voice: DeskVoice?; var ringID: String? = nil }

    private let audioSession: CallAudioSessionControl?
    private let working: (String) -> Bool
    /// The desk's thread, every time it changes; the first value is history.
    private let feed: (String) -> AsyncStream<[Message]>
    private var feedTask: Task<Void, Never>?
    private var pendingTarget: Target?
    private var policy = CallInterruptionPolicy()
    private var watch: AnyCancellable?
    private var active = false
    /// Bumped per dial: a stale "call gone" never tears down the next call.
    private var generation = 0

    public init(session: VoiceSession, audioSession: CallAudioSessionControl?, speakerOn: Bool = true,
                working: @escaping (String) -> Bool, feed: @escaping (String) -> AsyncStream<[Message]>) {
        self.session = session
        self.audioSession = audioSession
        self.speakerOn = speakerOn
        self.working = working
        self.feed = feed
        watch = session.$call.sink { [weak self] call in
            // @Published fires before the value lands: read the emitted one.
            guard call == nil else { return }
            MainActor.assumeIsolated {
                guard let self else { return }
                let generation = self.generation
                Task { @MainActor [weak self] in self?.callGone(generation) }
            }
        }
    }

    private var plan: CallAudioSessionPlan? { CallAudioSessionPlan.forCall(on: .phone, speaker: speakerOn) }

    // MARK: - Calling

    /// Call on a desk (thread header, roster swipe).
    public func call(desk: String, name: String, voice: DeskVoice?) async {
        let target = Target(desk: desk, name: name.isEmpty ? desk : name, voice: voice)
        switch PhoneCallDecision.decide(current: session.call, desk: desk, name: target.name) {
        case .showCall:
            isCallScreenShown = true
        case .askFirst(let question):
            pendingTarget = target
            pendingSwitch = question
        case .dial:
            await dial(target)
        }
    }

    /// A desk called him and he tapped Answer: the same call screen as a
    /// call he placed, opened by answering the ring. Tapping Answer is his
    /// choice, so a call he is on ends first without asking.
    public func answer(ring: IncomingRing, name: String, voice: DeskVoice?) async {
        cancelSwitch()
        if session.call != nil { await hangUp() }
        await dial(Target(desk: ring.agent, name: name.isEmpty ? ring.agent : name, voice: voice, ringID: ring.id))
    }

    public func confirmSwitch() async {
        guard let target = pendingTarget else { return }
        pendingSwitch = nil
        pendingTarget = nil
        await session.hangUp()
        teardown()
        await dial(target)
    }

    public func cancelSwitch() {
        pendingSwitch = nil
        pendingTarget = nil
    }

    public func hangUp() async {
        await session.hangUp()
        teardown()
    }

    public func toggleMute() { session.toggleMute() }

    public func toggleSpeaker() {
        speakerOn.toggle()
        guard active, let plan else { return }
        do { try audioSession?.activate(plan) } catch {
            VoiceTrace.fail("call.phone.route_failed", "\(error)")
        }
    }

    private func dial(_ target: Target) async {
        notice = nil
        if let plan {
            do { try audioSession?.activate(plan) } catch {
                VoiceTrace.fail("call.phone.session_failed", "\(error)")
                notice = "The phone's audio wouldn't start for the call."
                return
            }
        }
        active = true
        generation += 1
        policy = CallInterruptionPolicy()
        let stream = feed("direct:\(target.desk)")
        var updates = stream.makeAsyncIterator()
        // History first, so opening the call never reads yesterday aloud.
        let history = await updates.next() ?? []
        session.sync(desk: target.desk, displayName: target.name, voice: target.voice,
                     working: working(target.desk), messages: history)
        isCallScreenShown = true
        if let ring = target.ringID { await session.answerCall(ringID: ring) } else { await session.startCall() }
        guard session.call?.desk == target.desk else {
            if session.call == nil {
                // Could not start: why outlives the call screen.
                if notice == nil, let why = session.notice { notice = why }
                teardown()
            }
            return
        }
        feedTask = Task { [weak self] in
            while let messages = await updates.next() {
                guard let self, !Task.isCancelled, self.session.call?.desk == target.desk else { return }
                self.session.sync(desk: target.desk, displayName: target.name, voice: target.voice,
                                  working: self.working(target.desk), messages: messages)
            }
        }
    }

    private func callGone(_ generation: Int) {
        guard generation == self.generation, session.call == nil, active else { return }
        // Dropped on its own: why outlives the call screen.
        if notice == nil, let why = session.notice { notice = why }
        teardown()
    }

    private func teardown() {
        guard active else {
            isCallScreenShown = false
            return
        }
        active = false
        feedTask?.cancel()
        feedTask = nil
        isOnHold = false
        isCallScreenShown = false
        session.sync(desk: nil, displayName: "", voice: nil, working: false, messages: [])
        audioSession?.deactivate()
    }

    // MARK: - Interruptions

    public func handle(_ event: CallInterruption, at now: Date = Date()) async {
        switch policy.handle(event, at: now, onCall: session.call != nil) {
        case .keep:
            break
        case .hold:
            VoiceTrace.note("call.phone.hold")
            isOnHold = true
        case .resume:
            VoiceTrace.note("call.phone.resume")
            isOnHold = false
            if let plan { try? audioSession?.activate(plan) }
            resumeAudio?()
        case .hangUp(let why):
            VoiceTrace.note("call.phone.interrupted_end", why)
            notice = why
            await hangUp()
        }
    }
}

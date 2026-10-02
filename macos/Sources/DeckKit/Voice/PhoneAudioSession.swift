#if os(iOS)
import Foundation
import AVFoundation

/// **The iPhone's audio session for a call.** Applies a `CallAudioSessionPlan`
/// to `AVAudioSession`, gives the audio back when the call ends, and turns the
/// OS's interruption / route / reset notifications into `CallInterruption`s for
/// `PhoneCallCenter`. iOS only: the Mac has no audio session.
@MainActor
public final class PhoneAudioSession: CallAudioSessionControl {
    private var observers: [NSObjectProtocol] = []

    public init() {}

    public func activate(_ plan: CallAudioSessionPlan) throws {
        // A test process never takes the phone's audio.
        guard !AudioGate.isSilenced else { return }
        let session = AVAudioSession.sharedInstance()
        var options: AVAudioSession.CategoryOptions = []
        if plan.options.contains(.defaultToSpeaker) { options.insert(.defaultToSpeaker) }
        if plan.options.contains(.allowBluetooth) { options.insert(.allowBluetooth) }
        try session.setCategory(.playAndRecord, mode: .voiceChat, options: options)
        try session.setActive(true)
        try session.overrideOutputAudioPort(plan.output == .speaker ? .speaker : .none)
        VoiceTrace.note("call.phone.session", "output=\(plan.output.rawValue) route=\(Self.routeName)")
    }

    public func deactivate() {
        guard !AudioGate.isSilenced else { return }
        do {
            try AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        } catch {
            VoiceTrace.fail("call.phone.deactivate_failed", "\(error)")
        }
    }

    /// Where the call is coming out right now, for the trace.
    public static var routeName: String {
        AVAudioSession.sharedInstance().currentRoute.outputs.first?.portType.rawValue ?? "none"
    }

    /// Hands every interruption to `handler` until `stopObserving()`.
    public func observe(_ handler: @escaping @MainActor (CallInterruption) -> Void) {
        stopObserving()
        let center = NotificationCenter.default
        let session = AVAudioSession.sharedInstance()
        let names: [Notification.Name] = [AVAudioSession.interruptionNotification,
                                          AVAudioSession.routeChangeNotification,
                                          AVAudioSession.mediaServicesWereResetNotification]
        observers = names.map { name in
            center.addObserver(forName: name, object: session, queue: .main) { note in
                guard let event = Self.interruption(name: note.name, userInfo: note.userInfo) else { return }
                MainActor.assumeIsolated { handler(event) }
            }
        }
    }

    public func stopObserving() {
        observers.forEach { NotificationCenter.default.removeObserver($0) }
        observers = []
    }

    /// The OS's notification, as the call understands it. Nil: not news.
    nonisolated public static func interruption(name: Notification.Name,
                                                userInfo: [AnyHashable: Any]?) -> CallInterruption? {
        switch name {
        case AVAudioSession.mediaServicesWereResetNotification:
            return .mediaServicesReset
        case AVAudioSession.routeChangeNotification:
            guard let raw = userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt,
                  AVAudioSession.RouteChangeReason(rawValue: raw) == .oldDeviceUnavailable else { return nil }
            return .routeLost
        case AVAudioSession.interruptionNotification:
            guard let raw = userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
                  let type = AVAudioSession.InterruptionType(rawValue: raw) else { return nil }
            switch type {
            case .began:
                return .began
            case .ended:
                let flags = (userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt).map {
                    AVAudioSession.InterruptionOptions(rawValue: $0)
                } ?? []
                return .ended(shouldResume: flags.contains(.shouldResume))
            @unknown default:
                return nil
            }
        default:
            return nil
        }
    }
}
#endif

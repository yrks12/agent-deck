import Foundation

/// **"You're on a call with Atlas", from anywhere in the app.**
///
/// "when i move to other agent the call gets disconnected": a phone call now
/// belongs to the app. While he reads another desk (or no desk), a compact
/// pill carries the call — the desk's character, the timer, mute, hang up,
/// and a click back to that desk's thread. The big call screen is drawn only
/// in the called desk's own thread. Decided here so it is tested.
public struct CallPill: Equatable, Sendable {
    public var desk: String
    public var name: String
    public var startedAt: Date
    public var isMuted: Bool
    /// The deck's live voice (C3), or this Mac's speech.
    public var isLive: Bool

    /// The pill, or nil when there is no phone call or he is already looking
    /// at the called desk (the call bar is there).
    @MainActor
    public static func make(_ session: VoiceSession) -> CallPill? {
        guard let call = session.call, call.handsFree, call.desk != session.desk else { return nil }
        return CallPill(desk: call.desk, name: call.name, startedAt: call.startedAt,
                        isMuted: session.isMuted, isLive: session.realtime != nil)
    }

    /// Whether the thread on screen draws the call bar: only the called desk's.
    @MainActor
    public static func showsBar(_ session: VoiceSession) -> Bool {
        guard let call = session.call else { return false }
        return call.desk == session.desk
    }
}

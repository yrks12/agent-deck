import Foundation

/// **What the character on the call screen is doing, decided here so it is
/// tested.** "i dont have animations when im talking": the face listens to
/// him (his mic level), talks with the agent's voice (its output level), and
/// shows when the desk is off doing something he asked for.
///
/// `level` is the only thing that moves the picture, and it only changes when
/// audio does — a silent call is a still picture that costs nothing.
public struct CallStage: Equatable, Sendable {
    public enum Mode: String, Sendable {
        case connecting, listening, hearing, thinking, speaking, working, muted
    }

    public var mode: Mode
    /// 0...1: his voice while listening/hearing, the agent's while speaking.
    public var level: Float
    public var status: String
    /// Live voice (C3) or this Mac's speech.
    public var isLive: Bool

    public init(mode: Mode, level: Float, status: String, isLive: Bool) {
        self.mode = mode
        self.level = level
        self.status = status
        self.isLive = isLive
    }

    @MainActor
    public static func make(_ session: VoiceSession) -> CallStage {
        let name = session.displayName.isEmpty ? (session.desk ?? "the agent") : session.displayName
        if let call = session.realtime {
            return live(call, name: name)
        }
        if session.isMuted {
            return CallStage(mode: .muted, level: 0, status: "Muted — \(name) can still talk", isLive: false)
        }
        switch session.state {
        case .speaking:
            return CallStage(mode: .speaking, level: session.speakingLevel, status: "\(name) is speaking", isLive: false)
        case .sending, .waiting:
            return CallStage(mode: .working, level: 0, status: "\(name) is on it…", isLive: false)
        case .listening, .idle:
            let hearing = !session.heard.isEmpty
            return CallStage(mode: hearing ? .hearing : .listening, level: session.micLevel,
                             status: hearing ? "Hearing you…" : "Listening…", isLive: false)
        }
    }

    @MainActor
    static func live(_ call: RealtimeCallSession, name: String) -> CallStage {
        if call.isMuted {
            return CallStage(mode: .muted, level: 0, status: "Muted — \(name) can still talk", isLive: true)
        }
        switch call.phase {
        case .connecting:
            return CallStage(mode: .connecting, level: 0, status: "Calling \(name)…", isLive: true)
        case .speaking:
            let status = call.isWorking ? "\(name) is speaking — the desk is still working" : "\(name) is speaking"
            return CallStage(mode: .speaking, level: call.agentLevel, status: status, isLive: true)
        case .hearing:
            return CallStage(mode: .hearing, level: call.micLevel, status: "Hearing you…", isLive: true)
        case .thinking:
            return CallStage(mode: call.isWorking ? .working : .thinking, level: 0,
                             status: call.isWorking ? "Checking with the desk…" : "\(name) is thinking…",
                             isLive: true)
        case .listening:
            if call.isWorking {
                return CallStage(mode: .working, level: call.micLevel, status: "The desk is on it — keep talking",
                                 isLive: true)
            }
            return CallStage(mode: .listening, level: call.micLevel, status: "Listening…", isLive: true)
        case .ended:
            return CallStage(mode: .listening, level: 0, status: "Call ended", isLive: true)
        }
    }
}

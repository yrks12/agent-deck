import SwiftUI
import DeckKit

/// **The phone call's screen: the desk's character, large, alive.**
///
/// Listening, the ring breathes with *his* voice; speaking, the character
/// grows and its mouth opens with the *agent's* voice; while the desk is off
/// doing what he asked, it wears an orange "on it" ring. Everything that moves
/// is driven by a level that only changes when audio does (`LevelGate`), so a
/// silent call is a still picture: no `repeatForever`, no implicit
/// `.animation`, no display-rate timeline — the permanent-animation trap this
/// app has already paid a day for.
public struct CallStageView: View {
    @ObservedObject var session: VoiceSession
    let agent: Agent?

    public init(session: VoiceSession, agent: Agent?) {
        self.session = session
        self.agent = agent
    }

    public var body: some View {
        if let live = session.realtime {
            LiveCallStage(session: session, live: live, agent: agent)
        } else {
            CallStageFace(stage: CallStage.make(session), agent: agent, desk: session.desk ?? "", caption: "")
        }
    }
}

/// The live call publishes its own levels; observing it here keeps those
/// redraws inside the stage.
private struct LiveCallStage: View {
    @ObservedObject var session: VoiceSession
    @ObservedObject var live: RealtimeCallSession
    let agent: Agent?

    var body: some View {
        CallStageFace(stage: CallStage.make(session), agent: agent, desk: live.desk, caption: live.caption)
    }
}

/// The picture, from a decided `CallStage`. Pure: the captures draw it straight.
struct CallStageFace: View {
    let stage: CallStage
    let agent: Agent?
    let desk: String
    let caption: String
    var size: CGFloat = 116

    private var level: CGFloat { CGFloat(max(0, min(1, stage.level))) }

    var body: some View {
        VStack(spacing: 10) {
            ZStack {
                ring
                face
                    .scaleEffect(stage.mode == .speaking ? 1 + 0.07 * level : 1)
                mouth
            }
            .frame(width: size * 1.7, height: size * 1.5)
            Text(stage.status)
                .font(.callout.weight(.medium))
                .foregroundStyle(stage.mode == .working ? Color.orange : Color.secondary)
                .lineLimit(1)
            if !caption.isEmpty, stage.mode == .speaking {
                // Whole sentences, already trimmed to fit (`CallCaption`).
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .multilineTextAlignment(.center)
                    .frame(maxWidth: 360)
            }
        }
        .padding(.vertical, 10)
        .frame(maxWidth: .infinity)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("On a call with \(agent?.name ?? desk). \(stage.status)")
    }

    @ViewBuilder
    private var face: some View {
        if let agent {
            AvatarView(agent: agent, size: size)
        } else {
            AvatarView(look: AvatarLook.forName(desk), attention: .quiet, isDimmed: false,
                       localAvatarURL: nil, size: size)
        }
    }

    private var tint: Color {
        switch stage.mode {
        case .speaking: return .green
        case .working: return .orange
        case .muted: return .gray
        case .connecting, .thinking: return .secondary
        case .listening, .hearing: return .accentColor
        }
    }

    /// Two rings: an inner one that is always there (the call is up) and an
    /// outer one whose reach is the level of whoever is talking.
    private var ring: some View {
        ZStack {
            Circle()
                .stroke(tint.opacity(stage.mode == .muted ? 0.25 : 0.45),
                        style: StrokeStyle(lineWidth: 3, dash: stage.mode == .working ? [6, 5] : []))
                .frame(width: size * 1.18, height: size * 1.18)
            Circle()
                .fill(tint.opacity(0.10 + 0.22 * level))
                .frame(width: size * (1.18 + 0.42 * level), height: size * (1.18 + 0.42 * level))
            Circle()
                .stroke(tint.opacity(0.15 + 0.55 * level), lineWidth: 2 + 3 * level)
                .frame(width: size * (1.22 + 0.42 * level), height: size * (1.22 + 0.42 * level))
        }
    }

    /// The mouth only exists while the agent talks; it opens with its voice.
    @ViewBuilder
    private var mouth: some View {
        if stage.mode == .speaking {
            Capsule()
                .fill(Color.black.opacity(0.55))
                .frame(width: size * (0.16 + 0.08 * level), height: max(3, size * 0.16 * level))
                .offset(y: size * 0.24)
        }
    }
}

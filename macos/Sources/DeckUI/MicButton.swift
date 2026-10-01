import SwiftUI
import DeckKit

/// **Hold to talk.** The composer's accessory slot: press and hold, say it,
/// let go. Letting go sends what was heard on the call's voice channel and
/// "Asking <Name>…" is said at once; the desk's answer is spoken. On a
/// hands-free call the same button is mute.
///
/// **Only his voice moves it.** While it listens, a ring around it reaches as
/// far as he is loud — driven by `micLevel`, which stops updating when he is
/// silent, so at rest this is a still glyph. No timeline, no implicit
/// animation: an animating indicator in this pane has already cost the app a
/// core (see `ThreadView`'s status strip).
public struct MicButton: View {
    @ObservedObject var session: VoiceSession
    @State private var pressed = false

    public init(session: VoiceSession) {
        self.session = session
    }

    public var body: some View {
        Image(systemName: glyph)
            .font(.system(size: 14, weight: .medium))
            .foregroundStyle(isHot ? Color.white : (session.notice != nil ? Color.secondary : Color.primary))
            .frame(width: 30, height: 30)
            .background(isHot ? AnyShapeStyle(Color.red) : AnyShapeStyle(.quaternary), in: Circle())
            .background { levelRing }
            .contentShape(Circle())
            .gesture(
                DragGesture(minimumDistance: 0)
                    .onChanged { _ in
                        guard !pressed else { return }
                        pressed = true
                        Task { await session.pressMic() }
                    }
                    .onEnded { _ in
                        pressed = false
                        Task { await session.releaseMic() }
                    })
            .help(help)
            .accessibilityElement()
            .accessibilityAddTraits(.isButton)
            .accessibilityLabel(help)
            // VoiceOver cannot hold: one activation starts, the next sends.
            .accessibilityAction {
                Task {
                    if session.state == .listening, session.call?.handsFree != true {
                        await session.releaseMic()
                    } else {
                        await session.pressMic()
                    }
                }
            }
    }

    private var name: String { session.displayName.isEmpty ? "the agent" : session.displayName }

    /// How loud he is, as a ring around the button.
    @ViewBuilder
    private var levelRing: some View {
        if isHot {
            let level = CGFloat(max(0, min(1, session.micLevel)))
            Circle()
                .fill(Color.red.opacity(0.18 + 0.25 * level))
                .frame(width: 30 + 16 * level, height: 30 + 16 * level)
                .overlay(Circle().stroke(Color.red.opacity(0.35 + 0.5 * level), lineWidth: 1.5)
                    .frame(width: 30 + 16 * level, height: 30 + 16 * level))
        }
    }

    private var isHot: Bool {
        session.state == .listening && !session.isMuted
    }

    private var glyph: String {
        if session.isMuted { return "mic.slash.fill" }
        switch session.state {
        case .listening: return "mic.fill"
        case .sending, .waiting: return "ellipsis"
        case .speaking: return "speaker.wave.2.fill"
        case .idle: return session.notice != nil ? "mic.slash" : "mic"
        }
    }

    private var help: String {
        if session.call?.handsFree == true {
            return session.isMuted ? "Unmute" : "Mute"
        }
        switch session.state {
        case .speaking: return "Hold to talk — \(name) stops"
        default: return "Hold to talk to \(name)"
        }
    }
}

/// **The line above the composer that belongs to the mic.** What he is saying
/// while he says it, or — when he cannot be heard — the one plain sentence
/// that says why. Typing is untouched either way.
public struct VoiceComposerLine: View {
    @ObservedObject var session: VoiceSession

    public init(session: VoiceSession) {
        self.session = session
    }

    public var body: some View {
        if let notice = session.notice {
            line(notice, systemImage: "mic.slash")
        } else if session.state == .listening, !session.heard.isEmpty {
            line("“\(session.heard)”", systemImage: "waveform")
        }
    }

    private func line(_ text: String, systemImage: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: systemImage)
                .accessibilityHidden(true)
            Text(text)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 22)
        .padding(.top, 6)
        .accessibilityElement(children: .combine)
    }
}

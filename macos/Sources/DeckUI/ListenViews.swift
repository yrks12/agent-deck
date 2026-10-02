import SwiftUI
import DeckKit

/// **Listen to a desk's message — the control on its bubble and the strip that
/// plays it.** Pure SwiftUI over `ListenPlayer`, compiled into DeckUI for the
/// Mac and straight into the iPhone target (see `ios/project.yml`), so both
/// apps draw one control. Colours are the shared palette; no stock accent.

/// The small speaker on an agent bubble. Idle: "Listen". This message
/// loading: "Loading…" (tap to cancel). Playing: pause. Paused: play.
struct ListenButton: View {
    @ObservedObject var player: ListenPlayer
    let message: Message
    /// Icon only: the phone's bubble.
    var compact = false

    private var mine: Bool { player.state.messageID == message.id }

    var body: some View {
        Button {
            DeckHaptics.tap()
            player.toggle(message)
        } label: {
            HStack(spacing: 4) {
                Image(systemName: symbol)
                    .font(.system(size: 11, weight: .semibold))
                    .frame(width: 14)
                if !compact { Text(title).font(.caption) }
            }
            .foregroundStyle(.secondary)
            .padding(.horizontal, compact ? 6 : 8)
            .padding(.vertical, 4)
            .background(DeckPalette.field, in: Capsule())
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibility)
    }

    private var symbol: String {
        guard mine else { return "speaker.wave.2" }
        switch player.state {
        case .playing: return "pause.fill"
        case .paused: return "play.fill"
        default: return "speaker.wave.2"
        }
    }

    private var title: String {
        guard mine else { return "Listen" }
        switch player.state {
        case .loading: return "Loading…"
        case .playing: return "Pause"
        case .paused: return "Resume"
        case .idle: return "Listen"
        }
    }

    private var accessibility: String {
        guard mine else { return "Listen to this message" }
        switch player.state {
        case .loading: return "Loading audio. Double-tap to cancel"
        case .playing: return "Pause listening"
        case .paused: return "Resume listening"
        case .idle: return "Listen to this message"
        }
    }
}

#if os(macOS)
/// **Mac: the Listen control appears when the pointer is on an agent's
/// bubble** (and stays while that message is playing), plus a right-click
/// entry. Hung on the bubble's top corner so nothing in the transcript moves.
struct ListenOnBubble: ViewModifier {
    @ObservedObject var player: ListenPlayer
    let message: Message
    @State private var hovering = false

    func body(content: Content) -> some View {
        if ListenPlayer.canListen(message) {
            content
                .onHover { hovering = $0 }
                .overlay(alignment: .topTrailing) {
                    if hovering || player.state.messageID == message.id {
                        ListenButton(player: player, message: message)
                            .offset(x: -8, y: -10)
                    }
                }
                .contextMenu { ListenMenuItem(player: player, message: message) }
        } else {
            content
        }
    }
}
#endif

/// The context-menu entry (Mac right-click, phone long-press).
struct ListenMenuItem: View {
    @ObservedObject var player: ListenPlayer
    let message: Message

    var body: some View {
        if ListenPlayer.canListen(message) {
            Button(player.state.messageID == message.id ? "Stop listening" : "Listen") {
                if player.state.messageID == message.id { player.stop() } else { player.toggle(message) }
            }
        }
    }
}

/// **The strip, while a message plays:** play / pause, a scrubber, the time,
/// the speed (tap to cycle 1× → 1.25× → 1.5× → 1.75× → 2× → 0.75×), "next"
/// (keep going with the following agent messages) and close. Draws nothing
/// when nothing is playing.
struct ListenStrip: View {
    @ObservedObject var player: ListenPlayer

    var body: some View {
        if player.isActive {
            VStack(spacing: 4) {
                HStack(spacing: 10) {
                    Button { player.pauseOrResume() } label: {
                        Image(systemName: player.state.isPaused ? "play.fill" : "pause.fill")
                            .font(.system(size: 13, weight: .bold))
                            .frame(width: 28, height: 28)
                            .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .disabled(player.isFallback || player.state.isLoading)
                    .accessibilityLabel(player.state.isPaused ? "Play" : "Pause")

                    if player.isFallback || player.state.isLoading {
                        Text(player.state.isLoading ? "Getting the voice ready…" : "Reading aloud")
                            .font(.caption).foregroundStyle(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    } else {
                        ListenProgressRow(clock: player.clock) { player.seek(toFraction: $0) }
                    }

                    Button { DeckHaptics.tap(); player.cycleSpeed() } label: {
                        Text(ListenSpeed.label(player.speed))
                            .font(.caption.weight(.semibold).monospacedDigit())
                            .frame(minWidth: 40)
                            .padding(.vertical, 5)
                            .background(DeckPalette.field, in: Capsule())
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .disabled(player.isFallback)
                    .accessibilityLabel("Speed")
                    .accessibilityValue(ListenSpeed.label(player.speed))
                    .accessibilityHint("Double-tap to change the speed")

                    Button { player.setPlayNext(!player.playNext) } label: {
                        Image(systemName: "forward.end.fill")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(player.playNext ? Color.primary : Color.secondary.opacity(0.6))
                            .frame(width: 28, height: 28)
                            .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Play next messages")
                    .accessibilityValue(player.playNext ? "On" : "Off")

                    Button { player.stop() } label: {
                        Image(systemName: "xmark")
                            .font(.system(size: 12, weight: .semibold))
                            .foregroundStyle(.secondary)
                            .frame(width: 28, height: 28)
                            .contentShape(Circle())
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Stop listening")
                }
                if let notice = player.notice {
                    Text(notice).font(.caption).foregroundStyle(.secondary)
                }
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .card(radius: 14)
            .padding(.horizontal, 12)
            .padding(.vertical, 4)
        } else if let notice = player.notice {
            HStack {
                Text(notice).font(.caption).foregroundStyle(.secondary)
                Spacer(minLength: 4)
                Button { player.dismissNotice() } label: { Image(systemName: "xmark") }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Dismiss")
            }
            .padding(.horizontal, 20)
            .padding(.vertical, 4)
        }
    }

    static func clock(_ seconds: TimeInterval) -> String {
        let whole = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", whole / 60, whole % 60)
    }
}

/// The scrubber and the time: the only thing that observes the clock.
private struct ListenProgressRow: View {
    @ObservedObject var clock: ListenClock
    let seek: (Double) -> Void

    var body: some View {
        ListenScrubber(progress: clock.progress, seek: seek)
        Text("\(ListenStrip.clock(clock.elapsed)) / \(ListenStrip.clock(clock.duration))")
            .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
            .accessibilityHidden(true)
    }
}

private extension ListenPlayer.State {
    var isPaused: Bool { if case .paused = self { return true } else { return false } }
    var isLoading: Bool { if case .loading = self { return true } else { return false } }
}

/// A scrubber drawn from the palette (a stock `Slider` paints the accent).
struct ListenScrubber: View {
    let progress: Double
    let seek: (Double) -> Void

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(DeckPalette.field)
                Capsule().fill(DeckPalette.ink)
                    .frame(width: max(4, geo.size.width * progress))
                Circle().fill(DeckPalette.ink)
                    .frame(width: 12, height: 12)
                    .offset(x: max(0, min(geo.size.width - 12, geo.size.width * progress - 6)))
            }
            .frame(height: 4)
            .frame(maxHeight: .infinity)
            .contentShape(Rectangle())
            .gesture(DragGesture(minimumDistance: 0).onChanged { drag in
                seek(max(0, min(1, drag.location.x / max(1, geo.size.width))))
            })
        }
        .frame(height: 24)
        .accessibilityElement()
        .accessibilityLabel("Position")
        .accessibilityValue("\(Int(progress * 100)) percent")
        .accessibilityAdjustableAction { direction in
            switch direction {
            case .increment: seek(min(1, progress + 0.1))
            case .decrement: seek(max(0, progress - 0.1))
            @unknown default: break
            }
        }
    }
}

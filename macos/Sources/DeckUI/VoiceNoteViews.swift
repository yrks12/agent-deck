import SwiftUI
import DeckKit

/// **"Spoken replies" in the conversation header.** One tap: on, an answer to
/// something he said out loud is read in the desk's call voice; off, it stays
/// text. Remembered on this Mac (`SpokenReplies.key`).
struct SpokenRepliesToggle: View {
    @AppStorage(SpokenReplies.key) private var spoken = true
    let name: String

    var body: some View {
        Button { spoken.toggle() } label: {
            Image(systemName: spoken ? "speaker.wave.2.fill" : "speaker.slash")
                .font(.system(size: 13, weight: .medium))
                .foregroundStyle(spoken ? Color.accentColor : Color.secondary)
                .frame(width: 26, height: 26)
        }
        .buttonStyle(.plain)
        .help(spoken
              ? "Spoken replies are on — when you talk to \(name), the answer is read aloud in its call voice. Click to keep answers as text."
              : "Spoken replies are off — answers stay text, even when you talk. Click to hear them.")
        .accessibilityLabel("Spoken replies")
        .accessibilityValue(spoken ? "On" : "Off")
        .accessibilityAddTraits(.isToggle)
    }
}

/// **Record a voice message** — the composer's second voice button. Not a
/// call and not hold-to-talk: tap, speak, tap send. Hidden while recording;
/// `VoiceNoteRecordingBar` takes its place.
struct VoiceNoteButton: View {
    @ObservedObject var composer: VoiceNoteComposer
    let threadID: String?
    let name: String

    var body: some View {
        Button {
            guard let threadID else { return }
            Task { await composer.start(threadID: threadID) }
        } label: {
            Image(systemName: composer.state == .sending ? "ellipsis" : "waveform")
                .font(.system(size: 14, weight: .medium))
                .frame(width: 30, height: 30)
                .background(.quaternary, in: Circle())
        }
        .buttonStyle(.plain)
        .disabled(threadID == nil || composer.state != .idle)
        .help("Record a voice message to \(name) — no call")
        .accessibilityLabel("Record a voice message")
    }
}

/// While he records: throw it away · his voice as a waveform · how long · send.
struct VoiceNoteRecordingBar: View {
    @ObservedObject var composer: VoiceNoteComposer

    var body: some View {
        HStack(spacing: 10) {
            Button { composer.cancel() } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 12, weight: .semibold))
                    .frame(width: 28, height: 28)
                    .background(.quaternary, in: Circle())
            }
            .buttonStyle(.plain)
            .help("Throw this recording away")
            .accessibilityLabel("Cancel recording")

            Circle().fill(Color.red).frame(width: 8, height: 8).accessibilityHidden(true)
            LiveWaveform(levels: composer.levels)
                .frame(maxWidth: .infinity, minHeight: 26, maxHeight: 26)
                .accessibilityHidden(true)
            if let started = composer.startedAt {
                Text(timerInterval: started...Date.distantFuture, countsDown: false)
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Button { Task { await composer.send() } } label: {
                Image(systemName: "arrow.up.circle.fill").font(.system(size: 26))
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.return, modifiers: [])
            .help("Send the voice message")
            .accessibilityLabel("Send voice message")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 7)
        .background(ThreadColors.composer, in: RoundedRectangle(cornerRadius: 20, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 20, style: .continuous).strokeBorder(Color.red.opacity(0.4)))
        .padding(.horizontal, 14)
        .padding(.top, 6)
        .padding(.bottom, 12)
    }
}

/// Bars from his loudness, newest on the right. Drawn only when a level
/// arrives — no timeline, no implicit animation.
struct LiveWaveform: View {
    let levels: [Float]

    var body: some View {
        Canvas { context, size in
            let bar: CGFloat = 3, gap: CGFloat = 2
            let fits = max(1, Int(size.width / (bar + gap)))
            let shown = Array(levels.suffix(fits))
            let start = size.width - CGFloat(shown.count) * (bar + gap)
            for (index, level) in shown.enumerated() {
                let height = max(3, CGFloat(level) * size.height)
                let rect = CGRect(x: start + CGFloat(index) * (bar + gap), y: (size.height - height) / 2,
                                  width: bar, height: height)
                context.fill(Path(roundedRect: rect, cornerRadius: 1.5), with: .color(.red.opacity(0.8)))
            }
        }
    }
}

/// **His voice message in his bubble:** play it back, stop it.
struct VoiceNotePlayButton: View {
    @ObservedObject var player: VoiceNotePlayer
    let note: VoiceNoteRef

    var body: some View {
        Button { Task { await player.toggle(note) } } label: {
            HStack(spacing: 6) {
                Image(systemName: player.playing == note.id ? "stop.fill" : "play.fill")
                    .font(.system(size: 11, weight: .bold))
                Image(systemName: "waveform")
                    .font(.system(size: 13))
                Text(player.loading == note.id ? "Loading…" : "Voice message")
                    .font(.caption)
            }
            .padding(.horizontal, 10)
            .padding(.vertical, 5)
            .background(.quaternary, in: Capsule())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(player.playing == note.id ? "Stop voice message" : "Play voice message")
    }
}

/// The composer, or — while he records — the recording bar in its place.
/// Observes only the recorder, so typing never redraws for it.
struct VoiceNoteSlot<Composer: View>: View {
    @ObservedObject var composer: VoiceNoteComposer
    @ViewBuilder let content: () -> Composer

    var body: some View {
        if composer.state == .recording {
            VoiceNoteRecordingBar(composer: composer)
        } else {
            content()
        }
    }
}

/// Why a voice message could not be recorded or sent, in one plain line.
struct VoiceNoteNotice: View {
    @ObservedObject var composer: VoiceNoteComposer

    var body: some View {
        if let notice = composer.notice {
            HStack(alignment: .top, spacing: 6) {
                Image(systemName: "waveform.slash").accessibilityHidden(true)
                Text(notice).lineLimit(2).fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                Button { composer.dismissNotice() } label: { Image(systemName: "xmark") }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Dismiss")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 22)
            .padding(.top, 6)
            .accessibilityElement(children: .combine)
        }
    }
}

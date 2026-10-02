import SwiftUI
import Combine
import DeckKit

/// **Voice messages and spoken replies on the phone** — the Mac's DeckKit
/// pieces, wired to this app's deck:
///
/// - record a voice message (not a call): `VoiceNoteComposer`, transcribed on
///   the deck, lands as his own message and wakes the desk;
/// - his bubble plays it back: `VoiceNotePlayer`;
/// - the answer is read in the desk's call voice when "Spoken replies" is on:
///   `ReplySpeaker` over `DeckSpeechOutput` (Apple's voice only as fallback).
@MainActor
final class PhoneVoice: ObservableObject {
    let notes: VoiceNoteComposer
    let player: VoiceNotePlayer
    let replies: ReplySpeaker
    /// Listen to any agent message, in the desk's call voice, at his speed.
    let listen: ListenPlayer
    private var busyWatch: [AnyCancellable] = []

    init(store: PhoneStore) {
        notes = VoiceNoteComposer(recorder: SystemVoiceRecorder(),
                                  client: { [weak store] in store?.voiceNoteClient })
        player = VoiceNotePlayer(client: { [weak store] in store?.voiceNoteClient })
        replies = ReplySpeaker(output: PhoneVoice.callVoice(store: store))
        listen = ListenPlayer(client: { [weak store] in store?.speechClient }, fallback: SystemSpeechOutput())
        listen.onStart = { [replies] in replies.stop() }
        let (listen, notes) = (listen, notes)
        notes.$state.sink { [weak self] state in
            listen.voiceBusy(state != .idle || (self?.callIsUp ?? false))
        }.store(in: &busyWatch)
    }

    private var callIsUp = false

    /// The app's one call: while it is up (or a hold-to-talk turn is on), nothing is listened to.
    func bindBusy(to session: VoiceSession) {
        let (listen, notes) = (listen, notes)
        session.$state.sink { [weak self] state in
            self?.callIsUp = state != .idle
            listen.voiceBusy(state != .idle || notes.state != .idle)
        }.store(in: &busyWatch)
    }

    /// The desk's call voice from the deck; Apple's voice if the deck can't.
    static func callVoice(store: PhoneStore) -> SpeechOutput {
        DeckSpeechOutput(client: { [weak store] in store?.speechClient }, fallback: SystemSpeechOutput())
    }
}

private struct PhoneVoiceKey: EnvironmentKey {
    static let defaultValue: PhoneVoice? = nil
}

extension EnvironmentValues {
    /// Optional so renders and previews draw without one.
    var phoneVoice: PhoneVoice? {
        get { self[PhoneVoiceKey.self] }
        set { self[PhoneVoiceKey.self] = newValue }
    }
}

/// **"Spoken replies"**, in the thread's toolbar: on, the answer to his voice
/// message is read aloud in the desk's call voice; off, it stays text.
struct SpokenRepliesButton: View {
    @AppStorage(SpokenReplies.key) private var spoken = true

    var body: some View {
        Button {
            spoken.toggle()
            Haptics.tap()
        } label: {
            Image(systemName: spoken ? "speaker.wave.2.fill" : "speaker.slash")
        }
        .accessibilityLabel("Spoken replies")
        .accessibilityValue(spoken ? "On" : "Off")
        .accessibilityHint(spoken ? "Answers to your voice messages are read aloud. Double-tap to keep them as text."
                                  : "Answers stay text. Double-tap to hear them.")
    }
}

/// The composer's right-hand button when there is nothing typed: record.
struct VoiceNoteRecordButton: View {
    @ObservedObject var notes: VoiceNoteComposer
    let threadID: String

    var body: some View {
        Button {
            Haptics.tap()
            Task { await notes.start(threadID: threadID) }
        } label: {
            Image(systemName: notes.state == .sending ? "ellipsis" : "mic.fill")
                .font(.system(size: 17, weight: .bold))
                .foregroundStyle(PhoneTheme.canvas)
                .frame(width: 38, height: 38)
                .background(Circle().fill(Color.primary))
        }
        .disabled(notes.state != .idle)
        .accessibilityLabel("Record a voice message")
    }
}

/// While he records: cancel · his voice as a waveform · how long · send.
struct VoiceNoteRecordingBar: View {
    @ObservedObject var notes: VoiceNoteComposer

    var body: some View {
        HStack(spacing: 10) {
            Button { Haptics.tap(); notes.cancel() } label: {
                Image(systemName: "trash")
                    .font(.system(size: 16, weight: .semibold))
                    .frame(width: 38, height: 38)
            }
            .accessibilityLabel("Cancel recording")

            Circle().fill(Color.red).frame(width: 8, height: 8).accessibilityHidden(true)
            WaveformBars(levels: notes.levels)
                .frame(maxWidth: .infinity, minHeight: 28, maxHeight: 28)
                .accessibilityHidden(true)
            if let started = notes.startedAt {
                Text(timerInterval: started...Date.distantFuture, countsDown: false)
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            Button {
                Haptics.tap()
                Task { await notes.send() }
            } label: {
                Image(systemName: "arrow.up")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(PhoneTheme.canvas)
                    .frame(width: 38, height: 38)
                    .background(Circle().fill(Color.primary))
            }
            .accessibilityLabel("Send voice message")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(.bar)
    }
}

struct WaveformBars: View {
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

/// His voice message, in his bubble: play it back or stop it.
struct VoiceNotePlayRow: View {
    @ObservedObject var player: VoiceNotePlayer
    let note: VoiceNoteRef

    var body: some View {
        Button { Task { await player.toggle(note) } } label: {
            HStack(spacing: 8) {
                Image(systemName: player.playing == note.id ? "stop.circle.fill" : "play.circle.fill")
                    .font(.system(size: 26))
                Image(systemName: "waveform")
                Text(player.loading == note.id ? "Loading…" : "Voice message")
                    .font(.footnote.weight(.medium))
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(player.playing == note.id ? "Stop voice message" : "Play voice message")
    }
}

/// The typing composer, or the recording bar while he records — and the
/// reason a voice message could not go, above it.
struct VoiceComposerSwitch<Typing: View, Recording: View>: View {
    @ObservedObject var notes: VoiceNoteComposer
    @ViewBuilder let typing: () -> Typing
    @ViewBuilder let recording: () -> Recording

    var body: some View {
        VStack(spacing: 0) {
            if let notice = notes.notice {
                HStack(alignment: .top, spacing: 6) {
                    Image(systemName: "waveform.slash").accessibilityHidden(true)
                    Text(notice).fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 4)
                    Button { notes.dismissNotice() } label: { Image(systemName: "xmark") }
                        .accessibilityLabel("Dismiss")
                }
                .font(.footnote)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 16)
                .padding(.vertical, 6)
                .frame(maxWidth: .infinity)
                .background(.bar)
            }
            if notes.state == .recording { recording() } else { typing() }
        }
    }
}

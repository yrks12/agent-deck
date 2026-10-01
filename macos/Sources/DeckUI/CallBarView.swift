import SwiftUI
import DeckKit

/// **On a call: who with, for how long, and the two buttons that matter.**
/// Sits under the thread header while a call is up and draws nothing
/// otherwise. The timer is `Text(timerInterval:)` — the system redraws one
/// label once a second; nothing here animates.
public struct CallBarView: View {
    @ObservedObject var session: VoiceSession
    /// The desk, for its character. `nil` derives the face from the name.
    let agent: Agent?

    public init(session: VoiceSession, agent: Agent?) {
        self.session = session
        self.agent = agent
    }

    public var body: some View {
        // Only in the called desk's thread; anywhere else `CallPillView` carries it.
        if let call = session.call, CallPill.showsBar(session) {
            VStack(spacing: 0) {
                // C4: an open-mic call is a phone call — the desk's character,
                // large, listening and talking.
                if call.handsFree {
                    CallStageView(session: session, agent: agent)
                        .background(.bar)
                }
                HStack(spacing: 10) {
                    face(call)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("On a call with \(name)")
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                        Text(status(call))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .accessibilityElement(children: .combine)
                    Spacer(minLength: 8)
                    Text(timerInterval: call.startedAt...Date.distantFuture, countsDown: false)
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("Call length")
                    if call.handsFree {
                        Button { session.toggleMute() } label: {
                            Image(systemName: session.isMuted ? "mic.slash.fill" : "mic.fill")
                                .font(.system(size: 13, weight: .medium))
                                .foregroundStyle(session.isMuted ? Color.white : Color.primary)
                                .frame(width: 30, height: 30)
                                .background(session.isMuted ? AnyShapeStyle(Color.orange) : AnyShapeStyle(.quaternary),
                                            in: Circle())
                        }
                        .buttonStyle(.plain)
                        .help(session.isMuted ? "Unmute" : "Mute")
                        .accessibilityLabel(session.isMuted ? "Unmute" : "Mute")
                    }
                    Button { Task { await session.hangUp() } } label: {
                        Image(systemName: "phone.down.fill")
                            .font(.system(size: 13, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(width: 30, height: 30)
                            .background(Color.red, in: Circle())
                    }
                    .buttonStyle(.plain)
                    .help("Hang up")
                    .accessibilityLabel("Hang up on \(name)")
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(.bar)
                Divider()
            }
        }
    }

    private var name: String { session.call?.name ?? (session.displayName.isEmpty ? (session.desk ?? "the agent") : session.displayName) }

    @ViewBuilder
    private func face(_ call: VoiceSession.Call) -> some View {
        if let agent {
            AvatarView(agent: agent, size: 28)
        } else {
            AvatarView(look: AvatarLook.forName(call.desk), attention: .quiet, isDimmed: false,
                       localAvatarURL: nil, size: 28)
        }
    }

    private func status(_ call: VoiceSession.Call) -> String {
        if call.handsFree { return CallStage.make(session).isLive ? "Live voice" : "On-Mac speech" }
        if session.isMuted { return "Muted — \(name) can still talk" }
        switch session.state {
        case .listening: return "Listening…"
        case .sending: return "Asking \(name)…"
        case .waiting: return "\(name) is thinking…"
        case .speaking: return "\(name) is speaking"
        case .idle: return call.handsFree ? "Listening…" : "Hold the mic to talk"
        }
    }
}

/// **The phone's call button, in the thread header.** Call this desk, or —
/// while a call with it is up — hang up. One call at a time: calling a second
/// desk asks "End call with Atlas and call Acme?" first.
struct CallButton: View {
    @ObservedObject var session: VoiceSession

    private var onThisCall: Bool { session.call?.handsFree == true && CallPill.showsBar(session) }
    private var name: String { session.displayName.isEmpty ? "this agent" : session.displayName }

    var body: some View {
        if session.desk != nil {
            Button {
                DeckHaptics.tap()
                Task { if onThisCall { await session.hangUp() } else { await session.startCall() } }
            } label: {
                Image(systemName: onThisCall ? "phone.down.fill" : "phone.fill")
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(onThisCall ? Color.white : Color.primary)
                    .frame(width: 30, height: 30)
                    .background(onThisCall ? AnyShapeStyle(Color.red) : AnyShapeStyle(.quaternary), in: Circle())
            }
            .buttonStyle(.plain)
            .help(onThisCall ? "Hang up" : "Call \(name)")
            .accessibilityLabel(onThisCall ? "Hang up" : "Call \(name)")
            .confirmationDialog(
                session.pendingSwitch?.question ?? "",
                isPresented: Binding(get: { session.pendingSwitch != nil },
                                     set: { if !$0 { session.cancelSwitch() } })
            ) {
                Button(session.pendingSwitch.map { "Call \($0.to)" } ?? "Call") {
                    Task { await session.confirmSwitch() }
                }
                Button("Stay on the call", role: .cancel) { session.cancelSwitch() }
            }
        }
    }
}

/// **The call, from anywhere.** A compact strip at the top of the
/// conversation pane while he reads another desk (or none): who he is on a
/// call with, for how long, mute, hang up — and a click back to that desk.
struct CallPillView: View {
    @ObservedObject var session: VoiceSession
    let agents: [String: Agent]
    let openDesk: (String) -> Void

    var body: some View {
        if let pill = CallPill.make(session) {
            HStack(spacing: 8) {
                Button { openDesk(pill.desk) } label: {
                    HStack(spacing: 8) {
                        if let agent = agents[pill.desk] {
                            AvatarView(agent: agent, size: 20)
                        } else {
                            AvatarView(look: AvatarLook.forName(pill.desk), attention: .quiet, isDimmed: false,
                                       localAvatarURL: nil, size: 20)
                        }
                        Text("On a call with \(pill.name)")
                            .font(.callout.weight(.semibold))
                            .lineLimit(1)
                        Text(timerInterval: pill.startedAt...Date.distantFuture, countsDown: false)
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)
                .help("Back to \(pill.name)'s thread")
                .accessibilityLabel("On a call with \(pill.name). Open their thread")
                Spacer(minLength: 8)
                Button { session.toggleMute() } label: {
                    Image(systemName: pill.isMuted ? "mic.slash.fill" : "mic.fill")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(pill.isMuted ? Color.white : Color.primary)
                        .frame(width: 24, height: 24)
                        .background(pill.isMuted ? AnyShapeStyle(Color.orange) : AnyShapeStyle(.quaternary), in: Circle())
                }
                .buttonStyle(.plain)
                .help(pill.isMuted ? "Unmute" : "Mute")
                .accessibilityLabel(pill.isMuted ? "Unmute" : "Mute")
                Button { Task { await session.hangUp() } } label: {
                    Image(systemName: "phone.down.fill")
                        .font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(.white)
                        .frame(width: 24, height: 24)
                        .background(Color.red, in: Circle())
                }
                .buttonStyle(.plain)
                .help("Hang up")
                .accessibilityLabel("Hang up on \(pill.name)")
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 6)
            .background(Color.green.opacity(0.12))
            Divider()
        }
    }
}

/// **The app's one voice session.** A phone call belongs to the app, not to
/// the thread on screen ("when i move to other agent the call gets
/// disconnected"), so there is one, `shared`, for the life of the process.
/// Holds it without publishing, so the pane itself never redraws for voice
/// changes — only the mic, the call bar, the pill and the menu observe it.
@MainActor
final class VoiceDock: ObservableObject {
    static let shared = VoiceDock()

    let session: VoiceSession
    /// Record a voice message (not a call), and play his back.
    let notes: VoiceNoteComposer
    let notePlayer: VoiceNotePlayer
    /// The answer to a voice message, in the desk's call voice — when
    /// "Spoken replies" is on.
    let replies: ReplySpeaker

    init(session: VoiceSession? = nil) {
        notes = VoiceNoteComposer(recorder: SystemVoiceRecorder(), client: { VoiceDock.deckClient() })
        notePlayer = VoiceNotePlayer(client: { VoiceDock.deckClient() })
        replies = ReplySpeaker(output: VoiceDock.callVoice())
        self.session = session ?? VoiceSession(
            input: SystemSpeechInput(), output: VoiceDock.callVoice(), calls: VoiceDock.callClient,
            realtime: { dial in
                // C3: the deck minted a live voice for this call.
                RealtimeCallSession(offer: dial.offer, desk: dial.desk, displayName: dial.displayName,
                                    callID: dial.callID, threadID: dial.threadID,
                                    transport: URLSessionRealtimeTransport(), audio: EngineCallAudio(),
                                    calls: dial.calls)
            })
    }

    /// K6 over the same deck and token as the rest of the app. Built here —
    /// like `DeckApp`'s terminal client — because `DeckStore` keeps its client
    /// private; built on the first call, never when a thread opens.
    /// Replies in the desk's CALL voice from the deck; Apple's voice only
    /// when the deck cannot (traced as `tts.fallback`).
    static func callVoice() -> SpeechOutput {
        DeckSpeechOutput(client: { VoiceDock.deckClient() }, fallback: SystemSpeechOutput())
    }

    /// The live deck, or nil in fixture mode (no speech, no voice messages).
    static func deckClient() -> HTTPDeckClient? {
        guard case .live(let url) = DeckEndpoint.resolve(environment: ProcessInfo.processInfo.environment,
                                                         stored: UserDefaults.standard.string(forKey: "deckBaseURL"))
        else { return nil }
        return HTTPDeckClient(baseURL: url, tokens: KeychainTokenStore())
    }

    static func callClient() -> CallClient? {
        switch DeckEndpoint.resolve(environment: ProcessInfo.processInfo.environment,
                                    stored: UserDefaults.standard.string(forKey: "deckBaseURL")) {
        case .live(let url): return HTTPDeckClient(baseURL: url, tokens: KeychainTokenStore())
        case .fixture: return FixtureDeckClient()
        }
    }
}

extension View {
    /// Hands the open conversation to the voice session every time it changes.
    /// Uses the values the publishers emit: `@Published` fires before the
    /// property is set, so reading `store.thread` here would be one behind.
    func voiceFeed(_ session: VoiceSession, store: DeckStore) -> some View {
        self
            .onReceive(store.$thread) { thread in
                VoiceFeed.push(session, thread: thread, agents: store.agents)
            }
            .onReceive(store.$agents) { agents in
                VoiceFeed.push(session, thread: store.thread, agents: agents)
            }
    }
}

@MainActor
enum VoiceFeed {
    static func push(_ session: VoiceSession, thread: LoadState<ThreadScreen>, agents: [String: Agent]) {
        guard case .loaded(let screen) = thread,
              let desk = ThreadID.desk(ofDirect: screen.presentation.threadID) else {
            if case .loading = thread { return }   // opening: keep the call
            session.sync(desk: nil, displayName: "", voice: nil, working: false, messages: [])
            return
        }
        let agent = agents[desk]
        session.sync(desk: desk, displayName: screen.presentation.headerTitle, voice: agent?.voice,
                     working: agent?.state == .working, messages: screen.messages)
        VoiceDock.shared.replies.sync(desk: desk, messages: screen.messages)
    }
}

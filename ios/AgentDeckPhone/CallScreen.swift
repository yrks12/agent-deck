import SwiftUI
import DeckKit

// MARK: - Full-screen call

/// **The call, full screen.** The desk's character large and alive — the
/// Mac's `CallStageFace`, moving with his voice and the agent's — the timer,
/// captions in whole sentences, and mute / speaker / hang up. The chevron
/// minimises it to the pill; the call carries on.
struct CallScreen: View {
    @ObservedObject var center: PhoneCallCenter
    @ObservedObject var session: VoiceSession
    let agent: Agent?
    @EnvironmentObject private var store: PhoneStore
    @State private var screen: ScreenRoute?

    var body: some View {
        Group {
            if let call = session.call {
                if let live = session.realtime {
                    LiveCallContent(center: center, session: session, live: live, call: call, agent: agent,
                                    actions: actions(for: call))
                } else {
                    CallScreenContent(stage: CallStage.make(session), call: .init(call), agent: agent, caption: "",
                                      isMuted: session.isMuted, speakerOn: center.speakerOn,
                                      isOnHold: center.isOnHold, actions: actions(for: call))
                }
            } else {
                PhoneTheme.canvas.ignoresSafeArea()
            }
        }
        .fullScreenCover(item: $screen) { route in
            AgentScreenView(route: route, client: store.screenClient, messages: store.isReadOnly ? nil : store.client)
        }
    }

    /// The call's own buttons, plus one tap to the agent's screen: he can
    /// watch and drive its machine while it talks him through it.
    private func actions(for call: VoiceSession.Call) -> CallActions {
        var actions = CallActions(center)
        actions.screen = { screen = ScreenRoute(desk: call.desk, displayName: call.name, threadID: call.threadID); Haptics.tap() }
        return actions
    }
}

/// The live call publishes its own levels; observing it here keeps those
/// redraws inside the call screen.
private struct LiveCallContent: View {
    @ObservedObject var center: PhoneCallCenter
    @ObservedObject var session: VoiceSession
    @ObservedObject var live: RealtimeCallSession
    let call: VoiceSession.Call
    let agent: Agent?
    let actions: CallActions

    var body: some View {
        CallScreenContent(stage: CallStage.make(session), call: .init(call), agent: agent, caption: live.caption,
                          isMuted: session.isMuted, speakerOn: center.speakerOn,
                          isOnHold: center.isOnHold, actions: actions)
    }
}

struct CallActions {
    var minimise: () -> Void = {}
    /// Open the agent's screen. Nil draws no button (the render tests).
    var screen: (() -> Void)?
    var mute: () -> Void = {}
    var speaker: () -> Void = {}
    var hangUp: () -> Void = {}

    @MainActor
    init(_ center: PhoneCallCenter) {
        minimise = { [weak center] in center?.isCallScreenShown = false }
        mute = { [weak center] in center?.toggleMute(); Haptics.tap() }
        speaker = { [weak center] in center?.toggleSpeaker(); Haptics.tap() }
        hangUp = { [weak center] in Task { await center?.hangUp() }; Haptics.warning() }
    }

    init() {}
}

/// Pure: drawn straight by the render tests.
struct CallScreenContent: View {
    /// What the screen shows of the call.
    struct Line {
        var desk: String
        var name: String
        var startedAt: Date
        init(desk: String, name: String, startedAt: Date) {
            self.desk = desk; self.name = name; self.startedAt = startedAt
        }
        init(_ call: VoiceSession.Call) { self.init(desk: call.desk, name: call.name, startedAt: call.startedAt) }
    }

    let stage: CallStage
    let call: Line
    let agent: Agent?
    let caption: String
    let isMuted: Bool
    let speakerOn: Bool
    let isOnHold: Bool
    var actions = CallActions()

    private var shownStage: CallStage {
        guard isOnHold else { return stage }
        return CallStage(mode: .muted, level: 0, status: "On hold — your phone has the audio", isLive: stage.isLive)
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button(action: actions.minimise) {
                    Image(systemName: "chevron.down")
                        .font(.system(size: 20, weight: .semibold))
                        .frame(width: 44, height: 44)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Minimise call")
                Spacer()
                Text(stage.isLive ? "Live voice" : "Phone speech")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Spacer()
                if let screen = actions.screen {
                    Button(action: screen) {
                        Image(systemName: "display")
                            .font(.system(size: 19, weight: .semibold))
                            .frame(width: 44, height: 44)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("\(call.name)'s screen")
                } else {
                    Color.clear.frame(width: 44, height: 44)
                }
            }
            .padding(.horizontal, 8)

            VStack(spacing: 6) {
                Text(call.name)
                    .font(.largeTitle.weight(.semibold))
                    .lineLimit(1)
                Text(timerInterval: call.startedAt...Date.distantFuture, countsDown: false)
                    .font(.title3.monospacedDigit())
                    .foregroundStyle(.secondary)
            }
            .padding(.top, 12)

            Spacer(minLength: 12)
            CallStageFace(stage: shownStage, agent: agent, desk: call.desk, caption: "", size: 180)
            // Whole sentences, already trimmed to fit (`CallCaption`).
            Text(stage.mode == .speaking && !isOnHold ? caption : " ")
                .font(.body)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .lineLimit(3)
                .frame(maxWidth: .infinity, minHeight: 72, alignment: .top)
                .padding(.horizontal, 28)
            Spacer(minLength: 12)

            HStack(spacing: 36) {
                CallControl(symbol: isMuted ? "mic.slash.fill" : "mic.fill", label: isMuted ? "Unmute" : "Mute",
                            isOn: isMuted, action: actions.mute)
                CallControl(symbol: speakerOn ? "speaker.wave.3.fill" : "iphone", label: speakerOn ? "Speaker" : "Phone",
                            isOn: speakerOn, action: actions.speaker)
                Button(action: actions.hangUp) {
                    VStack(spacing: 8) {
                        Image(systemName: "phone.down.fill")
                            .font(.system(size: 28, weight: .semibold))
                            .foregroundStyle(.white)
                            .frame(width: 72, height: 72)
                            .background(Circle().fill(Color.red))
                        Text("End").font(.footnote).foregroundStyle(.secondary)
                    }
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Hang up on \(call.name)")
            }
            .padding(.bottom, 36)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PhoneTheme.canvas.ignoresSafeArea())
    }
}

private struct CallControl: View {
    let symbol: String
    let label: String
    let isOn: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            VStack(spacing: 8) {
                Image(systemName: symbol)
                    .font(.system(size: 26, weight: .medium))
                    .foregroundStyle(isOn ? PhoneTheme.canvas : .primary)
                    .frame(width: 72, height: 72)
                    .background(Circle().fill(isOn ? Color.primary : PhoneTheme.field))
                Text(label).font(.footnote).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }
}

// MARK: - The pill

/// "You're on a call with Atlas", above whatever he is reading. Tap to bring
/// the call back; mute and hang up without leaving the screen.
struct CallPillBar: View {
    struct Pill { var desk: String; var name: String; var startedAt: Date; var isMuted: Bool }
    let pill: Pill
    var isOnHold = false
    var expand: () -> Void = {}
    var mute: () -> Void = {}
    var hangUp: () -> Void = {}

    var body: some View {
        HStack(spacing: 10) {
            Button(action: expand) {
                HStack(spacing: 8) {
                    AvatarView(look: AvatarLook.forName(pill.desk), attention: .quiet, isDimmed: false,
                               localAvatarURL: nil, size: 26)
                    VStack(alignment: .leading, spacing: 0) {
                        Text(isOnHold ? "On hold · \(pill.name)" : "On a call with \(pill.name)")
                            .font(.subheadline.weight(.semibold))
                            .lineLimit(1)
                        Text(timerInterval: pill.startedAt...Date.distantFuture, countsDown: false)
                            .font(.caption.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Return to the call with \(pill.name)")
            Button(action: mute) {
                Image(systemName: pill.isMuted ? "mic.slash.fill" : "mic.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(PhoneTheme.field))
            }
            .buttonStyle(.plain)
            .accessibilityLabel(pill.isMuted ? "Unmute" : "Mute")
            Button(action: hangUp) {
                Image(systemName: "phone.down.fill")
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 34, height: 34)
                    .background(Circle().fill(Color.red))
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Hang up on \(pill.name)")
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 7)
        .background(Color.green.opacity(0.16))
        .background(.bar)
    }
}

/// Observes the call so only the pill redraws for it, not the whole app.
private struct LivePill: View {
    @ObservedObject var center: PhoneCallCenter
    @ObservedObject var session: VoiceSession

    var body: some View {
        if let pill = CallPill.phone(session, callScreenShown: center.isCallScreenShown) {
            CallPillBar(pill: .init(desk: pill.desk, name: pill.name, startedAt: pill.startedAt, isMuted: pill.isMuted),
                        isOnHold: center.isOnHold,
                        expand: { center.isCallScreenShown = true },
                        mute: { center.toggleMute(); Haptics.tap() },
                        hangUp: { Task { await center.hangUp() }; Haptics.warning() })
        }
    }
}

// MARK: - Wiring

extension View {
    /// The app's one call: full screen when up, the pill when minimised, the
    /// "End call with X and call Y?" question, and why a call ended.
    func phoneCalls(_ center: PhoneCallCenter, agents: [String: Agent]) -> some View {
        modifier(PhoneCallChrome(center: center, session: center.session, agents: agents))
    }
}

private struct PhoneCallChrome: ViewModifier {
    @ObservedObject var center: PhoneCallCenter
    @ObservedObject var session: VoiceSession
    let agents: [String: Agent]

    func body(content: Content) -> some View {
        content
            .safeAreaInset(edge: .top, spacing: 0) { LivePill(center: center, session: session) }
            .fullScreenCover(isPresented: $center.isCallScreenShown) {
                CallScreen(center: center, session: session, agent: session.call.flatMap { agents[$0.desk] })
            }
            .confirmationDialog(center.pendingSwitch?.question ?? "", isPresented: Binding(
                get: { center.pendingSwitch != nil }, set: { if !$0 { center.cancelSwitch() } }),
                titleVisibility: .visible) {
                Button("End call and call \(center.pendingSwitch?.to ?? "")", role: .destructive) {
                    Task { await center.confirmSwitch() }
                }
                Button("Stay on the call", role: .cancel) { center.cancelSwitch() }
            }
            .alert("Call", isPresented: Binding(
                get: { !center.isCallScreenShown && center.notice != nil },
                set: { if !$0 { center.notice = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(center.notice ?? "") }
    }
}

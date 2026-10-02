// A desk calling him, on the phone — what works and what does not.
//
// No paid Apple team: no CallKit, no PushKit/VoIP push, no APNs. So:
// * App open and in front: it asks the deck every 3 s, and a ring comes up
//   full screen here within a few seconds of the desk ringing. No ringtone.
// * App closed, in the background, or the phone locked: the ONLY alert is the
//   ntfy push (priority 5, "Atlas is calling — <reason>"). There is no ringing
//   screen on the lock screen. Tapping the push opens the app straight into
//   this screen if the ring is still inside its 30 s; after that it opens the
//   desk's thread with "Missed — Atlas's reason is in the thread".
// * In the background iOS suspends the app within seconds (unless a call
//   holds the audio), so the 3 s ask stops; background refresh is minutes
//   apart at best and cannot catch a 30 s ring.

import SwiftUI
import DeckKit

/// **"Atlas is calling."** Full screen: the desk's face, its reason, the
/// seconds left, and Answer / Decline. It goes by itself when the ring leaves
/// the deck's list or runs out.
struct IncomingCallView: View {
    let ring: IncomingRing
    let agent: Agent?
    var answer: () -> Void = {}
    var decline: () -> Void = {}
    /// The countdown reached zero.
    var ranOut: () -> Void = {}

    private var name: String { agent?.displayName ?? Agent.displayName(forWireName: ring.agent) }

    var body: some View {
        VStack(spacing: 0) {
            Spacer(minLength: 24)
            face
            Text("\(name) is calling")
                .font(.largeTitle.weight(.semibold))
                .multilineTextAlignment(.center)
                .padding(.top, 20)
            if !ring.reason.isEmpty {
                Text(ring.reason)
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(4)
                    .padding(.top, 8)
            }
            TimelineView(.periodic(from: .now, by: 1)) { context in
                let left = ring.secondsLeft(at: context.date)
                Text(ring.urgent ? "Urgent · \(left) s left" : "\(left) s left")
                    .font(.callout.monospacedDigit())
                    .foregroundStyle(ring.urgent ? Color.red : .secondary)
                    .accessibilityLabel("\(left) seconds left to answer")
            }
            .padding(.top, 12)
            Spacer(minLength: 24)
            HStack(spacing: 72) {
                button("Decline", symbol: "phone.down.fill", color: .red, action: decline)
                    .accessibilityLabel("Decline \(name)'s call")
                button("Answer", symbol: "phone.fill", color: .green, action: answer)
                    .accessibilityLabel("Answer \(name)")
            }
            .padding(.bottom, 48)
        }
        .padding(.horizontal, 28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(PhoneTheme.canvas.ignoresSafeArea())
        .task(id: ring.id) {
            let wait = ring.expiresAt - Date().timeIntervalSince1970
            if wait > 0 { try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000)) }
            if !Task.isCancelled { ranOut() }
        }
    }

    @ViewBuilder private var face: some View {
        if let agent {
            AvatarView(agent: agent, size: 140)
        } else {
            AvatarView(look: AvatarLook.forName(ring.agent), attention: .quiet, isDimmed: false,
                       localAvatarURL: nil, size: 140)
        }
    }

    private func button(_ label: String, symbol: String, color: Color, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            VStack(spacing: 10) {
                Image(systemName: symbol)
                    .font(.system(size: 32, weight: .semibold))
                    .foregroundStyle(.white)
                    .frame(width: 80, height: 80)
                    .background(Circle().fill(color))
                Text(label).font(.footnote).foregroundStyle(.secondary)
            }
        }
        .buttonStyle(.plain)
    }
}

/// The incoming call over the app, the one-line notice, and Answer handed
/// to the app's call once the incoming screen has gone (two full-screen
/// covers cannot be up at once).
struct IncomingCallChrome: ViewModifier {
    @ObservedObject var alerts: PhoneAlerts
    let calls: PhoneCallCenter?
    let agents: [String: Agent]
    @State private var answering: IncomingRing?

    func body(content: Content) -> some View {
        content
            .fullScreenCover(item: Binding(get: { alerts.incoming }, set: { _ in }), onDismiss: startAnswer) { ring in
                IncomingCallView(
                    ring: ring, agent: agents[ring.agent],
                    answer: { answering = ring; alerts.answered(ring); Haptics.tap() },
                    decline: { alerts.decline(ring); Haptics.tap() },
                    ranOut: { alerts.tick() })
            }
            .overlay(alignment: .top) {
                if let line = alerts.notice {
                    Text(line)
                        .font(.footnote.weight(.medium))
                        .lineLimit(1)
                        .padding(.horizontal, 14)
                        .padding(.vertical, 8)
                        .background(Capsule().fill(PhoneTheme.field))
                        .padding(.top, 4)
                        .onTapGesture { alerts.notice = nil }
                        .transition(.move(edge: .top).combined(with: .opacity))
                }
            }
            .animation(.default, value: alerts.notice)
            .task(id: alerts.notice) {
                guard alerts.notice != nil else { return }
                try? await Task.sleep(nanoseconds: 5_000_000_000)
                if !Task.isCancelled { alerts.notice = nil }
            }
    }

    private func startAnswer() {
        guard let ring = answering else { return }
        answering = nil
        let agent = agents[ring.agent]
        Task {
            await calls?.answer(ring: ring, name: agent?.displayName ?? Agent.displayName(forWireName: ring.agent),
                                voice: agent?.voice)
        }
    }
}

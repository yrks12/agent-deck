import SwiftUI
import DeckKit

/// **A desk is calling him.** Drawn over the whole window while
/// `DeckStore.incomingRing` is set, and gone the moment the deck stops listing
/// the ring. Silent by design: the system notification already made the one
/// sound, and nothing here plays another.
struct IncomingCallView: View {
    let ring: IncomingRing
    /// The desk, for its character. `nil` derives the face from the name.
    let agent: Agent?
    /// Why his Decline did not land, in one sentence.
    let problem: String?
    let answer: () -> Void
    let decline: () -> Void

    var body: some View {
        ZStack {
            Color.black.opacity(0.35)
                .ignoresSafeArea()
                .accessibilityHidden(true)
            VStack(spacing: 12) {
                face
                Text(ring.callerLine)
                    .font(.title2.weight(.semibold))
                    .multilineTextAlignment(.center)
                if !ring.reason.isEmpty {
                    Text(ring.reason)
                        .font(.body)
                        .multilineTextAlignment(.center)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // One label redrawn once a second; the card itself never is.
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    let left = ring.secondsLeft(at: context.date)
                    Text("\(left)s left")
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                        .accessibilityLabel("\(left) seconds left to answer")
                }
                if let problem {
                    FlatLabel(problem, systemImage: "exclamationmark.triangle")
                        .font(.callout)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                }
                HStack(spacing: 12) {
                    Button(action: decline) {
                        HStack(spacing: 6) {
                            Image(systemName: "phone.down.fill").accessibilityHidden(true)
                            Text("Decline")
                        }
                    }
                    .buttonStyle(.deckSecondary)
                    .keyboardShortcut(.cancelAction)
                    .accessibilityLabel("Decline")
                    Button(action: answer) {
                        HStack(spacing: 6) {
                            Image(systemName: "phone.fill").accessibilityHidden(true)
                            Text("Answer")
                        }
                    }
                    .buttonStyle(.deckPrimary)
                    .keyboardShortcut(.defaultAction)
                    .accessibilityLabel("Answer")
                }
                .padding(.top, 4)
            }
            .padding(24)
            .frame(width: 340)
            .card(radius: 18)
            .shadow(radius: 20)
            .accessibilityElement(children: .contain)
            .accessibilityLabel("Incoming call")
            .accessibilityAddTraits(.isModal)
        }
    }

    @ViewBuilder
    private var face: some View {
        if let agent {
            AvatarView(agent: agent, size: 64)
        } else {
            AvatarView(look: AvatarLook.forName(ring.agent), attention: .quiet, isDimmed: false,
                       localAvatarURL: nil, size: 64)
        }
    }
}

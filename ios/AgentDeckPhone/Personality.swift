import SwiftUI
import DeckKit

/// **The app's own character.** The Mac draws every desk as a face; the phone
/// borrows that for the moments with no desk on screen — first load, an empty
/// roster, an empty attention list — so the app never falls back to a grey
/// SF-symbol placeholder. One fixed look, so it is the same face every time.
enum Mascot {
    /// A soft mint squircle with a blush, looking up: calm, not busy.
    static let look = AvatarLook(shape: .squircle, tintIndex: 2, initials: "AD",
                                 gaze: .upRight, eyes: .normal, blush: true, seed: 42)
}

/// A character, a short headline and one line of explanation. The mood is
/// carried by the face (`attention` animates the eyes while working; `sleepy`
/// droops the lids), never by an exclamation mark.
struct CharacterState: View {
    var look: AvatarLook = Mascot.look
    var attention: RowAttention = .quiet
    var sleepy = false
    var size: CGFloat = 72
    let title: String
    var message: String?

    var body: some View {
        VStack(spacing: 10) {
            AvatarView(look: look, attention: attention, isDimmed: sleepy, localAvatarURL: nil, size: size)
                .padding(.bottom, 4)
            Text(title).font(.headline).multilineTextAlignment(.center)
            if let message {
                Text(message)
                    .font(.subheadline).foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.horizontal, 32)
        .accessibilityElement(children: .combine)
        // Where it was laid out, on screen: the layout tests read this.
        .background(GeometryReader { geo in
            Color.clear.preference(key: CharacterStateFrames.self, value: [title: geo.frame(in: .global)])
        })
    }
}

/// Every `CharacterState` on screen, by title, in screen coordinates.
struct CharacterStateFrames: PreferenceKey {
    static let defaultValue: [String: CGRect] = [:]
    static func reduce(value: inout [String: CGRect], nextValue: () -> [String: CGRect]) {
        value.merge(nextValue(), uniquingKeysWith: { _, new in new })
    }
}

extension ConnectionState {
    /// The toolbar's word for the link to the deck, a little warmer than the
    /// Mac's status label.
    var phoneLabel: String {
        switch self {
        case .idle: return "Offline"
        case .connecting: return "Finding your deck"
        case .live: return "Live"
        case .reconnecting: return "Reconnecting"
        }
    }
}

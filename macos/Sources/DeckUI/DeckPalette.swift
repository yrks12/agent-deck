import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif
import DeckKit

/// **The one palette, drawn by the Mac and the iPhone alike.** Compiled into
/// DeckUI for the Mac and straight into the iPhone target (see
/// `ios/project.yml`, like `AvatarView.swift`), so the two apps are not two
/// palettes that happen to agree — they are the same file. The numbers are
/// `DeckTokens`; `ThemeParityTests` resolves these colours on the Mac in both
/// appearances and checks they are those numbers.
enum DeckPalette {
    static let canvas = grey(DeckTokens.canvas)
    static let ownerBubble = grey(DeckTokens.ownerBubble)
    static let agentBubble = grey(DeckTokens.agentBubble)
    static let card = grey(DeckTokens.card)
    static let cardStroke = grey(DeckTokens.cardStroke)
    static let field = grey(DeckTokens.field)
    /// Black on white, white on black: the unread badge and the primary
    /// button. A plain `Color.primary` is turned translucent by the Mac
    /// sidebar's vibrancy, which drew the badge as a grey smudge.
    static let ink = grey(DeckTokens.ink)

    /// The orange "waiting for you" as text: darkened in the light appearance
    /// for contrast (`AttentionPalette`).
    #if os(macOS)
    static let waiting = Color(nsColor: AttentionPalette.waitingText)
    static let working = Color(nsColor: .systemGreen)
    #else
    static let waiting = Color(uiColor: AttentionPalette.waitingText)
    static let working = Color(uiColor: .systemGreen)
    #endif

    static let bubbleRadius = CGFloat(DeckTokens.bubbleRadius)

    static func tone(_ tone: StatusLine.Tone) -> Color {
        switch tone {
        case .waiting: return waiting
        case .working: return working
        case .neutral: return .secondary
        }
    }

    #if os(macOS)
    static func grey(_ token: DeckTokens.Grey) -> Color {
        Color(nsColor: greyColor(token))
    }

    /// The AppKit colour, resolved per appearance at draw time — exposed so a
    /// test can resolve it under aqua and darkAqua and read the white level.
    static func greyColor(_ token: DeckTokens.Grey) -> NSColor {
        NSColor(name: nil) { appearance in
            appearance.bestMatch(from: [.aqua, .darkAqua]) == .darkAqua
                ? NSColor(white: token.dark, alpha: 1) : NSColor(white: token.light, alpha: 1)
        }
    }
    #else
    static func grey(_ token: DeckTokens.Grey) -> Color {
        Color(uiColor: UIColor { $0.userInterfaceStyle == .dark
            ? UIColor(white: token.dark, alpha: 1) : UIColor(white: token.light, alpha: 1) })
    }
    #endif
}

/// **Feedback on his own decisions, never on incoming traffic.** A tap on the
/// phone; on the Mac, the trackpad's quiet click where there is a Force Touch
/// trackpad and nothing at all where there is not — the subtle equivalent.
enum DeckHaptics {
    #if os(macOS)
    static func tap() { perform(.generic) }
    static func success() { perform(.levelChange) }
    static func warning() { perform(.alignment) }
    static func failure() { perform(.alignment) }

    private static func perform(_ pattern: NSHapticFeedbackManager.FeedbackPattern) {
        NSHapticFeedbackManager.defaultPerformer.perform(pattern, performanceTime: .now)
    }
    #else
    static func tap() { UIImpactFeedbackGenerator(style: .light).impactOccurred() }
    static func success() { UINotificationFeedbackGenerator().notificationOccurred(.success) }
    static func warning() { UINotificationFeedbackGenerator().notificationOccurred(.warning) }
    static func failure() { UINotificationFeedbackGenerator().notificationOccurred(.error) }
    #endif
}

/// A raised card: the chief, the attention banner, a decision, the usage meter.
struct CardBackground: ViewModifier {
    var radius: CGFloat = CGFloat(DeckTokens.cardRadius)
    func body(content: Content) -> some View {
        content
            .background(DeckPalette.card, in: RoundedRectangle(cornerRadius: radius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: radius, style: .continuous)
                .strokeBorder(DeckPalette.cardStroke, lineWidth: 0.5))
    }
}

extension View {
    func card(radius: CGFloat = CGFloat(DeckTokens.cardRadius)) -> some View {
        modifier(CardBackground(radius: radius))
    }
}

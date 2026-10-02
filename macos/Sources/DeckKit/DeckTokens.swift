import Foundation

/// **The look, as numbers, once — for the Mac and the iPhone both.**
///
/// The owner asked for the Mac app to feel exactly like the iPhone does. The
/// two apps used to keep their own greys: the phone's own-bubble was 23% white
/// in the dark, the Mac's 26%; the phone's canvas was black, the Mac's the
/// window's grey. Written here, as plain numbers with no UI framework, the
/// palette each app builds (`DeckPalette`, compiled into both targets) cannot
/// drift, and `ThemeParityTests` checks that what the Mac resolves on screen
/// is these numbers in both appearances.
///
/// Greyscale surfaces only; the colour on screen is the characters' and the
/// two meanings (orange = waiting on you, green = working).
public enum DeckTokens {

    /// A neutral grey, as white level 0...1, in each appearance.
    public struct Grey: Equatable, Sendable {
        public let light: Double
        public let dark: Double
        public init(light: Double, dark: Double) {
            self.light = light
            self.dark = dark
        }
    }

    /// Behind everything: the roster and the conversation.
    public static let canvas = Grey(light: 1.0, dark: 0.0)
    /// His own bubble, on the right.
    public static let ownerBubble = Grey(light: 0.87, dark: 0.23)
    /// The desk's bubble, on the left.
    public static let agentBubble = Grey(light: 0.955, dark: 0.12)
    /// A raised card: the chief, the attention banner, a decision.
    public static let card = Grey(light: 0.965, dark: 0.10)
    public static let cardStroke = Grey(light: 0.88, dark: 0.20)
    /// The box he types in.
    public static let field = Grey(light: 0.94, dark: 0.14)
    /// Text-strength fill: the unread badge, the primary button.
    public static let ink = Grey(light: 0.0, dark: 1.0)

    public static let bubbleRadius: Double = 20
    public static let cardRadius: Double = 18
    public static let heroRadius: Double = 22
    public static let bannerRadius: Double = 14
    public static let fieldRadius: Double = 22

    /// The characters' sizes, in points, in the places both apps draw them.
    public static let heroAvatar: Double = 76
    public static let rowAvatar: Double = 48
    public static let headerAvatar: Double = 28
}

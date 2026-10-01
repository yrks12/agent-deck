import Foundation

/// Which deck the app talks to, decided in one testable place.
///
/// The default is the **real deck on this Mac**. The fixture is a thing you ask
/// for, not a thing you fall into: an app bundle opened from the Dock or the
/// Finder inherits no environment, so a fixture-by-default app can never show
/// the owner his own sessions no matter what is running.
public enum DeckEndpoint {

    /// The daemon's address on this machine. It runs from a LaunchAgent, so it
    /// is the right assumption rather than a hopeful one.
    public static let localDeck = URL(string: "http://127.0.0.1:7788")!

    public enum Choice: Equatable, Sendable {
        case live(URL)
        case fixture

        /// The window must say which it is. "Why am I seeing a mock" is only
        /// answerable if the title answers it.
        public var windowTitle: String {
            switch self {
            case .live: return "Agent Deck"
            case .fixture: return "Agent Deck (fixture)"
            }
        }
    }

    /// `DECK_FIXTURE` asks for the mock. `DECK_URL` names a different deck --
    /// the box on the mesh, say. `stored` is the Base URL typed into Settings,
    /// which used to be written and never read. Anything unusable falls to the
    /// local deck, never to the fixture: showing invented agents is a worse
    /// answer to a typo than failing to connect and saying so.
    ///
    /// Precedence, highest first: the fixture flag, the environment's URL, the
    /// saved field, the deck on this Mac. A launch that names a deck is never
    /// silently overridden by whatever is left in the panel.
    public static func resolve(environment: [String: String], stored: String? = nil) -> Choice {
        if let flag = environment["DECK_FIXTURE"],
           !flag.isEmpty, flag != "0", flag.lowercased() != "false" {
            return .fixture
        }
        for candidate in [environment["DECK_URL"], stored] {
            if let url = usableURL(candidate) { return .live(url) }
        }
        return .live(localDeck)
    }

    private static func usableURL(_ raw: String?) -> URL? {
        guard let raw else { return nil }
        guard let url = URL(string: raw.trimmingCharacters(in: .whitespacesAndNewlines)),
              url.scheme != nil, url.host != nil else { return nil }
        return url
    }
}

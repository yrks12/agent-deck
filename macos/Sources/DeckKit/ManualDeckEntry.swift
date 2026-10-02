import Foundation

/// **A deck's address and token, typed by hand.**
///
/// The pairing code (ADK1, `PairingCode`) is the front door; this is the side
/// door for a deck that is already running and whose token he already holds —
/// the phone on the same VPN as a Mac that is already connected. Forgiving in
/// what it takes (`10.0.0.1:7789`, a pasted `…/v1/`, stray whitespace) and
/// strict in what it keeps: a scheme, a host, no path.
public struct ManualDeckEntry: Equatable, Sendable {
    public let url: URL
    public let token: String

    public enum Problem: Error, Equatable, Sendable {
        case noAddress, notAnAddress, noToken

        public var userFacingText: String {
            switch self {
            case .noAddress: return "Enter the deck's address, like your-deck:7789."
            case .notAnAddress: return "That doesn't look like a deck address. Use http:// or https:// and a host."
            case .noToken: return "Enter the deck's API token."
            }
        }
    }

    public static func parse(url rawURL: String, token rawToken: String) throws -> ManualDeckEntry {
        var text = rawURL.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { throw Problem.noAddress }
        if !text.contains("://") { text = "http://" + text }
        guard var parts = URLComponents(string: text),
              let scheme = parts.scheme?.lowercased(), scheme == "http" || scheme == "https",
              let host = parts.host, !host.isEmpty else { throw Problem.notAnAddress }
        parts.scheme = scheme
        parts.path = ""
        parts.query = nil
        parts.fragment = nil
        guard let url = parts.url else { throw Problem.notAnAddress }

        let token = rawToken.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !token.isEmpty else { throw Problem.noToken }
        return ManualDeckEntry(url: url, token: token)
    }
}

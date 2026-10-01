import Foundation

/// K9: where this process keeps its connection. `DECK_STATE_SUITE=<s>` moves
/// the UserDefaults domain to `<bundle id>.<s>` and the Keychain account
/// to `deck-api-token.<s>`, so an automated pairing test can never overwrite the
/// owner's real connection.
public struct StateSuite: Sendable {
    public static let bundleID = DeckIdentity.bundleID
    public static let keychainService = DeckIdentity.bundleID
    public static let baseAccount = "deck-api-token"

    /// nil = the real settings.
    public let name: String?

    public init(environment: [String: String] = ProcessInfo.processInfo.environment) {
        let raw = (environment["DECK_STATE_SUITE"] ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        // A mistyped name still isolates: odd characters become "_" instead of
        // the variable quietly meaning "use the real settings".
        let cleaned = String(raw.unicodeScalars.map {
            CharacterSet.alphanumerics.contains($0) || $0 == "-" || $0 == "_" ? Character($0) : "_"
        })
        name = cleaned.isEmpty ? nil : cleaned
    }

    public var defaultsSuiteName: String? { name.map { "\(Self.bundleID).\($0)" } }

    public var keychainAccount: String {
        name.map { "\(Self.baseAccount).\($0)" } ?? Self.baseAccount
    }

    public var defaults: UserDefaults {
        guard let suite = defaultsSuiteName, let d = UserDefaults(suiteName: suite) else { return .standard }
        return d
    }

    public func tokenStore() -> KeychainTokenStore {
        KeychainTokenStore(service: Self.keychainService, account: keychainAccount)
    }
}

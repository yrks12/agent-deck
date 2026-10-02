import Foundation

/// The one thing a failure screen offers to do about itself.
///
/// A button that cannot possibly work is worse than no button: it costs a
/// click, it fails again, and it teaches the reader that the app does not know
/// what is wrong. So there is a `none` case, and it is used.
public enum FailureAction: Equatable, Sendable {
    /// Trying again could genuinely produce a different answer.
    case retry(title: String)
    /// The fix is a token, and the token is entered in Settings.
    case openSettings(title: String)
    /// Nothing the reader can press changes this.
    case none
}

/// What a failed surface says out loud: one heading, one sentence, one action.
///
/// Both the sidebar and the conversation pane render this, so the two panes
/// cannot describe the same failure in different words — which is exactly what
/// they did when the deck was reachable and the Keychain was empty.
public struct FailurePresentation: Equatable, Sendable {
    public var heading: String
    public var detail: String
    public var action: FailureAction
    public var symbol: String

    public init(heading: String, detail: String, action: FailureAction, symbol: String) {
        self.heading = heading
        self.detail = detail
        self.action = action
        self.symbol = symbol
    }

    /// Four causes that look identical from a distance and are not:
    ///
    /// - `missingToken` — this Mac has no token. The deck is very probably fine;
    ///   nothing has been asked of it yet. Never worded as a transport fault.
    /// - `unauthorized` — a token was sent and refused. The deck is up.
    /// - `authNotConfigured` — the *server* has no token set. Nothing to type.
    /// - `transport` — the socket did not open. The only case where
    ///   "Can't reach the deck" is a true sentence.
    public static func make(_ error: DeckError) -> FailurePresentation {
        switch error {
        case .missingToken:
            return FailurePresentation(
                heading: "Not connected yet",
                detail: error.userFacingText,
                action: .openSettings(title: "Open Settings"),
                symbol: "key.slash"
            )
        case .unauthorized:
            return FailurePresentation(
                heading: "The deck rejected this token",
                detail: error.userFacingText,
                action: .openSettings(title: "Replace the token"),
                symbol: "lock.trianglebadge.exclamationmark"
            )
        case .authNotConfigured:
            return FailurePresentation(
                heading: "This deck has no token of its own",
                detail: error.userFacingText,
                action: .none,
                symbol: "lock.slash"
            )
        case .transport:
            return FailurePresentation(
                heading: "Can't reach the deck",
                detail: error.userFacingText,
                action: .retry(title: "Try again"),
                symbol: "bolt.horizontal.circle"
            )
        case .decoding:
            return FailurePresentation(
                heading: "The deck sent something unreadable",
                detail: error.userFacingText,
                action: .retry(title: "Try again"),
                symbol: "questionmark.square.dashed"
            )
        default:
            return FailurePresentation(
                heading: "The deck refused this",
                detail: error.userFacingText,
                action: error.isRetryable ? .retry(title: "Try again") : .none,
                symbol: "exclamationmark.triangle"
            )
        }
    }
}

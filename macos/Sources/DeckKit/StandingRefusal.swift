import Foundation

/// A refused §23 call, by `reason`. Read only off the standing-approval routes:
/// `revoked` and `missing_field` mean other things elsewhere on the deck.
public enum StandingRefusal: String, Sendable, Hashable, CaseIterable {
    case badKind = "bad_kind"
    case missingField = "missing_field"
    case noLimit = "no_limit"
    case badLimit = "bad_limit"
    case tooWide = "too_wide"
    case badPolicy = "bad_policy"
    case unknownPolicy = "unknown_policy"
    case neverCoverable = "never_coverable"
    case notProposed = "not_proposed"
    case revoked
    case noDesk = "no_desk"

    public func text(detail: String) -> String {
        let tail = detail.isEmpty ? "" : " (\(detail))"
        switch self {
        case .badKind: return "The deck does not know that kind of action\(tail)."
        case .missingField: return "Pick a desk, or every desk\(tail)."
        case .noLimit: return "Set a daily limit: a number of actions, or dollars. Spending money needs a dollar limit\(tail)."
        case .badLimit: return "A limit or an expiry must be a number above zero\(tail)."
        case .tooWide: return "A command approval needs a command pattern, such as gh pr*\(tail)."
        case .badPolicy: return "The deck could not read that as a standing approval\(tail)."
        case .unknownPolicy: return "That standing approval is no longer on this deck."
        case .neverCoverable:
            return "This always asks: money to others, deleting data, a credential or account security can never be covered\(tail)."
        case .notProposed: return "That one is no longer a proposal, so nothing changed."
        case .revoked: return "That standing approval was already revoked, so nothing changed."
        case .noDesk: return "The deck cannot tell which desk asked, so no limit can be scoped to it. Answer this one on its own."
        }
    }
}

extension DeckError {
    /// A refusal from a §23 route: its own reasons first, then the deck's
    /// common ones (`unauthorized`, `unknown_ask`, …) exactly as elsewhere.
    public static func standing(status: Int, body: Data) -> DeckError {
        struct Envelope: Decodable { let reason: String?; let detail: String? }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body)
        if let refusal = StandingRefusal(rawValue: envelope?.reason ?? "") {
            return .standingRefused(refusal, detail: envelope?.detail ?? "")
        }
        return DeckError(status: status, body: body)
    }
}

import Foundation

/// **Approve / No on a proposal's notification** (§23, the needs-you item).
/// A desk proposed a standing approval; the banner answers it without opening
/// the app. PURE apart from `send`, so both apps read the buttons the same way.
public struct StandingAlertAnswer: Equatable, Sendable {
    public enum Verb: Equatable, Sendable { case approve, revoke }

    public static let categoryIdentifier = "deck.standing"
    public static let approveAction = "deck.standing.approve"
    public static let revokeAction = "deck.standing.revoke"
    static let policyKey = "deck_policy"

    public let policyID: String
    public let verb: Verb

    public init(policyID: String, verb: Verb) {
        self.policyID = policyID
        self.verb = verb
    }

    /// One of the two buttons on a standing alert; `nil` for anything else.
    public init?(actionIdentifier: String, userInfo: [AnyHashable: Any]) {
        guard let policy = userInfo[Self.policyKey] as? String, !policy.isEmpty else { return nil }
        switch actionIdentifier {
        case Self.approveAction: self.init(policyID: policy, verb: .approve)
        case Self.revokeAction: self.init(policyID: policy, verb: .revoke)
        default: return nil
        }
    }

    /// `nil` when done — or already settled elsewhere (`not_proposed`,
    /// `revoked`), which leaves nothing to do; otherwise the deck's reason.
    public func send(via client: StandingApprovalsClient) async -> String? {
        do {
            switch verb {
            case .approve: _ = try await client.approveStanding(id: policyID)
            case .revoke: _ = try await client.revokeStanding(id: policyID)
            }
            return nil
        } catch let error as DeckError {
            switch error {
            case .standingRefused(.notProposed, _), .standingRefused(.revoked, _): return nil
            default: return error.userFacingText
            }
        } catch {
            return DeckError.transport(error.localizedDescription).userFacingText
        }
    }
}

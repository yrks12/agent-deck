import Foundation
import DeckKit

/// **The Standing approvals screen's state**, for both apps (compiled into the
/// iPhone target via `ios/project.yml`). It asks the deck and acts on his taps;
/// every word and every order is `StandingPresentation`'s.
///
/// Nothing is optimistic: a row changes when the deck says it did. A refusal
/// is shown in the deck's own terms (its `reason`), never as a bare status.
@MainActor
public final class StandingApprovalsModel: ObservableObject {
    public enum Phase: Equatable { case loading, unavailable, failed(String), loaded }

    public let client: StandingApprovalsClient
    @Published public private(set) var phase: Phase = .loading
    @Published public private(set) var policies: [StandingPolicy] = []
    @Published public private(set) var audit: [StandingAuditLine] = []
    /// Rows with a tap in flight: their buttons are off until the deck answers.
    @Published public private(set) var busy: Set<String> = []
    /// The last refusal or failure, in words. Cleared by the next success.
    @Published public var problem: String?

    public init(client: StandingApprovalsClient) { self.client = client }

    public var sections: StandingPresentation.Sections { StandingPresentation.sections(policies) }
    public var proposedCount: Int { StandingPresentation.proposedCount(policies) }

    /// Also the live refresh: usage moves while the screen is open. A failed
    /// refresh keeps what is on screen.
    public func load() async {
        do {
            policies = try await client.standingApprovals(status: nil, desk: nil)
            phase = .loaded
        } catch {
            if Self.isMissingRoute(error) { phase = .unavailable; return }
            if phase == .loaded { return }
            phase = .failed(Self.text(error))
        }
    }

    public func loadAudit() async {
        do { audit = try await client.standingAudit(desk: nil, policyID: nil, limit: 200) }
        catch { problem = Self.text(error) }
    }

    public func approve(_ policy: StandingPolicy) async {
        await act(policy.id) { try await self.client.approveStanding(id: policy.id) }
    }

    /// Revoke an active one, or dismiss a proposal: the same DELETE.
    public func revoke(_ policy: StandingPolicy) async {
        await act(policy.id) { try await self.client.revokeStanding(id: policy.id) }
    }

    /// Create (`editing == nil`) or edit. `true` when the deck took it, so the
    /// form can close; on a refusal the form stays open with the reason.
    @discardableResult
    public func save(_ draft: StandingDraft, editing: String? = nil) async -> Bool {
        if let local = StandingPresentation.problem(draft) { problem = local; return false }
        let key = editing ?? "new"
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            if let editing {
                replace(try await client.updateStanding(id: editing, draft))
            } else {
                replace(try await client.createStanding(draft))
            }
            problem = nil
            return true
        } catch {
            problem = Self.text(error)
            return false
        }
    }

    private func act(_ id: String, _ call: @escaping () async throws -> StandingPolicy) async {
        guard !busy.contains(id) else { return }
        busy.insert(id)
        defer { busy.remove(id) }
        do {
            replace(try await call())
            problem = nil
        } catch {
            problem = Self.text(error)
            await load()
        }
    }

    private func replace(_ row: StandingPolicy) {
        if let i = policies.firstIndex(where: { $0.id == row.id }) { policies[i] = row } else { policies.append(row) }
        phase = .loaded
    }

    /// An older deck has no §23 routes: a state, not an error.
    static func isMissingRoute(_ error: Error) -> Bool {
        if case .http(404, let reason)? = error as? DeckError, reason.isEmpty { return true }
        return false
    }

    static func text(_ error: Error) -> String {
        (error as? DeckError)?.userFacingText ?? DeckError.transport(error.localizedDescription).userFacingText
    }
}

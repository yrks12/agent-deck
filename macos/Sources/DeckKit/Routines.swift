import Foundation

/// The routines panel's state. Four outcomes, kept apart on purpose: loading,
/// a real empty list, a failure, and rows.
public actor RoutinesModel {
    public static let emptyText =
        "No routines yet. A routine sends this agent the same prompt on a schedule."

    private let client: DeckClient

    public private(set) var all: [Routine] = []
    public private(set) var problem: String?
    public private(set) var hasLoaded = false

    public init(client: DeckClient) {
        self.client = client
    }

    /// Non-nil only when the deck answered, answered cleanly, and had nothing.
    /// A failure is never an empty list.
    public var emptyMessage: String? {
        guard hasLoaded, problem == nil, all.isEmpty else { return nil }
        return Self.emptyText
    }

    public func routines(forAgent name: String) -> [Routine] {
        all.filter { $0.agentName == name }
    }

    public func load() async {
        do {
            all = try await client.routines()
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
        hasLoaded = true
    }

    @discardableResult
    public func create(_ draft: RoutineDraft) async -> Bool {
        if let issue = draft.problem {
            problem = issue
            return false
        }
        do {
            // The deck's answer is what is shown — it carries the `next_run_at`
            // this client is not allowed to compute.
            let created = try await client.createRoutine(draft)
            all.append(created)
            problem = nil
            return true
        } catch let error as DeckError {
            problem = error.userFacingText
            return false
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
            return false
        }
    }

    public func setEnabled(_ enabled: Bool, id: String) async {
        do {
            let updated = try await client.setRoutine(id: id, enabled: enabled)
            if let index = all.firstIndex(where: { $0.id == id }) {
                all[index] = updated
            }
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }

    public func delete(id: String) async {
        do {
            try await client.deleteRoutine(id: id)
            all.removeAll { $0.id == id }
            problem = nil
        } catch let error as DeckError {
            problem = error.userFacingText
        } catch {
            problem = DeckError.transport(error.localizedDescription).userFacingText
        }
    }
}

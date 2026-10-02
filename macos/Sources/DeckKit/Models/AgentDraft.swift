import Foundation

/// The "+" form: what a new desk needs before the deck will take it.
///
/// The reference app creates an agent by having a conversation about it. This
/// one asks the six questions the roster actually stores, because the deck's
/// hire route needs all six and inventing any of them client-side would put a
/// desk in the wrong folder or under the wrong boss.
public struct AgentDraft: Equatable, Sendable {
    /// The engines the deck can put at a desk. Anything else is refused by the
    /// server with `unknown_engine`, so the form offers only these.
    public static let engines = ["claude", "opencode", "codex"]

    public var name: String
    public var title: String
    public var detail: String
    public var directory: String
    public var engine: String
    /// Who it reports to. `nil` is the top of the org chart, which is a real
    /// answer rather than a missing one.
    public var boss: String?

    public init(
        name: String = "",
        title: String = "",
        detail: String = "",
        directory: String = "",
        engine: String = "claude",
        boss: String? = nil
    ) {
        self.name = name
        self.title = title
        self.detail = detail
        self.directory = directory
        self.engine = engine
        self.boss = boss
    }

    /// Names key every thread id (`direct:<name>`) and every org edge on this
    /// deck, so the charset is checked here rather than discovered as a 400.
    private static let allowed = CharacterSet(charactersIn:
        "abcdefghijklmnopqrstuvwxyz0123456789 -_")

    /// What is stopping this form being sent, in the user's words. `nil` means
    /// it is ready.
    public var problem: String? {
        let name = self.name.trimmingCharacters(in: .whitespacesAndNewlines)
        if name.isEmpty {
            return "Give the agent a name. It is the identity every conversation here is keyed on."
        }
        if name.lowercased() != name || !CharacterSet(charactersIn: name).isSubset(of: Self.allowed) {
            return "A name can use lowercase letters, numbers, spaces, hyphens and underscores — it goes into every thread id on this deck."
        }
        if directory.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Choose the folder this agent works in."
        }
        if !Self.engines.contains(engine) {
            return "Pick one of the engines this deck can run: \(Self.engines.joined(separator: ", "))."
        }
        return nil
    }

    public var isReady: Bool { problem == nil }

    /// The wire body. `reports_to` is sent as an explicit `null` for a
    /// top-level desk: absent and "nobody" are different answers, and the deck
    /// should not have to guess which one was meant.
    public func encodedBody() throws -> Data {
        var object: [String: Any] = [
            "name": name.trimmingCharacters(in: .whitespacesAndNewlines),
            "label": title,
            "charter": detail,
            "cwd": directory.trimmingCharacters(in: .whitespacesAndNewlines),
            "engine": engine,
            "reports_to": NSNull(),
        ]
        if let boss, !boss.isEmpty { object["reports_to"] = boss }
        return try JSONSerialization.data(withJSONObject: object)
    }
}

/// Why the deck said no. Each one is a different thing for the user to change,
/// so each one gets its own sentence — the server's `detail` is kept because
/// it is where the numbers and names are.
public enum CreateAgentRefusal: String, Equatable, Sendable, CaseIterable {
    case nameTaken = "name_taken"
    case tooDeep = "too_deep"
    case tooManyLive = "too_many_live"
    case noSuchBoss = "no_such_boss"
    case noSuchDirectory = "no_such_cwd"
    case unknownEngine = "unknown_engine"
    case missingField = "missing_field"

    public var sentence: String {
        switch self {
        case .nameTaken:
            return "That name is already on this deck. Pick a different name."
        case .tooDeep:
            return "That is too far down the chain — pick someone nearer the top under \"Reports to\"."
        case .tooManyLive:
            return "This deck is already running as many agents as it allows. Stop or park one, then add this."
        case .noSuchBoss:
            return "Nobody on this deck by that name to report to. Pick an existing agent under \"Reports to\"."
        case .noSuchDirectory:
            return "That folder does not exist on this Mac. Pick a directory the agent can work in."
        case .unknownEngine:
            return "This deck cannot run that engine. Choose one of: \(AgentDraft.engines.joined(separator: ", "))."
        case .missingField:
            return "The deck needs one more thing before it can create this desk — the missing field is named below."
        }
    }

    /// The server's own words carry the count and the names — "there are
    /// already 8 agents running" is the sentence that tells him what to do.
    public func text(detail: String) -> String {
        detail.isEmpty ? sentence : "\(sentence) (\(detail))"
    }
}

import Foundation

/// Everything "+" is allowed to ask for.
///
/// Deliberately one field. A name, a title and a description are the agent's to
/// write once it knows what it is for — asking the owner to invent them before
/// the first sentence has been said is asking him to do the agent's job.
///
/// There is no folder here either, and that is the point: the deck allocates
/// every new desk its own workspace under `~/.claude/agent-bus/workspaces/`, so
/// `cwd` is not something the owner has to know, pick, or be refused for.
/// `engine` and `reportsTo` are optional, are not on the front screen, and the
/// deck defaults both.
public struct InterviewDraft: Equatable, Sendable {
    /// What you want this one for, in your own words.
    public var roleHint: String
    public var engine: String?
    public var reportsTo: String?

    public init(roleHint: String, engine: String? = nil, reportsTo: String? = nil) {
        self.roleHint = roleHint
        self.engine = engine
        self.reportsTo = reportsTo
    }

    /// The only thing that can block a send: an empty message. Refused here
    /// rather than sent, because the deck's answer would be the same and the
    /// round trip only delays it. `nil` means it is ready to send.
    public var problem: String? {
        if roleHint.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Say what you want this one for — that is the whole question."
        }
        return nil
    }

    /// The body of `POST /v1/agents/interview`, keyed as that route documents.
    public var wireBody: [String: String] {
        var body = ["role_hint": roleHint.trimmingCharacters(in: .whitespacesAndNewlines)]
        if let engine, !engine.isEmpty { body["engine"] = engine }
        if let reportsTo, !reportsTo.isEmpty { body["reports_to"] = reportsTo }
        return body
    }
}

/// What `POST /v1/agents/interview` answers with. The 201 carries more than
/// this (`provisional`, `name`, `thread_id`) but they are redundant with
/// `agent` — see the adapter — and `pretrust` is the one other fact worth
/// keeping: whether the trust dialog was pre-accepted before the window
/// opened.
public struct InterviewOutcome: Equatable, Sendable {
    public var agent: Agent
    /// `nil` for a deck that has not shipped the field yet, or one that came
    /// back ok — either way there is nothing to tell him.
    public var pretrust: PretrustOutcome?

    public init(agent: Agent, pretrust: PretrustOutcome? = nil) {
        self.agent = agent
        self.pretrust = pretrust
    }
}

/// The wire body of the 201, decoded for exactly the two fields above.
struct InterviewResponse: Decodable {
    let agent: Agent
    let pretrust: PretrustOutcome?
}

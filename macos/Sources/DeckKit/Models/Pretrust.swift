import Foundation

/// `pretrust` on the 201 body of `POST /v1/agents/interview` (§11.1) — not yet
/// in `client-api.md` at the time of writing; `server/api.py` is the tiebreak.
///
/// Whether the deck managed to pre-accept Claude Code's workspace-trust
/// dialog before the Terminal window it opens ever draws it. `reason` is the
/// same slug vocabulary `Blocked.reason` uses — both come from the one trust
/// check, just reported at two different moments: this is "before the window
/// opened", `Blocked` is "the desk is still OFFLINE".
public struct PretrustOutcome: Equatable, Sendable, Decodable {
    public let ok: Bool
    public let reason: String

    public init(ok: Bool, reason: String) {
        self.ok = ok
        self.reason = reason
    }

    /// `nil` when `ok` — there is nothing to tell him. Otherwise the one
    /// sentence the new chat thread shows, at the point he is already
    /// looking, rather than leaving him to find it later in the settings
    /// panel. Never the raw slug: unlike `Blocked` this response carries no
    /// `detail` to fall back to, so an unrecognised reason goes straight to
    /// the same plain sentence `Blocked` uses as its own last resort.
    public var problemSentence: String? {
        guard !ok else { return nil }
        if let known = BlockedReason(rawValue: reason) { return known.sentence }
        return Blocked.fallbackSentence
    }
}

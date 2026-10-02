import Foundation

/// Why a stuck desk is stuck, and what a person can do about it.
///
/// `not_deck_workspace`, `engine_not_covered`, `lock_busy`, `unexpected_shape`
/// and `write_failed` are the five start-up-gate slugs (a desk that never
/// started; see `server/pretrust.py` on the deck side). `dialog_unrelayed` is
/// different in kind: a desk that **started and then froze**, seated on a
/// permission dialog the deck has no live question for — see the note on
/// `Blocked` below for the rule about when each can arrive. A build of this
/// app may be older than the deck it talks to, so an unrecognised slug is
/// never shown as itself — see `sentence`.
public enum BlockedReason: String, Equatable, Sendable, CaseIterable {
    case notDeckWorkspace = "not_deck_workspace"
    case engineNotCovered = "engine_not_covered"
    case lockBusy = "lock_busy"
    case unexpectedShape = "unexpected_shape"
    case writeFailed = "write_failed"
    case dialogUnrelayed = "dialog_unrelayed"

    /// One sentence, written to be read once and acted on — never the slug,
    /// never raw error text.
    public var sentence: String {
        switch self {
        case .notDeckWorkspace:
            return "This one's folder wasn't set up by the deck itself, so Claude Code never pre-trusted it — its window is waiting on the \u{201c}trust this folder?\u{201d} prompt. Open it and accept."
        case .engineNotCovered:
            return "Its window has a prompt on screen the deck doesn't know how to pre-answer — open it and answer whatever it's asking."
        case .lockBusy:
            return "Another Claude Code session was writing to the shared config at that moment, so this one's trust setting never got set — open its window and accept the trust prompt, or try starting it again."
        case .unexpectedShape:
            return "Claude Code's config file wasn't shaped the way the deck expected, so it backed off rather than risk breaking it — open the window and accept the trust prompt yourself."
        case .writeFailed:
            return "The deck tried to mark the workspace trusted and the write failed — open the window and accept the trust prompt yourself."
        case .dialogUnrelayed:
            return "This one is seated and running, but it's sitting on a permission dialog in its own window that never reached the deck — nothing here can answer it for you. Open that session's window and answer the dialog yourself."
        }
    }
}

/// A live `agent_state` frame's `blocked` field (`client-api.md` §8),
/// distinguishing the three things the wire can say — which a plain
/// `Blocked?` cannot, because it only has room for two.
///
/// - `.unspecified` — the frame carried no `blocked` key at all. Either an
///   older deck that has not shipped the field, or (once other frame types
///   start diffing it) a frame class that never carries it. This is **not**
///   a retraction: the contract's own instinct, confirmed by §8's "additive,
///   not a shape change", is that "this frame does not tell me" must leave
///   whatever the client already believes alone.
/// - `.cleared` — the key was present and explicitly `null`. This *is* the
///   retraction: the deck is telling the client, on this tick, "not blocked
///   any more" — dialog answered, session reseated. The badge must clear.
/// - `.value` — the key was present with an object. The desk is stuck; render
///   it, whether or not `state` also moved on this same frame (§8: the frame
///   fires on `state` **or** `blocked` changing).
public enum BlockedUpdate: Equatable, Hashable, Sendable {
    case unspecified
    case cleared
    case value(Blocked)
}

/// `blocked` on an agent row (§3) and the settings panel (§4).
///
/// **Contract, as of the 2026-09-01 revision of `client-api.md` §3.1 — read
/// that doc, not this comment, if they ever disagree.** `blocked` is non-`nil`
/// in **two** cases, not one:
///   - while `state == .offline`, for every reason except `.dialogUnrelayed`
///     (a desk that never started);
///   - while the desk is **seated and frozen**, for `.dialogUnrelayed` only
///     (a desk that started and then stopped on a dialog nothing relayed).
///
/// Do **not** gate rendering of this field on `state`. It used to be true that
/// `blocked` only ever travelled with `state == .offline`; a client written to
/// that stale rule silently drops `dialog_unrelayed` on the floor, which is
/// the exact stall it most needs to surface. Render `blocked` whenever it is
/// non-`nil`, full stop.
///
/// Either way it clears itself — the deck does not need telling and this app
/// does not cache it across a state change.
public struct Blocked: Equatable, Hashable, Sendable, Decodable {
    /// The deck's own framing of what is stuck. Carried, but the row and the
    /// panel prefer `sentence` — this is not shown verbatim anywhere.
    public let what: String
    /// A stable slug. Never shown as itself — see `sentence`.
    public let reason: String
    /// The deck's prose for this instance. The fallback when `reason` is not
    /// one this build recognises.
    public let detail: String
    public let at: Date?

    enum CodingKeys: String, CodingKey { case what, reason, detail, at }

    public init(what: String, reason: String, detail: String = "", at: Date? = nil) {
        self.what = what
        self.reason = reason
        self.detail = detail
        self.at = at
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        what = try container.decodeIfPresent(String.self, forKey: .what) ?? ""
        reason = try container.decodeIfPresent(String.self, forKey: .reason) ?? ""
        detail = try container.decodeIfPresent(String.self, forKey: .detail) ?? ""
        // Epoch seconds as a float, same convention as every other timestamp
        // on this wire.
        at = try container.decodeIfPresent(Double.self, forKey: .at)
            .map(Date.init(timeIntervalSince1970:))
    }

    /// The badge and the panel's one sentence. Never the raw slug: a `reason`
    /// this build does not recognise falls back to the deck's own `detail`,
    /// and only when that is empty too does it fall back further.
    public var sentence: String {
        if let known = BlockedReason(rawValue: reason) { return known.sentence }
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return trimmed }
        return Blocked.fallbackSentence
    }

    /// Said when neither a recognised reason nor a `detail` gives anything to
    /// say — still honest about the one fact that matters: it needs a look.
    public static let fallbackSentence =
        "The deck could not start this one — its window needs a look."

    /// The short word shown in the sidebar row, next to (never instead of)
    /// the fuller sentence in the panel.
    public static let badgeText = "Needs a look"
}

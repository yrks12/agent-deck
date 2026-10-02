import Foundation

/// Every refusal the deck makes is `{"ok": false, "reason": "<slug>", "detail":
/// "<prose>"}`. The contract is blunt that a client must branch on `reason` and
/// never on the status alone — 401 and 503 in particular mean opposite things
/// to the person holding the phone.
public enum DeckError: Error, Equatable, Sendable {
    /// 503 `auth_not_configured`. The *deck* has no token set. There is nothing
    /// for the user to type; someone has to configure the server.
    case authNotConfigured
    /// 401 `unauthorized`. There is a token and the deck does not accept it.
    case unauthorized
    /// Client-side: nothing in this Mac's Keychain yet.
    case missingToken
    case badCursor
    case emptyText
    case unknownAgent
    case unknownThread
    case unknownMessage
    case threadIsReadOnly
    case reportsToIsNotASetting
    case notADesk
    case queueFailed
    /// A refused `POST /v1/agents`. The reason decides the sentence and the
    /// deck's own detail is kept: "there are already 8 agents running" is the
    /// half of the message that tells you what to do about it.
    case createRefused(CreateAgentRefusal, detail: String)
    /// §12 refusals. None of these is a success, and each sends the reader
    /// somewhere different.
    case unknownAsk
    case alreadyAnswered(detail: String)
    case approvalExpired
    case alwaysNotAvailable(detail: String)
    case badReply
    /// §13 refusals.
    case unknownRoutine
    case nextRunAtIsComputed
    case badCron(detail: String)
    /// A refused `/v1/store/*` call (Connectors & Skills).
    case storeRefused(StoreRefusal, detail: String)
    case transport(String)
    case decoding(String)
    case http(Int, reason: String)

    /// Builds the right case from a status and the refusal body.
    public init(status: Int, body: Data) {
        struct Envelope: Decodable { let reason: String?; let detail: String? }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body)
        let reason = envelope?.reason ?? ""
        let detail = envelope?.detail ?? ""

        if let refusal = StoreRefusal(rawValue: reason) {
            self = .storeRefused(refusal, detail: detail)
            return
        }

        if let refusal = CreateAgentRefusal(rawValue: reason) {
            self = .createRefused(refusal, detail: detail)
            return
        }

        switch reason {
        case "unknown_ask": self = .unknownAsk; return
        case "already_answered": self = .alreadyAnswered(detail: detail); return
        case "expired": self = .approvalExpired; return
        case "always_not_available": self = .alwaysNotAvailable(detail: detail); return
        case "bad_reply": self = .badReply; return
        case "unknown_routine": self = .unknownRoutine; return
        case "next_run_at_is_computed": self = .nextRunAtIsComputed; return
        case "bad_cron": self = .badCron(detail: detail); return
        default: break
        }

        switch reason {
        case "auth_not_configured": self = .authNotConfigured
        case "unauthorized": self = .unauthorized
        case "bad_cursor": self = .badCursor
        case "empty_text": self = .emptyText
        case "unknown_agent": self = .unknownAgent
        case "unknown_thread": self = .unknownThread
        case "unknown_message": self = .unknownMessage
        case "thread_is_read_only": self = .threadIsReadOnly
        case "reports_to_is_not_a_setting": self = .reportsToIsNotASetting
        case "not_a_desk": self = .notADesk
        case "queue_failed": self = .queueFailed
        default:
            // No usable reason. Fall back on the status, but keep whatever slug
            // arrived so a newer refusal is still reportable.
            switch status {
            case 401, 403: self = .unauthorized
            case 503: self = .authNotConfigured
            default: self = .http(status, reason: reason)
            }
        }
    }

    /// What the user is actually told. "Closed until a token is set on the
    /// server" and "your token is wrong" send a person to two different places,
    /// so they never share wording.
    public var userFacingText: String {
        switch self {
        case .authNotConfigured:
            return "This deck is closed until its own API token is set on the server. There is no token for you to enter yet."
        case .unauthorized:
            return "The deck rejected this token. Set a different one in Settings."
        case .missingToken:
            return "Add your deck API token in Settings."
        case .badCursor:
            return "The deck did not recognise this position in the conversation. Reopening the thread will fix it."
        case .emptyText:
            return "There was nothing to send."
        case .unknownAgent:
            return "That agent is no longer on this deck."
        case .unknownThread:
            return "That conversation is no longer on this deck."
        case .unknownMessage:
            return "That message is no longer in this agent's threads."
        case .threadIsReadOnly:
            return "This is a transcript of two agents talking. Open the agent's own chat to say something."
        case .reportsToIsNotASetting:
            return "Who a desk reports to is the org chart, not a setting. Change it on the roster."
        case .notADesk:
            return "This is a live session with no roster desk, so its title and description cannot be set."
        case .queueFailed:
            return "The deck could not queue that message. Try again."
        case .createRefused(let refusal, let detail):
            return refusal.text(detail: detail)
        case .unknownAsk:
            return "That question is no longer on this deck. Nothing was granted."
        case .alreadyAnswered(let detail):
            let how = detail.isEmpty ? "" : " (\(detail))"
            return "This was already answered somewhere else\(how). Your tap changed nothing."
        case .approvalExpired:
            return "This question expired before it was answered, so nothing was granted or refused. The agent will ask again when it needs to."
        case .alwaysNotAvailable(let detail):
            let why = detail.isEmpty ? "" : " — \(detail)"
            return "This one asks every time\(why). Answer it once, or deny it."
        case .badReply:
            return "The deck did not understand that answer. Nothing was granted."
        case .unknownRoutine:
            return "That routine is no longer on this deck."
        case .nextRunAtIsComputed:
            return "The deck works out when a routine next runs; this app must not send one. Nothing was saved."
        case .badCron(let detail):
            return detail.isEmpty
                ? "That schedule could never run, so it was not saved."
                : "That schedule could never run, so it was not saved: \(detail)"
        case .storeRefused(let refusal, let detail):
            return refusal.text(detail: detail)
        case .transport(let detail):
            return "Could not reach the deck: \(detail)"
        case .decoding(let detail):
            return "The deck sent something this app could not read: \(detail)"
        case .http(let code, let reason):
            return reason.isEmpty ? "The deck answered \(code)." : "The deck refused: \(reason)."
        }
    }

    /// Retrying will not help for these; the UI offers a different action.
    public var isRetryable: Bool {
        switch self {
        case .authNotConfigured, .unauthorized, .missingToken,
             .threadIsReadOnly, .reportsToIsNotASetting, .notADesk,
             .unknownAgent, .unknownThread, .unknownMessage,
             // Retrying an identical form gets an identical refusal; the fix
             // is always an edit to the form.
             .createRefused,
             // A settled or expired question does not become live again, and a
             // reply the deck will not take here will not be taken next time.
             .unknownAsk, .alreadyAnswered, .approvalExpired, .alwaysNotAvailable, .badReply,
             .unknownRoutine, .nextRunAtIsComputed, .badCron:
            return false
        case .storeRefused(let refusal, _):
            return refusal == .fetchFailed
        default:
            return true
        }
    }
}

/// One decoder and one encoder for the whole app.
///
/// No date strategy is set on purpose: every timestamp on this wire is an epoch
/// second as a JSON float under a different key name (`ts`, `last_ts`,
/// `last_activity_at`, `created_at`), and each model converts its own. A global
/// strategy would silently mis-handle the ones that are `null`.
public enum DeckCoding {
    public static let decoder = JSONDecoder()
    public static let encoder = JSONEncoder()
}

import Foundation

// Signing in to a Claude account from the app (the deck runs `claude auth login`
// for us). Contract, from the backend builder:
//   POST /v1/accounts/login {id,label}          -> {login_id, url}
//   POST /v1/accounts/login/{login_id}/code {code}
//   GET  /v1/accounts/login/{login_id}          -> {status: waiting_code|done|failed|expired}

public struct AccountLoginStart: Decodable, Equatable, Sendable {
    public var loginID: String
    public var url: URL

    enum CodingKeys: String, CodingKey { case loginID = "login_id", url }

    public init(loginID: String, url: URL) {
        self.loginID = loginID
        self.url = url
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        loginID = try c.decode(String.self, forKey: .loginID)
        let text = try c.decode(String.self, forKey: .url)
        guard let parsed = URL(string: text), parsed.scheme?.hasPrefix("http") == true else {
            throw DecodingError.dataCorruptedError(forKey: .url, in: c, debugDescription: "not a web address")
        }
        url = parsed
    }
}

public enum AccountLoginPhase: String, Sendable {
    case waitingCode = "waiting_code"
    case done, failed, expired
}

public struct AccountLoginStatus: Decodable, Equatable, Sendable {
    public var status: AccountLoginPhase
    public var detail: String?

    enum CodingKeys: String, CodingKey { case status, detail }

    public init(status: AccountLoginPhase, detail: String? = nil) {
        self.status = status
        self.detail = detail
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // A state this client does not know is a failure, never "keep waiting".
        status = AccountLoginPhase(rawValue: (try? c.decode(String.self, forKey: .status)) ?? "") ?? .failed
        detail = try? c.decodeIfPresent(String.self, forKey: .detail)
    }
}

/// Why a login call was refused. The reason decides; the status breaks a tie.
/// The reason names are ASSUMED (the contract lists none).
public enum AccountLoginError: Error, Equatable, Sendable {
    /// A bare 404 on the start route: this deck has no sign-in.
    case unsupported
    case badCode(detail: String)
    case expired
    case failed(detail: String)
    case alreadyExists(detail: String)
    case other(DeckError)

    public enum Call: Sendable { case start, followUp }

    init(status: Int, body: Data, call: Call) {
        struct Envelope: Decodable { let reason: String?; let detail: String? }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body)
        let detail = envelope?.detail ?? ""
        switch envelope?.reason ?? "" {
        case "bad_code", "invalid_code": self = .badCode(detail: detail)
        case "expired", "login_expired", "unknown_login": self = .expired
        case "account_exists": self = .alreadyExists(detail: detail)
        case "login_failed": self = .failed(detail: detail)
        case "":
            switch (status, call) {
            case (404, .start): self = .unsupported
            case (404, .followUp), (410, _): self = .expired
            case (400, .followUp), (422, _): self = .badCode(detail: detail)
            case (502, _): self = .failed(detail: detail)
            default: self = .other(DeckError(status: status, body: body))
            }
        default: self = .other(DeckError(status: status, body: body))
        }
    }

    public var userFacingText: String {
        switch self {
        case .unsupported: return "This deck cannot sign in to accounts yet."
        case .badCode(let d): return d.isEmpty ? "That code was not accepted. Copy it again from Claude." : d
        case .expired: return "This sign-in ran out of time. Start again."
        case .failed(let d): return "The sign-in did not work." + (d.isEmpty ? "" : " \(d)")
        case .alreadyExists(let d): return d.isEmpty ? "An account with that name already exists." : d
        case .other(let e): return e.userFacingText
        }
    }
}

/// A client that can sign an account in. Separate from `DeckClient` so a deck
/// without the routes is a state, and no conformer inherits a silent default.
public protocol AccountLoginClient: Sendable {
    func startLogin(id: String, label: String) async throws -> AccountLoginStart
    func submitLoginCode(loginID: String, code: String) async throws
    func loginStatus(loginID: String) async throws -> AccountLoginStatus
}

/// Small decisions about the buttons, shared by both apps.
public enum AccountSignIn {
    /// A dashed slug of the label, never an id already taken; `nil` when the
    /// label has nothing usable in it.
    public static func newID(for label: String, existing: [String]) -> String? {
        var slug = ""
        for scalar in label.lowercased().unicodeScalars {
            if scalar.isASCII, CharacterSet.alphanumerics.contains(scalar) { slug.unicodeScalars.append(scalar) }
            else if !slug.isEmpty, slug.last != "-", CharacterSet.whitespaces.contains(scalar) || scalar == "-" || scalar == "_" {
                slug.append("-")
            }
        }
        slug = slug.trimmingCharacters(in: CharacterSet(charactersIn: "-"))
        guard !slug.isEmpty else { return nil }
        guard existing.contains(slug) else { return slug }
        var n = 2
        while existing.contains("\(slug)-\(n)") { n += 1 }
        return "\(slug)-\(n)"
    }

    /// Whether the deck serves accounts at all (it sent accounts or a policy).
    /// Absent, the sign-in buttons are not drawn. ASSUMED proxy: the contract
    /// has no capability flag for the login routes themselves.
    public static func isOffered(usage: ClaudeUsage?) -> Bool {
        guard let usage else { return false }
        return !usage.accounts.isEmpty || usage.policy != nil
    }
}

/// The sign-in flow as a state machine, so the Mac and the iPhone cannot
/// disagree about it: start -> browser -> paste the code -> poll -> done.
@MainActor
public final class AccountLoginModel: ObservableObject {
    public enum State: Equatable {
        case idle
        case starting
        /// The browser is open; `problem` is why the last code did not do.
        case awaitingCode(problem: String?)
        case verifying
        case done
        case failed(String)
        case expired
    }

    @Published public private(set) var state: State = .idle
    public let id: String
    public let label: String
    public private(set) var url: URL?

    private let client: AccountLoginClient
    private let openURL: (URL) -> Void
    private let onDone: () -> Void
    private let sleep: () async -> Void
    private let maxPolls: Int
    private var loginID: String?

    public init(client: AccountLoginClient, id: String, label: String,
                openURL: @escaping (URL) -> Void, onDone: @escaping () -> Void,
                sleep: @escaping () async -> Void = { try? await Task.sleep(nanoseconds: 1_000_000_000) },
                maxPolls: Int = 45) {
        self.client = client
        self.id = id
        self.label = label
        self.openURL = openURL
        self.onDone = onDone
        self.sleep = sleep
        self.maxPolls = maxPolls
    }

    /// Asks the deck for a login and opens the browser on its address.
    public func start() async {
        if state == .starting || state == .verifying { return }
        state = .starting
        do {
            let started = try await client.startLogin(id: id, label: label)
            loginID = started.loginID
            url = started.url
            openURL(started.url)
            state = .awaitingCode(problem: nil)
        } catch {
            state = .failed(Self.sentence(error))
        }
    }

    public func retry() async { await start() }

    /// The browser again, for a window he closed.
    public func reopenBrowser() { if let url { openURL(url) } }

    public func submit(code: String) async {
        guard case .awaitingCode = state, let loginID else { return }
        let trimmed = code.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            state = .awaitingCode(problem: "Paste the code Claude shows you first.")
            return
        }
        state = .verifying
        do {
            try await client.submitLoginCode(loginID: loginID, code: trimmed)
        } catch AccountLoginError.badCode(let detail) {
            state = .awaitingCode(problem: AccountLoginError.badCode(detail: detail).userFacingText)
            return
        } catch AccountLoginError.expired {
            state = .expired
            return
        } catch {
            state = .failed(Self.sentence(error))
            return
        }
        for attempt in 0..<maxPolls {
            do {
                let status = try await client.loginStatus(loginID: loginID)
                switch status.status {
                case .done:
                    state = .done
                    onDone()
                    return
                case .failed:
                    state = .failed(AccountLoginError.failed(detail: status.detail ?? "").userFacingText)
                    return
                case .expired:
                    state = .expired
                    return
                case .waitingCode:
                    if attempt < maxPolls - 1 { await sleep() }
                }
            } catch AccountLoginError.expired {
                state = .expired
                return
            } catch {
                state = .failed(Self.sentence(error))
                return
            }
        }
        state = .failed("Claude did not confirm the sign-in in time. Try again.")
    }

    private static func sentence(_ error: Error) -> String {
        if let e = error as? AccountLoginError { return e.userFacingText }
        if let e = error as? DeckError { return e.userFacingText }
        return DeckError.transport(error.localizedDescription).userFacingText
    }
}

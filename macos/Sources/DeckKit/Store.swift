import Foundation

// MARK: - Connectors & Skills: the wire (`docs/connectors.md`)
//
// `kind`, `trust`, `auth`, `source` and `Reload.action` are kept as strings on
// purpose. Each is an open set on the server, and a closed enum here would turn
// a newer deck's perfectly good answer into a decoding error on screen. The
// decisions that read them (`StoreBrowser`) compare against the values the
// contract names and treat anything else as the cautious case.

/// One field an `api_key` item needs before it can be installed.
public struct StoreSecretField: Codable, Hashable, Sendable {
    public var name: String
    public var label: String
    public var description: String
    public var required: Bool

    public init(name: String, label: String, description: String, required: Bool) {
        self.name = name
        self.label = label
        self.description = description
        self.required = required
    }

    enum CodingKeys: String, CodingKey { case name, label, description, required }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        name = try c.decode(String.self, forKey: .name)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? name
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        required = try c.decodeIfPresent(Bool.self, forKey: .required) ?? true
    }
}

/// A connector (an MCP server) or a skill (a `SKILL.md` folder).
public struct StoreItem: Decodable, Hashable, Sendable, Identifiable {
    public var id: String
    /// `connector` | `skill`.
    public var kind: String
    public var name: String
    public var title: String
    public var description: String
    public var publisher: String
    public var source: String
    public var repo: String?
    public var stars: Int?
    /// ISO 8601, as the deck sent it. `updatedDate` is the parsed form.
    public var updatedAt: String?
    /// `official` | `verified` | `unverified`.
    public var trust: String
    public var trustNote: String
    public var version: String?
    /// `none` | `api_key` | `oauth` | `unknown`.
    public var auth: String
    public var secrets: [StoreSecretField]
    public var installable: Bool
    public var whyNot: String?
    public var installedOn: [String]
    /// Only on `GET /v1/store/item`, and only for skills.
    public var readme: String?

    public init(id: String, kind: String, name: String, title: String, description: String,
                publisher: String, source: String, repo: String?, stars: Int?,
                updatedAt: String?, trust: String, trustNote: String, version: String?,
                auth: String, secrets: [StoreSecretField], installable: Bool,
                whyNot: String?, installedOn: [String], readme: String?) {
        self.id = id
        self.kind = kind
        self.name = name
        self.title = title
        self.description = description
        self.publisher = publisher
        self.source = source
        self.repo = repo
        self.stars = stars
        self.updatedAt = updatedAt
        self.trust = trust
        self.trustNote = trustNote
        self.version = version
        self.auth = auth
        self.secrets = secrets
        self.installable = installable
        self.whyNot = whyNot
        self.installedOn = installedOn
        self.readme = readme
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, name, title, description, publisher, source, repo, stars
        case trust, version, auth, secrets, installable, readme
        case updatedAt = "updated_at"
        case trustNote = "trust_note"
        case whyNot = "why_not"
        case installedOn = "installed_on"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        kind = try c.decode(String.self, forKey: .kind)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? id
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? name
        description = try c.decodeIfPresent(String.self, forKey: .description) ?? ""
        publisher = try c.decodeIfPresent(String.self, forKey: .publisher) ?? ""
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? ""
        repo = try c.decodeIfPresent(String.self, forKey: .repo)
        stars = try c.decodeIfPresent(Int.self, forKey: .stars)
        updatedAt = try c.decodeIfPresent(String.self, forKey: .updatedAt)
        // Missing trust is the cautious answer, never a badge.
        trust = try c.decodeIfPresent(String.self, forKey: .trust) ?? "unverified"
        trustNote = try c.decodeIfPresent(String.self, forKey: .trustNote) ?? ""
        version = try c.decodeIfPresent(String.self, forKey: .version)
        auth = try c.decodeIfPresent(String.self, forKey: .auth) ?? "unknown"
        secrets = try c.decodeIfPresent([StoreSecretField].self, forKey: .secrets) ?? []
        installable = try c.decodeIfPresent(Bool.self, forKey: .installable) ?? false
        whyNot = try c.decodeIfPresent(String.self, forKey: .whyNot)
        installedOn = try c.decodeIfPresent([String].self, forKey: .installedOn) ?? []
        readme = try c.decodeIfPresent(String.self, forKey: .readme)
    }

    public var updatedDate: Date? { updatedAt.flatMap(Self.parseDate) }

    /// ISO 8601 with or without fractional seconds.
    public static func parseDate(_ iso: String) -> Date? {
        let plain = ISO8601DateFormatter()
        if let date = plain.date(from: iso) { return date }
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return fractional.date(from: iso)
    }
}

/// `GET /v1/store/catalog`.
public struct StoreCatalogPage: Decodable, Hashable, Sendable {
    public var items: [StoreItem]
    public var total: Int
    public var refreshedAt: String?
    /// The last refresh failed and this is the last good copy.
    public var stale: Bool

    public init(items: [StoreItem], total: Int, refreshedAt: String? = nil, stale: Bool = false) {
        self.items = items
        self.total = total
        self.refreshedAt = refreshedAt
        self.stale = stale
    }

    enum CodingKeys: String, CodingKey {
        case items, total, stale
        case refreshedAt = "refreshed_at"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        items = try c.decodeIfPresent([StoreItem].self, forKey: .items) ?? []
        total = try c.decodeIfPresent(Int.self, forKey: .total) ?? items.count
        refreshedAt = try c.decodeIfPresent(String.self, forKey: .refreshedAt)
        stale = try c.decodeIfPresent(Bool.self, forKey: .stale) ?? false
    }
}

/// One item on one desk.
public struct StoreInstalled: Decodable, Hashable, Sendable, Identifiable {
    public var id: String
    public var kind: String
    public var name: String
    public var title: String
    public var desk: String
    public var version: String?
    public var commit: String?
    public var installedAt: String
    public var trust: String
    public var updateAvailable: Bool

    public init(id: String, kind: String, name: String, title: String, desk: String,
                version: String?, commit: String?, installedAt: String, trust: String,
                updateAvailable: Bool) {
        self.id = id
        self.kind = kind
        self.name = name
        self.title = title
        self.desk = desk
        self.version = version
        self.commit = commit
        self.installedAt = installedAt
        self.trust = trust
        self.updateAvailable = updateAvailable
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, name, title, desk, version, commit, trust
        case installedAt = "installed_at"
        case updateAvailable = "update_available"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        kind = try c.decodeIfPresent(String.self, forKey: .kind) ?? ""
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? id
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? name
        desk = try c.decode(String.self, forKey: .desk)
        version = try c.decodeIfPresent(String.self, forKey: .version)
        commit = try c.decodeIfPresent(String.self, forKey: .commit)
        installedAt = try c.decodeIfPresent(String.self, forKey: .installedAt) ?? ""
        trust = try c.decodeIfPresent(String.self, forKey: .trust) ?? "unverified"
        updateAvailable = try c.decodeIfPresent(Bool.self, forKey: .updateAvailable) ?? false
    }
}

public struct StoreDeskInstalls: Decodable, Hashable, Sendable {
    public var desk: String
    public var items: [StoreInstalled]

    public init(desk: String, items: [StoreInstalled]) {
        self.desk = desk
        self.items = items
    }
}

/// `GET /v1/store/installed`.
public struct StoreInstalledPage: Decodable, Hashable, Sendable {
    public var desks: [StoreDeskInstalls]
    public init(desks: [StoreDeskInstalls]) { self.desks = desks }
}

/// How one desk picks up a change. `detail` is the deck's own sentence.
public struct StoreReload: Decodable, Hashable, Sendable {
    public var desk: String
    /// Open set: `after_turn`, `restart_after_turn`, `next_wake`, `restarting`, `none`, …
    public var action: String
    public var detail: String

    public init(desk: String, action: String, detail: String) {
        self.desk = desk
        self.action = action
        self.detail = detail
    }

    enum CodingKeys: String, CodingKey { case desk, action, detail }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        desk = try c.decode(String.self, forKey: .desk)
        action = try c.decodeIfPresent(String.self, forKey: .action) ?? "none"
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }
}

/// `POST /v1/store/install` and `/update`.
public struct StoreInstallResult: Decodable, Hashable, Sendable {
    public var item: StoreItem
    public var installed: [StoreInstalled]
    public var reload: [StoreReload]

    public init(item: StoreItem, installed: [StoreInstalled], reload: [StoreReload]) {
        self.item = item
        self.installed = installed
        self.reload = reload
    }
}

public struct StoreRemoved: Decodable, Hashable, Sendable {
    public var desk: String
    public var id: String
    public init(desk: String, id: String) {
        self.desk = desk
        self.id = id
    }
}

/// `POST /v1/store/uninstall`.
public struct StoreUninstallResult: Decodable, Hashable, Sendable {
    public var removed: [StoreRemoved]
    public var reload: [StoreReload]
    public init(removed: [StoreRemoved], reload: [StoreReload]) {
        self.removed = removed
        self.reload = reload
    }
}

/// `trust=` on the catalog.
public enum StoreTrustScope: String, Sendable, Hashable {
    case trusted
    case all
}

/// Where an install lands: every desk, or the ones named.
public enum StoreDesks: Hashable, Sendable, Encodable {
    case all
    case desks([String])

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .all: try c.encode("all")
        case .desks(let names): try c.encode(names)
        }
    }
}

/// The body of `POST /v1/store/install`. Carries typed keys, so it describes
/// itself without them: a log line, `dump` or `String(reflecting:)` names the
/// fields and never the values.
public struct StoreInstallRequest: Hashable, Sendable, Encodable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var id: String
    public var desks: StoreDesks
    public var secrets: [String: String]
    public var acceptUnverified: Bool

    public init(id: String, desks: StoreDesks, secrets: [String: String] = [:],
                acceptUnverified: Bool = false) {
        self.id = id
        self.desks = desks
        self.secrets = secrets
        self.acceptUnverified = acceptUnverified
    }

    enum CodingKeys: String, CodingKey {
        case id, desks, secrets
        case acceptUnverified = "accept_unverified"
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id)
        try c.encode(desks, forKey: .desks)
        if !secrets.isEmpty { try c.encode(secrets, forKey: .secrets) }
        // Only ever an explicit yes; absent means no.
        if acceptUnverified { try c.encode(true, forKey: .acceptUnverified) }
    }

    public var description: String {
        let fields = secrets.keys.sorted().joined(separator: ",")
        return "StoreInstallRequest(id: \(id), desks: \(desks), secrets: [\(fields)] <redacted>, "
            + "acceptUnverified: \(acceptUnverified))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: [
            "id": id, "desks": desks,
            "secrets": secrets.keys.sorted().map { "\($0)=<redacted>" },
            "acceptUnverified": acceptUnverified,
        ])
    }
}

/// A refused `/v1/store/*` call, by `reason`.
public enum StoreRefusal: String, Sendable, Hashable, CaseIterable {
    case unknownItem = "unknown_item"
    case unknownDesk = "unknown_desk"
    case notInstalled = "not_installed"
    case unverified
    case missingSecret = "missing_secret"
    case needsOAuth = "needs_oauth"
    case notInstallable = "not_installable"
    case fetchFailed = "fetch_failed"
    case badInput = "bad_input"
    // OAuth Connect, and the install paths behind it. The deck's own detail
    // is the sentence for these: it already says what to do.
    case badState = "bad_state"
    case oauthDenied = "oauth_denied"
    case oauthUnsupported = "oauth_unsupported"
    case oauthRegisterFailed = "oauth_register_failed"
    case oauthTokenFailed = "oauth_token_failed"
    case installFailed = "install_failed"
    case conflict

    public func text(detail: String) -> String {
        let tail = detail.isEmpty ? "" : ": \(detail)"
        switch self {
        case .unknownItem: return "That item is no longer in the catalog."
        case .unknownDesk: return "That desk is not on this deck's roster\(tail)."
        case .notInstalled: return "That is not installed there any more."
        case .unverified:
            return "This is from an unverified source. Confirm the warning to install it anyway."
        case .missingSecret: return "A required key is missing\(tail)."
        case .needsOAuth: return "This one needs an OAuth sign-in, which the deck cannot do yet."
        case .notInstallable:
            return detail.isEmpty ? "This item cannot be installed from here." : detail
        case .fetchFailed: return "The deck could not download it\(tail). Try again."
        case .badInput: return "The deck did not accept that request\(tail)."
        case .badState:
            return detail.isEmpty ? "That sign-in link is used up or expired; press Connect again." : detail
        case .oauthDenied:
            return detail.isEmpty ? "The sign-in was declined at the provider." : detail
        case .oauthUnsupported, .oauthRegisterFailed, .oauthTokenFailed:
            return detail.isEmpty ? "The provider would not complete the sign-in." : detail
        case .installFailed:
            return detail.isEmpty ? "The deck could not install it." : "The deck could not install it: \(detail)"
        case .conflict:
            return detail.isEmpty ? "Something is already in the way on that desk." : detail
        }
    }
}

/// The `/v1/store/*` routes. A separate protocol, like `DecisionClient`: a
/// transport that predates the store does not conform, and the sidebar row
/// says the store is unavailable rather than opening a panel that cannot load.
public protocol StoreClient: Sendable {
    /// `GET /v1/store/catalog`. `query` is only sent when non-empty.
    func storeCatalog(kind: String?, query: String?, trust: StoreTrustScope,
                      limit: Int, offset: Int) async throws -> StoreCatalogPage
    /// `GET /v1/store/item?id=`
    func storeItem(id: String) async throws -> StoreItem
    /// `POST /v1/store/install`
    func storeInstall(_ request: StoreInstallRequest) async throws -> StoreInstallResult
    /// `POST /v1/store/update`
    func storeUpdate(id: String, desks: StoreDesks) async throws -> StoreInstallResult
    /// `POST /v1/store/uninstall`
    func storeUninstall(id: String, desks: StoreDesks) async throws -> StoreUninstallResult
    /// `GET /v1/store/installed?desk=`
    func storeInstalled(desk: String?) async throws -> StoreInstalledPage
    /// `POST /v1/store/refresh` — true when a refresh was started.
    func storeRefresh() async throws -> Bool
    /// `POST /v1/store/connect` — start an OAuth sign-in for these desks.
    func storeConnect(id: String, desks: StoreDesks) async throws -> StoreConnectStart
    /// `POST /v1/store/connect/complete {"callback_url"}` — the address the
    /// browser came back to; answers like an install.
    func storeConnectComplete(callbackURL: String) async throws -> StoreInstallResult
    /// `GET /v1/store/connect/status?state=`
    func storeConnectStatus(state: String) async throws -> StoreConnectStatus
}

/// `POST /v1/store/connect`. `state` matches the browser's answer to this
/// sign-in; it is kept for the status poll and never described.
public struct StoreConnectStart: Decodable, Hashable, Sendable,
    CustomStringConvertible, CustomDebugStringConvertible, CustomReflectable {
    public var authorizeURL: URL
    public var state: String
    public var redirectURI: String
    public var expiresAt: String

    public init(authorizeURL: URL, state: String, redirectURI: String, expiresAt: String) {
        self.authorizeURL = authorizeURL
        self.state = state
        self.redirectURI = redirectURI
        self.expiresAt = expiresAt
    }

    enum CodingKeys: String, CodingKey {
        case state
        case authorizeURL = "authorize_url"
        case redirectURI = "redirect_uri"
        case expiresAt = "expires_at"
    }

    public var expiresDate: Date? { StoreItem.parseDate(expiresAt) }

    public var description: String {
        "StoreConnectStart(provider: \(authorizeURL.host ?? "?"), redirect: \(redirectURI), "
            + "expires: \(expiresAt), state: <redacted>)"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: ["provider": authorizeURL.host ?? "?", "redirectURI": redirectURI,
                                "expiresAt": expiresAt, "state": "<redacted>"])
    }
}

/// Where a Connect stands, as the wait draws it.
public enum StoreConnectPhase: Hashable, Sendable {
    case waiting
    case done
    case failed(String)
    case expired
}

/// `GET /v1/store/connect/status`.
public struct StoreConnectStatus: Decodable, Hashable, Sendable {
    /// `pending` | `done` | `failed` | `expired` — open set.
    public var state: String
    public var id: String?
    public var detail: String

    public init(state: String, id: String?, detail: String) {
        self.state = state
        self.id = id
        self.detail = detail
    }

    enum CodingKeys: String, CodingKey { case state, id, detail }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        state = try c.decodeIfPresent(String.self, forKey: .state) ?? "pending"
        id = try c.decodeIfPresent(String.self, forKey: .id)
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }

    /// An unknown state keeps waiting; the deadline still ends it.
    public var phase: StoreConnectPhase {
        switch state {
        case "done": return .done
        case "failed": return .failed(detail.isEmpty ? "The sign-in did not complete." : detail)
        case "expired": return .expired
        default: return .waiting
        }
    }
}

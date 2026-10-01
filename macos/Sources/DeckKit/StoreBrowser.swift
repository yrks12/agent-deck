import Foundation

/// The three tabs of the Connectors & Skills panel.
public enum StoreTab: String, CaseIterable, Hashable, Sendable, Identifiable {
    case connectors, skills, installed

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .connectors: return "Connectors"
        case .skills: return "Skills"
        case .installed: return "Installed"
        }
    }

    /// The catalog's `kind=`; nil for Installed, which has its own route.
    public var kind: String? {
        switch self {
        case .connectors: return "connector"
        case .skills: return "skill"
        case .installed: return nil
        }
    }
}

/// What the catalog is asked for.
public struct StoreCatalogRequest: Hashable, Sendable {
    public var kind: String
    public var query: String?
    public var trust: StoreTrustScope
}

/// **The store's decisions, with no view in sight.**
///
/// Trusted mode loads each tab's catalog once per open and searches it on the
/// Mac — a keystroke costs nothing. "Other sources" is the opposite: the deck
/// never caches unverified items, so the search itself is the server's and
/// goes with `trust=all`.
public struct StoreBrowser: Hashable, Sendable {
    public var tab: StoreTab
    public var searchText: String
    /// "Other sources (unverified)". Off unless he turns it on.
    public var showsUnverified: Bool

    public init(tab: StoreTab = .connectors, searchText: String = "", showsUnverified: Bool = false) {
        self.tab = tab
        self.searchText = searchText
        self.showsUnverified = showsUnverified
    }

    private var trimmedSearch: String {
        searchText.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// Nil on Installed. In trusted mode the query never travels.
    public var catalogRequest: StoreCatalogRequest? {
        guard let kind = tab.kind else { return nil }
        if showsUnverified {
            let q = trimmedSearch
            return StoreCatalogRequest(kind: kind, query: q.isEmpty ? nil : q, trust: .all)
        }
        return StoreCatalogRequest(kind: kind, query: nil, trust: .trusted)
    }

    /// The cards to draw from what the deck returned for this tab.
    public func visible(_ items: [StoreItem]) -> [StoreItem] {
        let ofKind = items.filter { $0.kind == tab.kind }
        if showsUnverified { return ofKind }
        let trusted = ofKind.filter { Self.isTrusted($0.trust) }
        let needle = trimmedSearch
        guard !needle.isEmpty else { return trusted }
        return trusted.filter { item in
            [item.title, item.name, item.publisher, item.description].contains {
                $0.range(of: needle, options: [.caseInsensitive, .diacriticInsensitive]) != nil
            }
        }
    }

    static func isTrusted(_ trust: String) -> Bool { trust == "official" || trust == "verified" }

    // MARK: what a card says

    /// "Official" only when the deck says `official`, "Verified" only when it
    /// says `verified`; anything else wears nothing. A badge is a claim.
    public static func badge(forTrust trust: String) -> String? {
        switch trust {
        case "official": return "Official"
        case "verified": return "Verified"
        default: return nil
        }
    }

    /// "999", "12.3k", "1.3M".
    public static func starsText(_ stars: Int?) -> String? {
        guard let stars else { return nil }
        if stars < 1_000 { return String(stars) }
        let thousands = (Double(stars) / 100).rounded() / 10
        if thousands < 1_000 { return compact(thousands) + "k" }
        return compact((Double(stars) / 100_000).rounded() / 10) + "M"
    }

    private static func compact(_ value: Double) -> String {
        value == value.rounded() ? String(Int(value)) : String(format: "%.1f", value)
    }

    /// "updated today", "updated 3 weeks ago". Nil when there is no date.
    public static func updatedText(_ iso: String?, now: Date = Date()) -> String? {
        guard let iso, let date = StoreItem.parseDate(iso) else { return nil }
        // Calendar days, not 24-hour blocks: five days ago at 12:00:00.123
        // is "5 days ago" at noon today, not 4.99 of them.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "UTC") ?? .current
        let days = max(0, calendar.dateComponents(
            [.day], from: calendar.startOfDay(for: date), to: calendar.startOfDay(for: now)).day ?? 0)
        func ago(_ n: Int, _ unit: String) -> String {
            "updated \(n) \(unit)\(n == 1 ? "" : "s") ago"
        }
        switch days {
        case 0: return "updated today"
        case 1: return "updated yesterday"
        case 2..<7: return ago(days, "day")
        case 7..<30: return ago(days / 7, "week")
        case 30..<365: return ago(days / 30, "month")
        default: return ago(days / 365, "year")
        }
    }

    /// "atlas — reloads after its current turn": the deck's own sentence.
    public static func reloadLine(_ reload: StoreReload) -> String {
        let detail = reload.detail.trimmingCharacters(in: .whitespacesAndNewlines)
        if !detail.isEmpty { return "\(reload.desk) — \(detail)" }
        if reload.action == "none" || reload.action.isEmpty {
            return "\(reload.desk) — nothing to reload"
        }
        return "\(reload.desk) — \(reload.action.replacingOccurrences(of: "_", with: " "))"
    }

    /// "1.2.0 · 9f2a15f". A skill's version already is its short sha.
    public static func versionText(_ row: StoreInstalled) -> String {
        let short = row.commit.map { String($0.prefix(7)) }
        switch (row.version, short) {
        case let (version?, commit?):
            if let full = row.commit, full.hasPrefix(version) { return version }
            return "\(version) · \(commit)"
        case let (version?, nil): return version
        case let (nil, commit?): return commit
        case (nil, nil): return ""
        }
    }

    /// The warning an unverified item carries, in the panel and on install.
    public static let unverifiedWarning =
        "Unverified source. Agents run with full access to your server; a malicious skill or connector can do anything they can."
}

/// Where an install goes.
public enum StoreInstallTarget: Hashable, Sendable {
    case allDesks
    case chosen(Set<String>)
}

/// **One install, being filled in.**
///
/// Typed keys live here and nowhere else — in memory, for as long as the
/// detail is open. `takeRequest()` hands them over once and clears them, and
/// nothing that describes a draft ever includes them.
public struct StoreInstallDraft: CustomStringConvertible, CustomDebugStringConvertible,
    CustomReflectable {
    public let item: StoreItem
    public var target: StoreInstallTarget = .allDesks
    /// The explicit "I understand" on an unverified item.
    public var confirmedUnverified = false
    public private(set) var secretValues: [String: String] = [:]

    public init(item: StoreItem) {
        self.item = item
    }

    public var needsUnverifiedConfirm: Bool { !StoreBrowser.isTrusted(item.trust) }

    /// The "OAuth sign-in isn't supported yet" note, with `why_not`.
    public var showsOAuthNote: Bool { item.auth == "oauth" || !item.installable }

    /// Key fields are only asked for on `api_key` items.
    public var secretFields: [StoreSecretField] { item.auth == "api_key" ? item.secrets : [] }

    public func secretValue(for name: String) -> String { secretValues[name] ?? "" }

    public mutating func setSecret(_ value: String, for name: String) {
        secretValues[name] = value
    }

    public mutating func clearSecrets() { secretValues.removeAll() }

    /// Why Install is disabled, in words; nil when it may go.
    public var problem: String? {
        if !item.installable {
            return item.whyNot ?? (item.auth == "oauth"
                ? "OAuth sign-in isn't supported yet."
                : "This item cannot be installed from here.")
        }
        if item.auth == "oauth" { return item.whyNot ?? "OAuth sign-in isn't supported yet." }
        if case .chosen(let desks) = target, desks.isEmpty { return "Choose at least one desk." }
        for field in secretFields where field.required {
            if secretValue(for: field.name).trimmingCharacters(in: .whitespaces).isEmpty {
                return "Enter \(field.label)."
            }
        }
        if needsUnverifiedConfirm && !confirmedUnverified {
            return "Confirm the unverified-source warning first."
        }
        return nil
    }

    public var canInstall: Bool { problem == nil }

    /// The request, once — and the keys leave the draft as it is made.
    public mutating func takeRequest() -> StoreInstallRequest? {
        guard canInstall else { return nil }
        var secrets: [String: String] = [:]
        for field in secretFields {
            let value = secretValue(for: field.name).trimmingCharacters(in: .whitespaces)
            if !value.isEmpty { secrets[field.name] = value }
        }
        clearSecrets()
        let desks: StoreDesks
        switch target {
        case .allDesks: desks = .all
        case .chosen(let names): desks = .desks(names.sorted())
        }
        return StoreInstallRequest(id: item.id, desks: desks, secrets: secrets,
                                   acceptUnverified: needsUnverifiedConfirm && confirmedUnverified)
    }

    public var description: String {
        "StoreInstallDraft(item: \(item.id), target: \(target), "
            + "secrets: \(secretValues.keys.sorted()) <redacted>, confirmedUnverified: \(confirmedUnverified))"
    }

    public var debugDescription: String { description }

    public var customMirror: Mirror {
        Mirror(self, children: [
            "item": item.id, "target": target,
            "secrets": secretValues.keys.sorted().map { "\($0)=<redacted>" },
            "confirmedUnverified": confirmedUnverified,
        ])
    }
}

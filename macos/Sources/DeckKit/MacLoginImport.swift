import Foundation

/// **Use the login he already has on this Mac, instead of a blank browser.**
///
/// Owner, live on the passkey flow: *"sign in with passkey takes me to browser
/// where I'm not logged in."* He expects his existing browser session to carry
/// to the desks. So the primary action reads the requested site's cookies from
/// the browser he is **already signed in with** and shares them; the fresh
/// passkey window stays as the fallback for a site he is not signed into here.
///
/// This file is the pure core: which browser he uses, which of its profiles are
/// live, and — the load-bearing rule — *which cookies are allowed to leave the
/// Mac for a given site*. Only the requested site's cookies, plus that site's
/// known sign-in domains, ever go. Everything here is deterministic and has no
/// filesystem or process of its own, so every rule is tested without a browser.

public enum BrowserFamily: String, Sendable, Hashable {
    case chrome, safari, firefox, other
}

public enum MacBrowsers {
    /// Chromium-family bundle ids share one profile and cookie layout.
    static let chromeFamily: Set<String> = [
        "com.google.chrome", "com.google.chrome.beta", "com.google.chrome.canary",
        "com.brave.browser", "com.microsoft.edgemac", "company.thebrowser.browser", // Arc
        "com.vivaldi.vivaldi", "com.operasoftware.opera",
    ]

    public static func family(forBundleID id: String?) -> BrowserFamily {
        guard let id = id?.lowercased() else { return .other }
        if chromeFamily.contains(id) { return .chrome }
        if id == "com.apple.safari" { return .safari }
        if id.contains("firefox") { return .firefox }
        return .other
    }

    /// A human name for the browser he uses, for the card's button.
    public static func label(forBundleID id: String?) -> String {
        switch id?.lowercased() {
        case "com.google.chrome": return "Chrome"
        case "com.brave.browser": return "Brave"
        case "com.microsoft.edgemac": return "Edge"
        case "company.thebrowser.browser": return "Arc"
        case "com.vivaldi.vivaldi": return "Vivaldi"
        case "com.apple.safari": return "Safari"
        case .some(let x) where x.contains("firefox"): return "Firefox"
        default: return "your browser"
        }
    }
}

/// One Chrome-family profile, as `Local State` describes it. `dir` is the
/// profile directory name ("Default", "Profile 10"), not a path.
public struct ChromeProfile: Hashable, Sendable {
    public let dir: String
    public let name: String
    public let email: String?
    public init(dir: String, name: String, email: String?) {
        self.dir = dir; self.name = name; self.email = email
    }

    /// Every profile named in a `Local State` file's `info_cache`. PURE — the
    /// caller pairs these with cookie-file timestamps to pick the live one, so
    /// no cookie is read here.
    public static func all(fromLocalState data: Data) -> [ChromeProfile] {
        guard let root = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let cache = (root["profile"] as? [String: Any])?["info_cache"] as? [String: Any]
        else { return [] }
        var out: [ChromeProfile] = []
        for (dir, raw) in cache {
            guard let meta = raw as? [String: Any],
                  let name = meta["name"] as? String, !name.isEmpty else { continue }
            let email = (meta["user_name"] as? String).flatMap { $0.isEmpty ? nil : $0 }
            out.append(ChromeProfile(dir: dir, name: name, email: email))
        }
        return out.sorted { $0.dir < $1.dir }
    }
}

/// **Which cookies may leave the Mac for a given site — and no others.**
///
/// The card is raised for one site. Sharing his whole cookie jar would hand
/// every desk every login he has; the owner's ruling is broad *inside* the
/// deck, but what leaves his Mac is scoped to the site that stopped the desk.
/// So a cookie travels only if its domain is the site's registrable domain, or
/// one of that site's known sign-in domains (Google keeps the session on
/// `accounts.google.com`, Microsoft on `login.microsoftonline.com`).
public enum SiteCookies {
    /// A short, deliberately incomplete public-suffix set: the multi-label TLDs
    /// common here, so `bbc.co.uk` is one registrable domain, not `co.uk`.
    static let twoLabelSuffixes: Set<String> = [
        "co.uk", "org.uk", "gov.uk", "ac.uk", "co.il", "com.au", "co.jp", "com.br",
    ]

    /// The registrable ("eTLD+1") domain of a host: `accounts.google.com` and
    /// `www.google.com` both reduce to `google.com`. Leading dots are stripped.
    public static func registrableDomain(_ host: String) -> String {
        let clean = host.hasPrefix(".") ? String(host.dropFirst()) : host
        let parts = clean.lowercased().split(separator: ".").map(String.init)
        guard parts.count > 2 else { return clean.lowercased() }
        let lastTwo = parts.suffix(2).joined(separator: ".")
        let take = twoLabelSuffixes.contains(lastTwo) ? 3 : 2
        return parts.suffix(take).joined(separator: ".")
    }

    /// Sign-in domains for a site whose auth lives on a *different* registrable
    /// domain than the site itself. Same-domain auth (accounts.google.com under
    /// google.com) needs no entry — the registrable-domain rule already keeps
    /// it. Keyed by the site's registrable domain.
    static let authDomainsByRegistrable: [String: Set<String>] = [
        "microsoft.com": ["login.microsoftonline.com", "login.live.com"],
        "office.com": ["login.microsoftonline.com", "login.live.com"],
        "live.com": ["login.live.com", "login.microsoftonline.com"],
        "atlassian.net": ["id.atlassian.com"],
        "slack.com": ["slack.com"],
    ]

    /// Every domain whose cookies are allowed to leave the Mac for `host`: the
    /// site's registrable domain and its cross-domain sign-in hosts.
    public static func allowedDomains(forHost host: String) -> Set<String> {
        let reg = registrableDomain(host)
        var out: Set<String> = [reg]
        out.formUnion(authDomainsByRegistrable[reg] ?? [])
        return out
    }

    /// Does a cookie's `domain` belong to `host`'s allowed set? A cookie domain
    /// matches an allowed domain when it *is* that domain or a subdomain of it
    /// (leading dots ignored) — never a mere suffix, so `notgoogle.com` does
    /// not match `google.com`.
    public static func belongs(cookieDomain: String, toSite host: String) -> Bool {
        let cookie = (cookieDomain.hasPrefix(".") ? String(cookieDomain.dropFirst())
                      : cookieDomain).lowercased()
        for allowed in allowedDomains(forHost: host) {
            if cookie == allowed || cookie.hasSuffix("." + allowed) { return true }
        }
        return false
    }

    /// Keeps only the cookies that may leave the Mac for `host`, from a CDP
    /// cookie array (`Network.getAllCookies`), as JSON in and JSON out. Values
    /// are carried through untouched and never inspected here.
    public static func filterJSON(_ data: Data, toSite host: String) throws -> Data {
        let rows = (try JSONSerialization.jsonObject(with: data)) as? [[String: Any]] ?? []
        let kept = rows.filter { row in
            guard let dom = row["domain"] as? String else { return false }
            return belongs(cookieDomain: dom, toSite: host)
        }
        return try JSONSerialization.data(withJSONObject: kept)
    }

    /// How many of a CDP array's cookies would leave for `host`. For a count in
    /// a report, never the values.
    public static func countJSON(_ data: Data, toSite host: String) -> Int {
        ((try? filterJSON(data, toSite: host))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [[String: Any]] })?.count ?? 0
    }
}

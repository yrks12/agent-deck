import Foundation

/// **Sign in on this Mac, and every desk is signed in.**
///
/// The owner, verbatim: *"i need to be able to login with the passkeys from my
/// mac on their computers"*. His passkeys are in iCloud Keychain — on this Mac
/// and his iPhone — and never in a desk's Linux container, so a passkey-only
/// sign-in dead-ends on a desk's screen.
///
/// So the sign-in happens here: a throwaway Chrome profile opens on this Mac
/// (Chrome on macOS offers iCloud passkeys to any profile, and a phone by QR),
/// he signs in with Touch ID, the app reads that profile's cookies over CDP
/// and hands them to the deck on `POST /v1/logins`, which writes them into
/// every desk's browser and its login vault. Chrome is then closed and the
/// profile deleted, whatever happened.
///
/// Why not the alternatives (measured or documented, see
/// docs/the-agents-computer.md): a `WKWebView` gets no passkeys without the
/// site's associated-domains entitlement; `ASWebAuthenticationSession` never
/// hands its cookies to the app; Safari has no cookie export at all.
public enum MacSignIn {

    /// Where to open, for a sign-in card: the site's front door. Never the
    /// card's own URL — that is one desk's half-finished attempt (a passkey
    /// challenge with a one-time token) and means nothing in a fresh browser.
    /// Only a `login` card with an https address qualifies: a code, a CAPTCHA
    /// or a payment is not a sign-in, and no passkey is offered to plain http.
    public static func url(kind: String, place: String) -> URL? {
        guard kind == "login",
              let parts = URLComponents(string: place.trimmingCharacters(in: .whitespaces)),
              parts.scheme?.lowercased() == "https",
              let host = parts.host, !host.isEmpty
        else { return nil }
        var front = URLComponents()
        front.scheme = "https"
        front.host = host
        front.port = parts.port
        front.path = "/"
        return front.url
    }

    /// The primary button on a sign-in card. Google, YouTube and Gmail sessions
    /// in his everyday Chrome are Device Bound (MEASURED, PR #164): the cookies
    /// copy over and Google rejects them on a desk, so those hosts lead with the
    /// fresh passkey sign-in. Everything else keeps his Chrome login first.
    public enum Primary: Equatable, Sendable { case chromeLogin, freshPasskey }

    public static func isDeviceBound(host: String) -> Bool {
        let h = host.lowercased()
        return ["google.com", "youtube.com", "gmail.com", "googleusercontent.com"]
            .contains { h == $0 || h.hasSuffix("." + $0) }
    }

    public static func primary(host: String, hasBrowser: Bool) -> Primary {
        (hasBrowser && !isDeviceBound(host: host)) ? .chromeLogin : .freshPasskey
    }

    /// The one-line reason "Use my Chrome login" is demoted, or nil when it leads.
    public static func chromeLoginDemotion(host: String) -> String? {
        chromeLoginRefusal(host: host)
    }

    /// Sites that revoke a session used from several browsers at once. The deck
    /// keeps them per desk (server/login_vault.py `PER_DESK_SITES`). MEASURED
    /// 2026-10-01: his TikTok session copied to the desks at 17:36 was dead on
    /// his Mac by 18:19; his Instagram one, copied at 23:39, was logged out on a
    /// desk by 00:27.
    public static func isPerDeskSite(host: String) -> Bool {
        let h = host.lowercased()
        return ["tiktok.com", "instagram.com", "stripe.com"]
            .contains { h == $0 || h.hasSuffix("." + $0) }
    }

    /// Why his everyday Chrome session must NOT be copied for `host`, or nil
    /// when it may. Copying it puts his live session on the desks, and for these
    /// sites that signs his own Mac out.
    public static func chromeLoginRefusal(host: String) -> String? {
        if isDeviceBound(host: host) {
            return "Google can't be copied from your Chrome, so sign in fresh with the passkey instead."
        }
        if isPerDeskSite(host: host) {
            return "Sharing your \(host) login signs you out everywhere, so each desk signs in on its own screen. Tap Take over."
        }
        return nil
    }

    /// One sentence for him, per way this can go wrong.
    public static func sentence(for error: Error) -> String {
        switch error {
        case let error as MacSignInError:
            switch error {
            case .chromeMissing:
                return "Google Chrome is not installed on this Mac. Install it, then try again - Safari cannot hand a sign-in to the desks."
            case .chromeDidNotStart:
                return "Chrome did not start. Try again."
            case .chromeClosed:
                return "Chrome was closed before the sign-in was shared. Start again and leave it open until you press Share."
            case .nothingSignedIn:
                return "Nothing is signed in in that Chrome window yet. Finish signing in there, then press Share."
            case .notStarted:
                return "Start with \"Sign in on this Mac\"."
            case .cdp:
                return "Could not read the sign-in from Chrome. Try again."
            }
        case let DeckError.http(_, reason):
            switch reason {
            case "isolated":
                return "This deck keeps each desk's logins separate ([desks] shared_logins = false), so it refused."
            case "no_cookies":
                return "The deck found no usable sign-in in what was sent."
            case "too_many_cookies":
                return "That browser held too many cookies to share at once."
            case "per_desk_site":
                return "This site signs everyone out when one login is shared, so each desk signs in on its own screen. Tap Take over."
            default:
                return "The deck refused the sign-in (\(reason))."
            }
        default:
            return "The sign-in could not be shared: \(error.localizedDescription)"
        }
    }
}

public enum MacSignInError: Error, Equatable, Sendable {
    case chromeMissing, chromeDidNotStart, chromeClosed, nothingSignedIn, notStarted, cdp
}

/// Where he is, per card, drawn on it.
public enum MacSignInPhase: Hashable, Sendable {
    /// Reading the login he already has in his browser here, and sharing it.
    case importing
    /// Chrome is open on this Mac; he signs in there, then presses Share.
    case waitingForYou
    case sharing
    case shared(desks: Int)
    case failed(String)
    /// He is not signed in to this site in his browser here: fall back to the
    /// fresh passkey sign-in, with the reason shown.
    case offerPasskeyInstead(String)
}

/// The deck's answer to `POST /v1/logins`: counts only, never a value.
public struct LoginShare: Hashable, Sendable, Decodable {
    public let cookies: Int
    public let desks: Int
    public let failed: [String]

    public init(cookies: Int, desks: Int, failed: [String]) {
        self.cookies = cookies
        self.desks = desks
        self.failed = failed
    }

    enum CodingKeys: String, CodingKey { case cookies, desks, failed }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        cookies = try container.decodeIfPresent(Int.self, forKey: .cookies) ?? 0
        desks = try container.decodeIfPresent(Int.self, forKey: .desks) ?? 0
        failed = try container.decodeIfPresent([String].self, forKey: .failed) ?? []
    }
}

/// `POST /v1/logins`. A separate protocol, like `DecisionClient`: a transport
/// that predates the route does not conform, and the card does not offer it.
public protocol LoginSharingClient: Sendable {
    /// `cookieJSON` is the CDP cookie array exactly as Chrome reported it. It
    /// is a sign-in: never log it, never keep it.
    func shareLogins(cookieJSON: Data) async throws -> LoginShare
    /// The same, saying where the login came from: "chrome" (his everyday
    /// Chrome) or "fresh" (a passkey sign-in made for the box). The deck
    /// records it on every vault row so the vault is auditable.
    func shareLogins(cookieJSON: Data, via: String) async throws -> LoginShare
}

extension LoginSharingClient {
    public func shareLogins(cookieJSON: Data, via: String) async throws -> LoginShare {
        try await shareLogins(cookieJSON: cookieJSON)
    }
}

/// A browser on this Mac that can hold a passkey sign-in.
public protocol SignInBrowser: Sendable {
    func open(_ url: URL) async throws -> SignInWindow
}

/// One open sign-in window and its throwaway profile.
public protocol SignInWindow: Sendable {
    /// Every cookie in the window's profile, as a JSON array.
    func cookieJSON() async throws -> Data
    /// Quit the browser and delete the profile. Safe to call twice.
    func close() async
}

/// Open, share, close — per card.
public actor MacSignInFlow {
    private let browser: SignInBrowser
    private let sharing: LoginSharingClient
    private var windows: [String: SignInWindow] = [:]

    public init(browser: SignInBrowser, sharing: LoginSharingClient) {
        self.browser = browser
        self.sharing = sharing
    }

    public func isOpen(id: String) -> Bool { windows[id] != nil }

    /// Opens the sign-in window for card `id`, once.
    public func start(id: String, url: URL) async throws {
        guard windows[id] == nil else { return }
        windows[id] = try await browser.open(url)
    }

    /// Reads the sign-in, hands it to the deck, and closes the window — the
    /// close happens on every path, so no signed-in profile is left behind.
    public func finish(id: String) async throws -> LoginShare {
        guard let window = windows.removeValue(forKey: id) else {
            throw MacSignInError.notStarted
        }
        do {
            let jar = try await window.cookieJSON()
            let rows = (try? JSONSerialization.jsonObject(with: jar)) as? [Any]
            guard let rows, !rows.isEmpty else { throw MacSignInError.nothingSignedIn }
            let share = try await sharing.shareLogins(cookieJSON: jar, via: "fresh")
            await window.close()
            return share
        } catch {
            await window.close()
            throw error
        }
    }

    public func cancel(id: String) async {
        await windows.removeValue(forKey: id)?.close()
    }

    /// Every window, for when the app quits.
    public func cancelAll() async {
        let open = windows
        windows = [:]
        for window in open.values { await window.close() }
    }
}

extension AttentionItem {
    /// Where "Sign in on this Mac" opens for this card, or nil when the card is
    /// not a sign-in it can help with.
    public var signInOnMacURL: URL? {
        guard case .handoff(let kind, _, _) = need else { return nil }
        return MacSignIn.url(kind: kind, place: place)
    }
}

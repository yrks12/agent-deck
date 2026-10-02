import Foundation

/// **One sign-in card, one ordered list of what it offers — on every screen.**
///
/// Owner: *"Don't have the login on the screen, only on the side? Also on
/// iPhone I don't have it at all."* The Mac's side pane drew the Chrome-login
/// and passkey buttons; the desk's own thread did not, and the phone had none.
/// The list is decided here, once, so the side pane, the thread and the phone
/// cannot offer different things in a different order.
///
/// Order: use the login he already has, sign in fresh with a passkey, Allow,
/// Allow always for this site, Take over (the last resort), then Skip. Google
/// is the one exception, measured in PR #164: its Chrome sessions are Device
/// Bound, so the passkey leads there.
public enum SignInCardAction: Hashable, Sendable, Identifiable {
    /// Mac: read his Chrome login here. Phone: ask the Mac to.
    case chromeLogin
    /// Mac: open the fresh-sign-in Chrome window here. Phone: ask the Mac to.
    case freshPasskey
    /// One of the deck's own answers (done / always / skipped, or a permission).
    case answer(AttentionAction)
    /// Open the desk's screen and drive it himself.
    case takeOver

    public var id: String {
        switch self {
        case .chromeLogin: return "signin.chrome"
        case .freshPasskey: return "signin.passkey"
        case .answer(let action): return action.id
        case .takeOver: return "takeover"
        }
    }

    /// The request method the phone files for this button, if it is one.
    public var method: LoginMethod? {
        switch self {
        case .chromeLogin: return .chrome
        case .freshPasskey: return .passkey
        default: return nil
        }
    }
}

public enum SignInCard {
    public enum Surface: Hashable, Sendable {
        /// `browser` is his Mac browser's name ("Chrome"); nil hides that button.
        case mac(browser: String?, canSignInHere: Bool)
        /// The phone asks the Mac; `canAskMac` is false against a deck without
        /// the request route.
        case phone(canAskMac: Bool)
    }

    public static func actions(for item: AttentionItem, surface: Surface,
                               canTakeOver: Bool) -> [SignInCardAction] {
        var out: [SignInCardAction] = []
        if let host = item.signInOnMacURL?.host {
            var (chrome, passkey): (Bool, Bool)
            switch surface {
            case .mac(let browser, let here): (chrome, passkey) = (here && browser != nil, here)
            case .phone(let ask): (chrome, passkey) = (ask, ask)
            }
            // His live session never leaves the Mac for a site where that signs
            // him out; a per-desk site is signed in on the desk's own screen.
            let copyable = MacSignIn.chromeLoginRefusal(host: host) == nil
            let shareable = !MacSignIn.isPerDeskSite(host: host)
            (chrome, passkey) = (chrome && copyable, passkey && shareable)
            let leadsWithPasskey = MacSignIn.primary(host: host, hasBrowser: chrome) == .freshPasskey
            if leadsWithPasskey {
                if passkey { out.append(.freshPasskey) }
                if chrome { out.append(.chromeLogin) }
            } else {
                if chrome { out.append(.chromeLogin) }
                if passkey { out.append(.freshPasskey) }
            }
        }
        let answers = item.actions
        func take(_ match: (AttentionAction) -> Bool) -> [SignInCardAction] {
            answers.filter(match).map(SignInCardAction.answer)
        }
        guard item.isHandoff else { return out + answers.map(SignInCardAction.answer) }
        out += take { $0.verb == .handoff(.done) }
        out += take { $0.verb == .handoff(.always) }
        if canTakeOver { out.append(.takeOver) }
        out += take { $0.verb == .handoff(.skipped) }
        // Anything a newer deck adds is still drawn, after the known ones.
        out += take { ![.handoff(.done), .handoff(.always), .handoff(.skipped)].contains($0.verb) }
        return out
    }

    public static func label(_ action: SignInCardAction, item: AttentionItem,
                             surface: Surface) -> String {
        let phone: Bool
        let browser: String
        switch surface {
        case .mac(let b, _): (phone, browser) = (false, b ?? "browser")
        case .phone: (phone, browser) = (true, "Chrome")
        }
        switch action {
        case .chromeLogin:
            return phone ? "Use my Mac's \(browser) login" : "Use my \(browser) login"
        case .freshPasskey:
            return phone ? "Sign in fresh with passkey on my Mac" : "Sign in fresh with passkey"
        case .takeOver:
            return "Take over"
        case .answer(let answer):
            // A browser card's "done" is a yes to the desk carrying on there.
            if answer.verb == .handoff(.done),
               item.actions.contains(where: { $0.verb == .handoff(.always) }) {
                return "Allow"
            }
            return answer.label
        }
    }

    /// The sentence under a button, before it is pressed.
    public static func hint(_ action: SignInCardAction, item: AttentionItem,
                            surface: Surface) -> String {
        let host = item.signInOnMacURL?.host ?? "the site"
        let phone: Bool
        if case .phone = surface { phone = true } else { phone = false }
        switch action {
        case .chromeLogin:
            return MacSignIn.chromeLoginDemotion(host: host)
                ?? (phone ? "Your Mac copies its \(host) login to every desk. Nothing comes to this phone."
                          : "Uses the login you already have here. Only \(host)'s cookies leave this Mac.")
        case .freshPasskey:
            return phone ? "Your Mac opens \(host) in Chrome; finish there with Touch ID."
                         : "Opens \(host) in a separate Chrome window on this Mac. Sign in with Touch ID or your iPhone there; then every desk gets the sign-in."
        case .takeOver:
            return "Last resort: open the desk's screen and do it yourself."
        case .answer(let answer):
            return answer.summary
        }
    }
}

extension AttentionItem {
    /// The cards a desk's own thread draws: its handoffs, nothing else (a
    /// permission is already inline as a tool-call card).
    public static func forThread(_ items: [AttentionItem], desk: String?) -> [AttentionItem] {
        guard let desk, !desk.isEmpty else { return [] }
        return items.filter { $0.isHandoff && $0.deskName == desk }
    }
}

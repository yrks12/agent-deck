import Foundation
import UserNotifications

/// **The words on the grant card, in one place.** The window card and the
/// macOS banner both read from here, so they cannot drift apart: what a grant
/// means is the one sentence a person decides on.
public enum MacGrantCopy {
    public static let allowHour = "Allow for 1 hour"
    public static let alwaysAllow = "Always allow this desk"
    public static let deny = "Deny"

    /// Offered the first time, when no folder is chosen yet.
    public static let suggestedFolder = "~/Agent Deck Workspace"

    public static let unconfinedWarning =
        "This Mac can't fence commands in, so anything it runs would have your full access."

    public static func displayName(_ desk: String) -> String {
        desk.prefix(1).uppercased() + desk.dropFirst()
    }

    public static func title(desk: String) -> String { "\(displayName(desk)) wants to use this Mac" }

    /// What it asked for first, bounded so a huge command cannot fill the card.
    public static func firstRequest(_ summary: String) -> String {
        let s = summary.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty { return "Something on this Mac" }
        return s.count > 200 ? String(s.prefix(200)) + "…" : s
    }

    /// "Agent Deck Workspace", "acme, Notes" — folder names, no paths.
    public static func folderNames(_ folders: [String]) -> String {
        folders.map { ($0 as NSString).lastPathComponent }.joined(separator: ", ")
    }

    public static func scope(folders: [String]) -> String {
        if folders.isEmpty {
            return "It can run commands as you. No folder is chosen yet, so it can't change files until you pick one."
        }
        return "It can run commands as you and change files in: \(folderNames(folders))"
    }
}

/// **The grant card as a macOS notification**, with the three answers as
/// buttons on the banner: he can say yes or no without opening the app.
/// Pure builders here; posting goes through `UNUserNotificationCenter`, which
/// only exists inside the app bundle, so nothing in this file touches it
/// unless asked.
public enum MacGrantNotifications {
    public static let categoryIdentifier = "\(DeckIdentity.bundleID).mac-grant"
    static let userInfoDeskKey = "mac_grant_desk"

    /// Identified under the category, which is under this build's bundle id.
    public enum Action: CaseIterable, Sendable, RawRepresentable {
        case hour, always, deny

        public init?(rawValue: String) {
            guard let match = Self.allCases.first(where: { $0.rawValue == rawValue }) else { return nil }
            self = match
        }

        public var rawValue: String {
            switch self {
            case .hour: return "\(MacGrantNotifications.categoryIdentifier).hour"
            case .always: return "\(MacGrantNotifications.categoryIdentifier).always"
            case .deny: return "\(MacGrantNotifications.categoryIdentifier).deny"
            }
        }

        var title: String {
            switch self {
            case .hour: return MacGrantCopy.allowHour
            case .always: return MacGrantCopy.alwaysAllow
            case .deny: return MacGrantCopy.deny
            }
        }

        var decision: MacGrantDecision {
            switch self {
            case .hour: return .hour
            case .always: return .always
            case .deny: return .deny
            }
        }
    }

    /// What a tap on the banner means. `nil` for the banner itself and for
    /// swiping it away: neither is an answer.
    public static func decision(forAction identifier: String) -> MacGrantDecision? {
        Action(rawValue: identifier)?.decision
    }

    public static func category() -> UNNotificationCategory {
        let actions = Action.allCases.map { action in
            UNNotificationAction(identifier: action.rawValue, title: action.title,
                                 options: action == .deny ? [.destructive] : [])
        }
        return UNNotificationCategory(identifier: categoryIdentifier, actions: actions,
                                      intentIdentifiers: [], options: [])
    }

    /// One banner per desk: a second ask from the same desk replaces it.
    public static func identifier(desk: String) -> String { "\(categoryIdentifier).\(desk)" }

    public static func content(for request: MacGrantRequest) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = MacGrantCopy.title(desk: request.desk)
        content.body = MacGrantCopy.firstRequest(request.summary)
        content.categoryIdentifier = categoryIdentifier
        content.userInfo = [userInfoDeskKey: request.desk]
        content.sound = .default
        return content
    }

    public static func desk(from userInfo: [AnyHashable: Any]) -> String? {
        userInfo[userInfoDeskKey] as? String
    }

    /// Who applies a banner's answer: the app's `MacBridgeHost` sets it at
    /// launch. The centre has ONE delegate (`OwnerNotificationRouter`), so it
    /// hands grant buttons here instead of treating them as a thread tap.
    @MainActor public static var onAnswer: ((String, MacGrantDecision) -> Void)?

    /// True when the response was a grant banner's button and was handled.
    @MainActor public static func handle(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> Bool {
        guard let decision = decision(forAction: actionIdentifier), let desk = desk(from: userInfo) else { return false }
        onAnswer?(desk, decision)
        return true
    }

    // MARK: the centre (app bundle only)

    /// Registers the three buttons. Call once at launch.
    public static func register(on center: UNUserNotificationCenter = .current()) {
        // Added, never set: the centre holds one set for the whole app, and
        // the owner-alert Reply lives in it too (`DeckNotificationCategories`).
        DeckNotificationCategories.add(category(), on: center)
    }

    /// Shows the banner for a request. Asks for permission the first time; if
    /// he has said no to notifications the window card is still there.
    public static func post(_ request: MacGrantRequest, on center: UNUserNotificationCenter = .current()) {
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            center.add(UNNotificationRequest(identifier: identifier(desk: request.desk),
                                             content: content(for: request), trigger: nil))
        }
    }

    /// Takes the banner down once he has answered somewhere else.
    public static func withdraw(desk: String, on center: UNUserNotificationCenter = .current()) {
        center.removeDeliveredNotifications(withIdentifiers: [identifier(desk: desk)])
        center.removePendingNotificationRequests(withIdentifiers: [identifier(desk: desk)])
    }
}

/// **"Atlas wants to control this Mac" as a banner**, with Allow (30 min,
/// that desk) and Deny on it: the tap-to-approve end of a desk's
/// `control_off`. The grant is made here, on the Mac — the deck cannot.
public enum MacControlNotifications {
    public static let categoryIdentifier = "\(DeckIdentity.bundleID).mac-control"
    public static let allowAction = "\(categoryIdentifier).allow"
    public static let denyAction = "\(categoryIdentifier).deny"
    static let userInfoDeskKey = "mac_control_desk"

    public static func category() -> UNNotificationCategory {
        UNNotificationCategory(identifier: categoryIdentifier, actions: [
            UNNotificationAction(identifier: allowAction, title: MacControlCopy.allowTitle, options: []),
            UNNotificationAction(identifier: denyAction, title: MacGrantCopy.deny, options: [.destructive]),
        ], intentIdentifiers: [], options: [])
    }

    public static func identifier(desk: String) -> String { "\(categoryIdentifier).\(desk)" }

    public static func content(desk: String) -> UNMutableNotificationContent {
        let content = UNMutableNotificationContent()
        content.title = MacControlCopy.askTitle(desk: desk)
        content.body = MacControlCopy.askBody
        content.categoryIdentifier = categoryIdentifier
        content.userInfo = [userInfoDeskKey: desk]
        content.sound = .default
        return content
    }

    /// Who applies Allow: `MacBridgeHost` at launch. (desk, allowed)
    @MainActor public static var onAnswer: ((String, Bool) -> Void)?

    @MainActor public static func handle(actionIdentifier: String, userInfo: [AnyHashable: Any]) -> Bool {
        guard [allowAction, denyAction].contains(actionIdentifier),
              let desk = userInfo[userInfoDeskKey] as? String else { return false }
        onAnswer?(desk, actionIdentifier == allowAction)
        return true
    }

    public static func register(on center: UNUserNotificationCenter = .current()) {
        DeckNotificationCategories.add(category(), on: center)
    }

    public static func post(desk: String, on center: UNUserNotificationCenter = .current()) {
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            guard granted else { return }
            center.add(UNNotificationRequest(identifier: identifier(desk: desk), content: content(desk: desk),
                                             trigger: nil))
        }
    }

    public static func withdraw(desk: String, on center: UNUserNotificationCenter = .current()) {
        center.removeDeliveredNotifications(withIdentifiers: [identifier(desk: desk)])
        center.removePendingNotificationRequests(withIdentifiers: [identifier(desk: desk)])
    }
}

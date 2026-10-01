import Foundation
#if canImport(UserNotifications)
import UserNotifications
#endif

/// **One thing a desk needs him for, or said to him** — `GET /v1/owner/alerts`.
///
/// The deck decides WHAT may notify him (`server/owner_alerts.py`): a decision
/// card, a pending approval, a waiting handoff (`needs_you`), and a desk's
/// message meant for him (`for_you`) — never desk↔desk chatter, never a
/// progress line. Both apps and the ntfy sender read the same page, so the
/// phone and the Mac can never disagree about what was worth a buzz.
public struct OwnerAlert: Codable, Hashable, Sendable, Identifiable {
    public enum Kind: String, Codable, Sendable {
        case needsYou = "needs_you"
        case forYou = "for_you"
    }

    public let id: String
    public let kind: Kind
    /// `message`, `decision`, `approval` or `handoff`.
    public let source: String
    public let agent: String
    public let threadID: String
    public let cardID: String
    public let title: String
    public let body: String
    public let ts: Double
    public let cursor: String
    public let urgent: Bool

    public init(id: String, kind: Kind, source: String, agent: String, threadID: String,
                cardID: String, title: String, body: String, ts: Double, cursor: String,
                urgent: Bool = false) {
        self.id = id; self.kind = kind; self.source = source; self.agent = agent
        self.threadID = threadID; self.cardID = cardID; self.title = title; self.body = body
        self.ts = ts; self.cursor = cursor; self.urgent = urgent
    }

    enum CodingKeys: String, CodingKey {
        case id, kind, source, agent, title, body, ts, cursor, urgent
        case threadID = "thread_id"
        case cardID = "card_id"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        kind = try c.decode(Kind.self, forKey: .kind)
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? "message"
        agent = try c.decodeIfPresent(String.self, forKey: .agent) ?? ""
        threadID = try c.decodeIfPresent(String.self, forKey: .threadID) ?? ""
        cardID = try c.decodeIfPresent(String.self, forKey: .cardID) ?? ""
        title = try c.decodeIfPresent(String.self, forKey: .title) ?? agent
        body = try c.decodeIfPresent(String.self, forKey: .body) ?? ""
        ts = try c.decodeIfPresent(Double.self, forKey: .ts) ?? 0
        cursor = try c.decode(String.self, forKey: .cursor)
        urgent = try c.decodeIfPresent(Bool.self, forKey: .urgent) ?? false
    }
}

public struct OwnerAlertPage: Codable, Hashable, Sendable {
    public let alerts: [OwnerAlert]
    public let nextSince: String

    public init(alerts: [OwnerAlert], nextSince: String) {
        self.alerts = alerts
        self.nextSince = nextSince
    }

    enum CodingKeys: String, CodingKey {
        case alerts
        case nextSince = "next_since"
    }
}

/// **What this device has already told him.** A value, persisted as JSON.
///
/// Two guards, because two things can hand the same alert over twice: the
/// cursor (the deck only returns what is after it) and the ids (the background
/// refresh and the foreground poll can both be in flight with the same cursor,
/// and a restored older cursor re-reads a page). The system also dedupes on the
/// notification identifier, which is the alert id — a third, free.
public struct AlertLedger: Codable, Equatable, Sendable {
    /// Ids remembered. A night's worth many times over; bounded so the ledger
    /// never grows with the deck's age.
    public static let memory = 200

    public private(set) var since: String?
    public private(set) var posted: [String]

    public init(since: String? = nil, posted: [String] = []) {
        self.since = since
        self.posted = posted
    }

    /// Folds one page in and returns what to post. A first run (no cursor
    /// yet) posts nothing and starts at the head: a fresh install is not
    /// buzzed with a month of history. An alert on `openThread` — the thread
    /// he is reading right now — is counted as told but not posted.
    public mutating func take(_ page: OwnerAlertPage, openThread: String?) -> [OwnerAlert] {
        guard since != nil else {
            since = page.nextSince
            return []
        }
        var known = Set(posted)
        var out: [OwnerAlert] = []
        for alert in page.alerts where !known.contains(alert.id) {
            known.insert(alert.id)
            posted.append(alert.id)
            if let open = openThread, !open.isEmpty, alert.threadID == open { continue }
            out.append(alert)
        }
        if posted.count > Self.memory { posted.removeFirst(posted.count - Self.memory) }
        if !page.nextSince.isEmpty, page.nextSince > (since ?? "") { since = page.nextSince }
        return out
    }
}

/// **Where a tap goes**: that desk's thread, and the card when there is one.
/// Read from a local notification's `userInfo` or from the ntfy click link
/// `agentdeck://open?thread=direct%3Aatlas&card=…` — the same route either way.
public struct AlertRoute: Hashable, Sendable {
    public static let scheme = "agentdeck"

    public let threadID: String
    public let agent: String
    public let cardID: String?

    public init(threadID: String, agent: String, cardID: String?) {
        self.threadID = threadID
        self.agent = agent
        self.cardID = (cardID?.isEmpty ?? true) ? nil : cardID
    }

    init?(thread: String?, card: String?) {
        guard let thread, !thread.isEmpty else { return nil }
        self.init(threadID: thread, agent: ThreadID.desk(ofDirect: thread) ?? thread, cardID: card)
    }

    public init?(userInfo: [AnyHashable: Any]) {
        self.init(thread: userInfo[AlertNotificationText.threadKey] as? String,
                  card: userInfo[AlertNotificationText.cardKey] as? String)
    }

    public init?(url: URL) {
        guard url.scheme?.lowercased() == Self.scheme, url.host?.lowercased() == "open",
              let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
        else { return nil }
        func value(_ name: String) -> String? { items.first { $0.name == name }?.value }
        self.init(thread: value("thread"), card: value("card"))
    }
}

/// An alert as the words of a notification. PURE, so both apps draw it the
/// same way and the test reads it without a notification centre.
public struct AlertNotificationText: Hashable, Sendable {
    static let threadKey = "deck_thread"
    static let cardKey = "deck_card"
    static let alertKey = "deck_alert"

    public let identifier: String
    public let threadIdentifier: String
    public let title: String
    public let body: String
    /// Something only he can clear breaks through a Focus summary.
    public let timeSensitive: Bool
    public let userInfo: [String: String]

    public init(_ alert: OwnerAlert) {
        identifier = alert.id
        threadIdentifier = alert.threadID
        title = alert.title.isEmpty ? Agent.displayName(forWireName: alert.agent) : alert.title
        body = alert.body
        timeSensitive = alert.kind == .needsYou || alert.urgent
        userInfo = [Self.threadKey: alert.threadID, Self.cardKey: alert.cardID,
                    Self.alertKey: alert.id]
    }
}

#if canImport(UserNotifications)
/// **Posts owner alerts as local notifications, once each.** Shared by the
/// iPhone (foreground poll + background refresh) and the Mac app.
///
/// Local, not push: the phone is signed with a free Apple team, which cannot
/// use APNs. See `docs/notifications.md` for the ntfy path that is real-time.
@MainActor
public final class OwnerNotifier {
    public typealias Fetch = @Sendable (String?) async throws -> OwnerAlertPage

    private let defaults: UserDefaults
    private let key: String
    /// The thread on screen right now; its alerts are not posted.
    public var openThread: String?

    public init(defaults: UserDefaults = .standard, key: String = "deck.ownerAlerts.ledger") {
        self.defaults = defaults
        self.key = key
    }

    /// A notification centre exists only inside an app bundle; `swift test`
    /// and `swift run` have none, and touching it there traps.
    public static var isAvailable: Bool {
        Bundle.main.bundleIdentifier != nil && Bundle.main.bundleURL.pathExtension == "app"
    }

    public var ledger: AlertLedger {
        get {
            (defaults.data(forKey: key)).flatMap { try? JSONDecoder().decode(AlertLedger.self, from: $0) }
                ?? AlertLedger()
        }
        set { defaults.set(try? JSONEncoder().encode(newValue), forKey: key) }
    }

    @discardableResult
    public static func requestPermission() async -> Bool {
        guard isAvailable else { return false }
        return (try? await UNUserNotificationCenter.current()
            .requestAuthorization(options: [.alert, .sound, .badge])) ?? false
    }

    /// One poll: fetch after the ledger's cursor, post what is new. Returns
    /// how many were posted. Never throws: a deck out of reach is a quiet
    /// poll, retried on the next one.
    @discardableResult
    public func poll(_ fetch: Fetch) async -> Int {
        let since = ledger.since
        guard let page = try? await fetch(since) else { return 0 }
        var next = ledger
        let fresh = next.take(page, openThread: openThread)
        ledger = next
        guard Self.isAvailable, !fresh.isEmpty else { return fresh.count }
        let center = UNUserNotificationCenter.current()
        for alert in fresh {
            let text = AlertNotificationText(alert)
            let content = UNMutableNotificationContent()
            content.title = text.title
            content.body = text.body
            content.threadIdentifier = text.threadIdentifier
            content.userInfo = text.userInfo
            content.sound = .default
            if #available(iOS 15.0, macOS 12.0, *) {
                content.interruptionLevel = text.timeSensitive ? .timeSensitive : .active
            }
            try? await center.add(UNNotificationRequest(identifier: text.identifier,
                                                        content: content, trigger: nil))
        }
        return fresh.count
    }

    /// Clears what was delivered for a thread he has now opened.
    public func withdraw(thread: String) {
        guard Self.isAvailable else { return }
        let center = UNUserNotificationCenter.current()
        center.getDeliveredNotifications { delivered in
            let ids = delivered.filter { $0.request.content.threadIdentifier == thread }
                .map(\.request.identifier)
            if !ids.isEmpty { center.removeDeliveredNotifications(withIdentifiers: ids) }
        }
    }
}
/// **The notification centre's delegate**: a tap becomes an `AlertRoute`,
/// and nothing banners over the thread he is already reading. One per app,
/// retained by the app (the centre holds its delegate weakly).
@MainActor
public final class OwnerNotificationRouter: NSObject, UNUserNotificationCenterDelegate {
    public var onRoute: (AlertRoute) -> Void
    /// The thread on screen, read at the moment a notification arrives.
    public var openThread: () -> String?

    public init(openThread: @escaping () -> String? = { nil },
                onRoute: @escaping (AlertRoute) -> Void) {
        self.openThread = openThread
        self.onRoute = onRoute
    }

    /// Installs itself as the centre's delegate. No-op outside an app bundle.
    public func install() {
        guard OwnerNotifier.isAvailable else { return }
        UNUserNotificationCenter.current().delegate = self
    }

    nonisolated public func userNotificationCenter(
        _ center: UNUserNotificationCenter, willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        let thread = notification.request.content.threadIdentifier
        Task { @MainActor in
            let reading = !thread.isEmpty && self.openThread() == thread
            completionHandler(reading ? [] : [.banner, .list, .sound])
        }
    }

    nonisolated public func userNotificationCenter(
        _ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        let route = AlertRoute(userInfo: response.notification.request.content.userInfo)
        Task { @MainActor in
            if let route { self.onRoute(route) }
            completionHandler()
        }
    }
}
#endif

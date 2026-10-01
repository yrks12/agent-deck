import SwiftUI
import UIKit
import BackgroundTasks
import UserNotifications
import DeckKit

/// **"Notify me when they need me and when they send messages intended to me."**
///
/// What may notify him is the deck's decision (`GET /v1/owner/alerts`, one
/// classification shared with the Mac and ntfy). This type owns only the
/// phone's half:
///
/// * **Foreground and backgrounded-alive** — a poll every few seconds while
///   the app runs (a call keeps it running with the screen locked).
/// * **Asleep** — a `BGAppRefreshTask` asks the same question whenever iOS
///   grants a refresh. iOS decides how often; it is not real time. The
///   real-time path on a free Apple team is ntfy (`docs/notifications.md`).
/// * **A tap** — the notification (or an ntfy click link, `agentdeck://open`)
///   opens that desk's thread.
///
/// Once-only is `AlertLedger`'s job (DeckKit, tested there); the thread he is
/// reading never banners.
@MainActor
final class PhoneAlerts: ObservableObject {
    static let shared = PhoneAlerts()
    static let refreshTask = "\(DeckIdentity.bundleID).owner-alerts"

    /// Set by a tap or a link; `RootView` opens it and clears it.
    @Published var route: AlertRoute?

    private let notifier = OwnerNotifier()
    private lazy var router = OwnerNotificationRouter(
        openThread: { [weak self] in self?.readingThread },
        onRoute: { [weak self] in self?.route = $0 })
    private var visibleThread: String?
    private var active = true
    private var loop: Task<Void, Never>?

    /// The thread on screen while the app is in front; nil otherwise, so an
    /// alert that lands while he is away is never swallowed as "read".
    private var readingThread: String? { active ? visibleThread : nil }

    private init() {}

    // MARK: lifecycle

    /// From `App.init`: iOS requires the refresh handler before launch ends.
    func install() {
        router.install()
        BGTaskScheduler.shared.register(forTaskWithIdentifier: Self.refreshTask, using: nil) { task in
            guard let task = task as? BGAppRefreshTask else { return }
            Task { @MainActor in PhoneAlerts.shared.handle(task) }
        }
    }

    /// The deck is connected: ask once for permission, then keep polling.
    func start() {
        guard loop == nil else { return }
        loop = Task { [weak self] in
            _ = await OwnerNotifier.requestPermission()
            PushRegistration.registerIfEnabled()
            while !Task.isCancelled {
                await self?.pollOnce()
                try? await Task.sleep(nanoseconds: 12_000_000_000)
            }
        }
    }

    func stop() {
        loop?.cancel()
        loop = nil
    }

    func scenePhase(_ phase: ScenePhase) {
        active = phase == .active
        switch phase {
        case .active:
            Task { await pollOnce() }
        case .background:
            schedule()
        default:
            break
        }
    }

    // MARK: the thread on screen

    func opened(thread: String) {
        visibleThread = thread
        notifier.withdraw(thread: thread)
    }

    func closed(thread: String) {
        if visibleThread == thread { visibleThread = nil }
    }

    /// An `agentdeck://open?thread=…` link — an ntfy tap lands here.
    func open(_ url: URL) {
        if let route = AlertRoute(url: url) { self.route = route }
    }

    // MARK: polling

    @discardableResult
    func pollOnce() async -> Int {
        guard let client = Self.client() else { return 0 }
        notifier.openThread = readingThread
        return await notifier.poll { try await client.ownerAlerts(since: $0) }
    }

    /// A client from the saved deck, so a background launch with no window
    /// and no `PhoneStore` running can still ask.
    private static func client() -> HTTPDeckClient? {
        let deck = DeckConnection()
        guard let saved = deck.saved else { return nil }
        return HTTPDeckClient(baseURL: saved.url, tokens: StateSuite().tokenStore(),
                              performer: deck.requestPerformer(), sse: deck.sseTransport())
    }

    // MARK: background refresh

    func schedule() {
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTask)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }

    private func handle(_ task: BGAppRefreshTask) {
        schedule()  // the next one first: a refresh that dies must not end the chain
        let work = Task { @MainActor in
            active = false
            await pollOnce()
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = { work.cancel() }
    }
}

/// **The APNs path, prepared and OFF.** A free Apple team cannot sign
/// `aps-environment`, so nothing here runs unless the Info.plist says
/// `DeckAPNsEnabled = true` — which needs the paid account, the entitlement in
/// `APNs.entitlements`, and a deck-side sender (not built). The token is kept
/// for that sender to collect.
enum PushRegistration {
    static let tokenKey = "deck.apns.token"

    static var isEnabled: Bool {
        Bundle.main.object(forInfoDictionaryKey: "DeckAPNsEnabled") as? Bool ?? false
    }

    @MainActor
    static func registerIfEnabled() {
        guard isEnabled else { return }
        UIApplication.shared.registerForRemoteNotifications()
    }

    static func received(token: Data) {
        let hex = token.map { String(format: "%02x", $0) }.joined()
        UserDefaults.standard.set(hex, forKey: tokenKey)
    }
}

extension PhoneAppDelegate {
    @objc func application(_ application: UIApplication,
                           didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
        PushRegistration.received(token: deviceToken)
    }
}

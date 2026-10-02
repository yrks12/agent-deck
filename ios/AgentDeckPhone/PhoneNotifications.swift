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
/// * **A desk calling** — every page lists what is ringing; while it rings the
///   app draws `IncomingCallView`. In front it asks every 3 s
///   (`OwnerAlertCadence`) so a 30 s ring is seen. A tapped ring
///   (`agentdeck://call?ring=…`) asks at once: still ringing opens the call,
///   ran out opens the thread with one line (`IncomingCallState`).
///
/// Once-only is `AlertLedger`'s job (DeckKit, tested there); the thread he is
/// reading never banners.
@MainActor
final class PhoneAlerts: ObservableObject {
    static let shared = PhoneAlerts()
    static let refreshTask = "\(DeckIdentity.bundleID).owner-alerts"

    /// Set by a tap or a link; `RootView` opens it and clears it.
    @Published var route: AlertRoute?
    /// A desk calling him, while it rings and until he answers or declines.
    @Published private(set) var incoming: IncomingRing?
    /// One line: a tapped call that already ran out, or a decline that failed.
    @Published var notice: String?

    private let notifier = OwnerNotifier()
    private lazy var router = OwnerNotificationRouter(
        openThread: { [weak self] in self?.readingThread },
        onRoute: { [weak self] in self?.receive($0) })
    private var visibleThread: String?
    private var active = true
    private var loop: Task<Void, Never>?
    private var calling = IncomingCallState()
    private var lastRings: [IncomingRing] = []

    /// The thread on screen while the app is in front; nil otherwise, so an
    /// alert that lands while he is away is never swallowed as "read".
    private var readingThread: String? { active ? visibleThread : nil }

    private init() {}

    // MARK: lifecycle

    /// From `App.init`: iOS requires the refresh handler before launch ends.
    func install() {
        // Reply typed on the banner: sent as a quote-reply from wherever the
        // phone is. A refusal opens the thread instead of losing it.
        router.onReply = { @MainActor [weak self] reply in
            do {
                guard let client = Self.client() else { throw DeckError.transport("no deck") }
                _ = try await client.send(threadID: reply.threadID, text: reply.text, replyTo: reply.replyTo)
            } catch {
                self?.route = AlertRoute(threadID: reply.threadID,
                                         agent: ThreadID.desk(ofDirect: reply.threadID) ?? reply.threadID,
                                         cardID: nil)
            }
        }
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
                let pace = OwnerAlertCadence.interval(active: self?.active ?? false)
                try? await Task.sleep(nanoseconds: UInt64(pace * 1_000_000_000))
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
        if let route = AlertRoute(url: url) { receive(route) }
    }

    /// A tap or a link. A desk's call asks the deck first: still ringing, it
    /// comes up full screen; ran out, its thread opens with one line.
    func receive(_ route: AlertRoute) {
        guard route.ringID != nil else { self.route = route; return }
        Task {
            let rings = await pollOnce()
            switch calling.open(route, ringing: rings, now: Date()) {
            case .ringing?:
                incoming = calling.showing
            case .missed(let thread, let agent, let line)?:
                incoming = calling.showing
                notice = line
                self.route = AlertRoute(threadID: thread, agent: agent, cardID: nil)
            case nil:
                self.route = route
            }
        }
    }

    // MARK: a desk calling

    /// The screen's countdown reached zero: drop a ring that ran out without
    /// waiting for the next page.
    func tick() {
        incoming = calling.update(lastRings, now: Date())
    }

    /// He tapped Answer; the call screen takes it from here.
    func answered(_ ring: IncomingRing) {
        calling.handle(ring.id)
        incoming = calling.showing
    }

    /// He tapped Decline: the deck posts the desk's reason to his thread.
    func decline(_ ring: IncomingRing) {
        calling.handle(ring.id)
        incoming = calling.showing
        Task {
            do {
                guard let client = Self.client() else { throw DeckError.transport("no deck") }
                try await client.declineRing(id: ring.id)
            } catch {
                notice = "Couldn't reach the deck to decline — it stops ringing on its own"
            }
        }
    }

    // MARK: polling

    /// One ask. Returns what is ringing, or nil when the deck was not reached.
    @discardableResult
    func pollOnce() async -> [IncomingRing]? {
        guard let client = Self.client() else { return nil }
        notifier.openThread = readingThread
        // ntfy, when the deck has it, has already buzzed this phone.
        notifier.deferToNtfy = true
        let inFront = active
        let seen = RingBox()
        await notifier.poll { since in
            let page = try await client.ownerAlerts(since: since, active: inFront)
            seen.rings = page.ringing
            return page
        }
        guard let rings = seen.rings else { return nil }
        lastRings = rings
        incoming = calling.update(rings, now: Date())
        return rings
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

/// The ringing list off the page `OwnerNotifier` fetched (its fetch is `@Sendable`).
private final class RingBox: @unchecked Sendable {
    private let lock = NSLock()
    private var value: [IncomingRing]?
    var rings: [IncomingRing]? {
        get { lock.lock(); defer { lock.unlock() }; return value }
        set { lock.lock(); value = newValue; lock.unlock() }
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

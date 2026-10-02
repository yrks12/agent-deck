#if os(macOS)
import Foundation
import UserNotifications

/// **A4: the app's one handle on the Mac bridge.**
///
/// `MacBridge` is an actor that dials out, claims jobs and asks the policy;
/// nothing started it, so the Mac never registered with the deck and no desk
/// could ever use this Mac. The host is what the app shell starts at launch
/// and stops at Quit, and what every Mac surface reads: the grant card, the
/// in-use bar, the menu-bar extra and Settings → Mac all draw `state` and call
/// back here.
///
/// The direct build carries the bridge; the App Store build never does — its
/// App Sandbox cannot spawn a login shell or wrap one in `sandbox-exec`, so
/// `live(channel: .appStore)` is nil and desks see `no_mac`, whose detail
/// names the direct download.
@MainActor
public final class MacBridgeHost: ObservableObject {

    /// The two distribution channels (macos/App/project.yml, contract C1).
    public enum Channel: Sendable { case direct, appStore }

    /// Only the direct channel may carry the bridge.
    public nonisolated static func carriesBridge(_ channel: Channel) -> Bool { channel == .direct }

    /// The name desks see, when he has typed one in Settings → Mac.
    public static let nameKey = "macNodeName"

    @Published public private(set) var state: MacBridgeState
    @Published public private(set) var config: MacPolicyConfig
    /// The deck's host, for the Full access sheet.
    public let deckHost: String?
    public private(set) var macName: String

    private var bridge: MacBridge
    private let rebuild: ((String) -> MacBridge)?
    private let postsNotifications: Bool
    private var bannered: Set<String> = []
    private var lastActiveMode: MacMode
    private var started = false
    // Mac control: the clock, the lock screen, the hotkey and the banners.
    private var controlClock: Task<Void, Never>?
    private var lockObserver: NSObjectProtocol?
    private let hotkey = MacControlHotkey()
    private var controlBannered: Set<String> = []
    /// Called after every state change (the app's banner and hand monitor).
    public var onStateChange: ((MacBridgeState) -> Void)?
    /// "mac:<node id>" once the bridge has registered: the live view's name.
    @Published public private(set) var liveViewName: String?

    /// Hands the actor's state to the host on the main queue, in order.
    public final class Relay: @unchecked Sendable {
        weak var host: MacBridgeHost?
        public init() {}
        public func send(_ s: MacBridgeState) {
            DispatchQueue.main.async { MainActor.assumeIsolated { self.host?.receive(s) } }
        }
    }

    /// `rebuild` makes a fresh bridge under a new name (Settings → This
    /// Mac's name); nil means the name is fixed.
    public init(bridge: MacBridge, relay: Relay, config: MacPolicyConfig, macName: String, deckHost: String?,
                postsNotifications: Bool = false, rebuild: ((String) -> MacBridge)? = nil) {
        self.bridge = bridge
        self.config = config
        self.macName = macName
        self.deckHost = deckHost
        self.postsNotifications = postsNotifications
        self.rebuild = rebuild
        lastActiveMode = config.mode == .paused || config.mode == .off ? .ask : config.mode
        state = MacBridgeState(mode: config.mode, connection: MacBridgeState.initialConnection(mode: config.mode, deck: nil))
        relay.host = self
    }

    /// The real thing: this suite's saved deck and token, the policy and log
    /// in Application Support, the login-shell executor. Nil on the App Store
    /// channel and when the app is not connected to a deck yet.
    public static func live(channel: Channel, suite: StateSuite = StateSuite(),
                            defaults: UserDefaults = .standard) -> MacBridgeHost? {
        guard carriesBridge(channel), let client = MacNodeClient.live(suite: suite) else { return nil }
        MacDisplayReader.watchNames()
        let store = MacPolicyStore()
        let config = store.load()
        let activity = MacActivityLog()
        let machineId = MacNodeIdentity(suite: suite).machineId()
        let version = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0.0.0"
        let relay = Relay()
        let make: (String) -> MacBridge = { name in
            let described = MacNodeIdentity.describe(name: name.isEmpty ? nil : name)
            let settings = MacBridgeSettings(machineId: machineId, name: described.name, os: described.os,
                                             appVersion: version)
            return MacBridge(client: client, executor: MacLocalExecutor(), policy: MacPolicy(config: store.load()),
                             policyStore: store, activity: activity, settings: settings,
                             input: MacInputInjector(), screen: MacScreenStreamer(client: client),
                             probe: {
                                 MacControlProbe(perms: MacPermissions(
                                    accessibility: MacInputInjector.accessibilityGranted,
                                    screenRecording: MacScreenStreamer.screenRecordingGranted),
                                                 screen: MacScreenStreamer.frameSize(),
                                                 displays: MacDisplayReader.current())
                             },
                             makeShell: { cols, rows in try MacPTYShell(cols: cols, rows: rows) },
                             onState: { relay.send($0) })
        }
        let name = defaults.string(forKey: nameKey) ?? ""
        return MacBridgeHost(bridge: make(name), relay: relay, config: config,
                             macName: MacNodeIdentity.describe(name: name.isEmpty ? nil : name).name,
                             deckHost: client.deckURL.host, postsNotifications: OwnerNotifier.isAvailable,
                             rebuild: make)
    }

    // MARK: lifecycle

    public func start() {
        guard !started else { return }
        started = true
        if postsNotifications {
            MacGrantNotifications.register()
            MacGrantNotifications.onAnswer = { [weak self] desk, decision in self?.answer(desk: desk, decision) }
            MacControlNotifications.register()
            MacControlNotifications.onAnswer = { [weak self] desk, allowed in
                if allowed { self?.startControl(scope: desk) }
            }
        }
        let b = bridge
        Task { await b.start() }
    }

    /// Quit: every job killed and reported before the app goes. The control
    /// grant lives only in memory, so it goes with the app.
    public func stop() async {
        started = false
        stopControl()
        await bridge.stop()
    }

    // MARK: Mac control

    /// "Let agents control this Mac": `scope` one desk, nil every desk, for
    /// 30 minutes. Ends on Stop, ⌃⌥⌘., the lock screen, Quit or the clock.
    public func startControl(scope: String?) {
        let grant = MacControlGrant.start(scope: scope, now: Date().timeIntervalSince1970)
        let b = bridge
        Task { await b.setControl(grant) }
        controlClock?.cancel()
        controlClock = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(MacControlGrant.duration * 1e9))
            guard !Task.isCancelled else { return }
            self?.stopControl()
        }
        if lockObserver == nil {
            lockObserver = DistributedNotificationCenter.default().addObserver(
                forName: Notification.Name("com.apple.screenIsLocked"), object: nil, queue: .main
            ) { [weak self] _ in MainActor.assumeIsolated { self?.stopControl() } }
        }
        syncHotkey(controlOn: true)
        if let scope { MacControlNotifications.withdraw(desk: scope) }
    }

    /// Stop: the grant ends now and every agent gesture is refused again.
    public func stopControl() {
        controlClock?.cancel()
        controlClock = nil
        if let lockObserver { DistributedNotificationCenter.default().removeObserver(lockObserver) }
        lockObserver = nil
        syncHotkey(controlOn: false)
        let b = bridge
        Task { await b.setControl(nil) }
    }

    /// Stop on his Mac's terminal banner (and ⌃⌥⌘.): the session and its
    /// shell end now.
    public func stopTerminal() {
        let b = bridge
        Task { await b.stopTerminal(.stopped) }
    }

    /// Stop on the banner, ⌃⌥⌘.: everything remote on this Mac ends.
    public func stopAll() {
        stopControl()
        stopTerminal()
    }

    /// Is ⌃⌥⌘. armed right now?
    public var hotkeyArmed: Bool { hotkey.isRegistered }

    /// ⌃⌥⌘. is registered while agents may control this Mac OR his terminal
    /// is open from elsewhere, and only then.
    private func syncHotkey(controlOn: Bool? = nil) {
        let on = controlOn ?? controlLive
        if on || state.terminal != nil {
            hotkey.register { [weak self] in self?.stopAll() }
        } else {
            hotkey.unregister()
        }
    }

    public var controlLive: Bool { state.control?.live(at: Date().timeIntervalSince1970) == true }

    /// The app may close its last window and keep running only while Mac
    /// access is on: the menu-bar extra keeps the bridge alive.
    public var keepsAppAlive: Bool { config.mode != .off }

    // MARK: what the surfaces call

    public func answer(desk: String, _ decision: MacGrantDecision) {
        let b = bridge
        Task { await b.answerGrant(desk: desk, decision: decision) }
    }

    public func stopJob(_ id: String) {
        let b = bridge
        Task { await b.stopJob(id) }
    }

    public func stopDesk(_ desk: String) {
        let b = bridge
        Task { await b.stopDesk(desk) }
    }

    public func pause() { setMode(.paused) }

    public func resume() { setMode(lastActiveMode) }

    public func setMode(_ mode: MacMode) {
        var c = config
        c.mode = mode
        setPolicy(c)
    }

    public func setPolicy(_ new: MacPolicyConfig) {
        if new.mode == .ask || new.mode == .full { lastActiveMode = new.mode }
        config = new
        let b = bridge
        Task { await b.setPolicy(new) }
    }

    /// A new name for desks to see: the bridge re-registers under it.
    public func rename(_ name: String, defaults: UserDefaults = .standard) {
        let clean = name.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let rebuild, clean != macName else { return }
        defaults.set(clean, forKey: Self.nameKey)
        macName = MacNodeIdentity.describe(name: clean.isEmpty ? nil : clean).name
        let old = bridge
        bridge = rebuild(clean)
        let fresh = bridge, wasStarted = started
        Task {
            await old.stop()
            if wasStarted { await fresh.start() }
        }
    }

    // MARK: state

    func receive(_ s: MacBridgeState) {
        let terminalChanged = (state.terminal == nil) != (s.terminal == nil)
        state = s
        config.mode = s.mode
        if terminalChanged { syncHotkey() }
        onStateChange?(s)
        if liveViewName == nil {
            let b = bridge
            Task { [weak self] in
                if let id = await b.registeredNodeId { self?.liveViewName = MacScreenName.agent(nodeId: id) }
            }
        }
        guard postsNotifications else { return }
        let asking = Set(s.controlAsks)
        for desk in asking.subtracting(controlBannered) where !(s.control?.covers(desk, now: Date().timeIntervalSince1970) ?? false) {
            MacControlNotifications.post(desk: desk)
        }
        for desk in controlBannered.subtracting(asking) { MacControlNotifications.withdraw(desk: desk) }
        controlBannered = asking
        let waiting = Set(s.pendingGrants.map(\.desk))
        for request in s.pendingGrants where !bannered.contains(request.desk) {
            MacGrantNotifications.post(request)
        }
        for desk in bannered.subtracting(waiting) { MacGrantNotifications.withdraw(desk: desk) }
        bannered = waiting
    }
}
#endif

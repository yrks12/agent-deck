import SwiftUI
import DeckKit

@main
struct AgentDeckPhoneApp: App {
    /// Only for the orientation mask: the agent's screen may turn, nothing else does.
    @UIApplicationDelegateAdaptor(PhoneAppDelegate.self) private var appDelegate
    @StateObject private var store: PhoneStore
    /// The app's one call: it outlives every screen.
    @StateObject private var calls: PhoneCallCenter
    /// Voice messages and spoken replies.
    @StateObject private var voice: PhoneVoice
    @Environment(\.scenePhase) private var scenePhase

    init() {
        let store = PhoneStore()
        _store = StateObject(wrappedValue: store)
        let calls = PhoneCalls.make(store: store)
        let voice = PhoneVoice(store: store)
        voice.bindBusy(to: calls.session)
        _calls = StateObject(wrappedValue: calls)
        _voice = StateObject(wrappedValue: voice)
        if !isHostingTests { PhoneAlerts.shared.install() }
    }

    /// Hosting the render tests must not open a connection to anything.
    private let isHostingTests = ProcessInfo.processInfo.environment["XCTestConfigurationFilePath"] != nil

    /// Simulator screenshots of the agent's screen, on a drawn page (debug only).
    private var isScreenDemo: Bool {
        #if DEBUG
        ScreenDemo.isOn
        #else
        false
        #endif
    }

    var body: some Scene {
        WindowGroup {
            if isHostingTests {
                Color.black.ignoresSafeArea()
            } else if isScreenDemo {
                #if DEBUG
                ScreenDemo.root
                #endif
            } else {
                RootView()
                    .phoneCalls(calls, agents: store.agents)
                    .environment(\.phoneCalls, calls)
                    .environment(\.phoneVoice, voice)
                    .environmentObject(store)
                    .onAppear { store.start(); PhoneAlerts.shared.start() }
                    .onOpenURL { PhoneAlerts.shared.open($0) }
            }
        }
        .onChange(of: scenePhase) { _, phase in
            PhoneAlerts.shared.scenePhase(phase)
            store.scene(phase)
        }
    }
}

/// Connect until there is a deck; then the two tabs.
struct RootView: View {
    @EnvironmentObject private var store: PhoneStore
    @ObservedObject private var alerts = PhoneAlerts.shared
    @Environment(\.phoneCalls) private var calls
    enum Tab: Hashable { case agents, attention }
    @State private var tab: Tab = .agents
    @State private var agentsPath = NavigationPath()
    @State private var attentionPath = NavigationPath()
    /// Launch-time deep link for demos: `DECK_OPEN_THREAD=direct:chief`.
    private let openOnLaunch = ProcessInfo.processInfo.environment["DECK_OPEN_THREAD"]

    var body: some View {
        switch store.phase {
        case .connect:
            NavigationStack { ConnectView() }
        case .live:
            TabView(selection: $tab) {
                NavigationStack(path: $agentsPath) {
                    RosterView().withThreadDestination(store: store)
                }
                .tabItem { Label("Agents", systemImage: "person.2.fill") }
                .tag(Tab.agents)

                NavigationStack(path: $attentionPath) {
                    AttentionView(path: $attentionPath).withThreadDestination(store: store)
                }
                .tabItem { Label("Attention", systemImage: "exclamationmark.bubble.fill") }
                .badge(store.attention.count)
                .tag(Tab.attention)
            }
            .tint(.primary)
            // A desk calling him: full screen while it rings (`IncomingCallView`).
            .modifier(IncomingCallChrome(alerts: alerts, calls: calls, agents: store.agents))
            .alert("Couldn't do that", isPresented: Binding(
                get: { store.actionError != nil }, set: { if !$0 { store.actionError = nil } })) {
                Button("OK", role: .cancel) {}
            } message: { Text(store.actionError ?? "") }
            // A tapped notification or ntfy link: that desk's thread, on top.
            .onChange(of: alerts.route) { _, route in follow(route) }
            .task {
                follow(alerts.route)  // a cold launch from a tap
                if let id = openOnLaunch, agentsPath.isEmpty {
                    let name = ThreadID.desk(ofDirect: id) ?? id
                    agentsPath.append(ThreadRoute(threadID: id, agent: name))
                }
            }
        }
    }
}

extension RootView {
    private func follow(_ route: AlertRoute?) {
        guard let route else { return }
        tab = .agents
        agentsPath = NavigationPath([ThreadRoute(threadID: route.threadID, agent: route.agent)])
        alerts.route = nil
    }
}

extension View {
    func withThreadDestination(store: PhoneStore) -> some View {
        navigationDestination(for: ThreadRoute.self) { route in
            ThreadScreen(route: route, model: ThreadModel(
                threadID: route.threadID, client: store.client, decisions: store.decisions,
                demoReadOnly: store.isReadOnly,
                unread: store.row(forAgent: route.agent)?.unreadCount ?? 0,
                uploader: store.attachmentClient,
                displayName: { store.agents[$0]?.displayName ?? Agent.displayName(forWireName: $0) }))
            .onAppear { PhoneAlerts.shared.opened(thread: route.threadID) }
            .onDisappear { PhoneAlerts.shared.closed(thread: route.threadID) }
        }
    }
}

/// The call center, for the screens that place calls. Optional so previews
/// and renders draw without one.
private struct PhoneCallsKey: EnvironmentKey {
    static let defaultValue: PhoneCallCenter? = nil
}

extension EnvironmentValues {
    var phoneCalls: PhoneCallCenter? {
        get { self[PhoneCallsKey.self] }
        set { self[PhoneCallsKey.self] = newValue }
    }
}

extension PhoneStore {
    /// Call on a desk, from its thread or its row.
    func placeCall(_ calls: PhoneCallCenter?, agent name: String) {
        guard let calls else { return }
        guard !isReadOnly else { actionError = "Read-only demo: no call was placed."; Haptics.warning(); return }
        let agent = agents[name]
        Haptics.tap()
        Task { await calls.call(desk: name, name: agent?.displayName ?? Agent.displayName(forWireName: name),
                                voice: agent?.voice) }
    }
}

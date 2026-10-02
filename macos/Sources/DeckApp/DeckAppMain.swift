import SwiftUI
import AppKit
import Combine
import ServiceManagement
import DeckKit
import DeckUI

/// Which deck the app talks to.
///
/// The deck on this Mac by default. `DECK_URL` names a different one and
/// `DECK_FIXTURE=1` asks for the mock -- neither is something you can land on
/// by accident, because an app opened from the Dock carries no environment.
private enum Backend {
    /// Decided in DeckKit so it can be tested; see `DeckEndpoint`.
    ///
    /// Read each time, not once at launch: pairing from the Connect screen
    /// changes the saved address while the app is running.
    static var choice: DeckEndpoint.Choice {
        DeckEndpoint.resolve(
            environment: ProcessInfo.processInfo.environment,
            // The Base URL typed into Settings or saved by the Connect screen.
            stored: UserDefaults.standard.string(forKey: "deckBaseURL"))
    }

    /// The saved connection, whose TLS pin the live clients carry.
    static let connection = DeckConnection(suite: StateSuite())

    /// Changes when the deck the app should talk to changes (address or pin).
    static var identity: String {
        guard case .live(let url) = choice else { return "fixture" }
        return url.absoluteString + "|" + (connection.savedPin(for: url) ?? "")
    }

    static func make() -> DeckClient {
        switch choice {
        case .live(let url):
            return connection.client(for: url)
        case .fixture:
            // DECK_SCREEN_DEMO=1: a drawn page on the agent's screen (docs screenshots).
            return FixtureDeckClient(
                demoScreenPage: ProcessInfo.processInfo.environment["DECK_SCREEN_DEMO"] == "1")
        }
    }

    /// `POST /v1/agents/{name}/terminal`, over the same deck and the same
    /// token. Built here rather than cast out of the client because
    /// `HTTPDeckClient`'s URL, token store and performer are all `private` —
    /// which in Swift means its own file — so nothing outside that file can
    /// extend it to answer this route. Nil for the fixture: it answers the
    /// terminal route itself.
    static func makeShell() -> DeskShellClient? {
        switch choice {
        case .live(let url):
            return connection.shell(for: url)
        case .fixture:
            return nil
        }
    }
}

/// The deck window. Pairing from the Connect screen swaps the live client (new
/// address, new pin) by rebuilding the window's store; `--pair <CODE>` on the
/// command line opens the Connect screen with that code filled in, unsent.
private struct DeckWindow: View {
    @State private var built = Backend.identity
    @State private var generation = 0
    @State private var connecting = LaunchPairing.code(from: CommandLine.arguments) != nil

    var body: some View {
        DeckRootView(client: Backend.make(), shell: Backend.makeShell())
            .id(generation)
            .onReceive(NotificationCenter.default.publisher(for: .deckCredentialsChanged)) { _ in
                guard Backend.identity != built else { return }
                built = Backend.identity
                generation += 1
            }
            .sheet(isPresented: $connecting) {
                ConnectDeckView(form: ConnectForm(launchArguments: CommandLine.arguments),
                                deck: Backend.connection,
                                onDone: { connecting = false })
            }
    }
}

/// The Mac bridge (docs/mac-bridge.md): desks on the deck run jobs on this
/// Mac, under his grant. Only the direct build carries it -- the App Store
/// build's App Sandbox cannot run a login shell -- and only against the live
/// deck, never the fixture.
@MainActor
final class MacAccess: ObservableObject {
    static let shared = MacAccess()

    #if DECK_APPSTORE
    static let channel: MacBridgeHost.Channel = .appStore
    #else
    static let channel: MacBridgeHost.Channel = .direct
    #endif

    let host: MacBridgeHost?
    private var relay: AnyCancellable?
    /// The banner and the "his hands win" monitor, only while control is on.
    private let chrome = MacControlChrome()

    private init() {
        if case .live = Backend.choice {
            host = MacBridgeHost.live(channel: Self.channel)
        } else {
            host = nil
        }
        relay = host?.objectWillChange.sink { [weak self] _ in self?.objectWillChange.send() }
        host?.onStateChange = { [weak self] s in
            self?.chrome.update(state: s, onStop: { self?.host?.stopAll() })
        }
    }

    /// Opens a desk-style screen window. Set by the main window.
    var openScreen: (ExpandedScreen) -> Void = { _ in }

    /// Asks macOS first (so Agent Deck is in the list), then opens the pane.
    func openPrivacyPane(_ pane: String) {
        MacPermissionRequest.live.request(pane)
    }

    var state: MacBridgeState { host?.state ?? MacBridgeState(mode: .off) }

    /// Opens the Activity window. Set by the main window, which has the
    /// `openWindow` action a scene builder does not.
    var openActivity: () -> Void = {}
}

/// "Waiting for keychain access…" while a keychain read is stuck on a dialog
/// (a rebuilt app, a locked screen): the window says what to do instead of
/// looking dead. Event-driven -- no polling.
private struct KeychainWaitingBanner: View {
    @State private var waiting = KeychainReadGate.shared.isWaiting

    var body: some View {
        Group {
            if waiting {
                VStack(alignment: .leading, spacing: 4) {
                    Text(KeychainReadGate.waitingText).font(.headline)
                    Text(KeychainReadGate.waitingHint).font(.callout).foregroundStyle(.secondary)
                }
                .padding(12)
                .frame(maxWidth: 460, alignment: .leading)
                .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                .padding(12)
            }
        }
        .onReceive(NotificationCenter.default.publisher(for: KeychainReadGate.waitingChanged)) { _ in
            waiting = KeychainReadGate.shared.isWaiting
        }
    }
}

/// His card and the in-use bar, over whatever the window shows. Absent when
/// nothing waits and nothing runs.
private struct MacAccessOverlay: View {
    @ObservedObject var access: MacAccess
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        ZStack(alignment: .top) {
            Color.clear.frame(height: 0)
                .onAppear {
                    access.openActivity = { openWindow(id: "mac-activity") }
                    access.openScreen = { openWindow(value: $0) }
                }
            VStack(spacing: 0) {
                KeychainWaitingBanner()
                content
            }
        }
    }

    @ViewBuilder private var content: some View {
        if let host = access.host, !(host.state.pendingGrants.isEmpty && host.state.running.isEmpty
                                     && pendingControl(host).isEmpty) {
            VStack(spacing: 10) {
                MacInUseBar(state: host.state, onStop: { host.stopDesk($0) })
                ForEach(pendingControl(host), id: \.self) { desk in
                    MacControlAskCard(desk: desk, onAllow: { host.startControl(scope: desk) })
                        .frame(maxWidth: 460)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                        .shadow(radius: 8)
                }
                ForEach(host.state.pendingGrants, id: \.desk) { request in
                    MacGrantCardView(request: request, onDecide: { host.answer(desk: request.desk, $0) })
                        .frame(maxWidth: 460)
                        .background(.regularMaterial, in: RoundedRectangle(cornerRadius: 12))
                        .shadow(radius: 8)
                }
            }
            .padding(12)
        }
    }
}

/// Desks waiting on his Allow that the live grant does not already cover.
@MainActor private func pendingControl(_ host: MacBridgeHost) -> [String] {
    let now = Date().timeIntervalSince1970
    return host.state.controlAsks.filter { !(host.state.control?.covers($0, now: now) ?? false) }
}

/// "Atlas wants to control this Mac": the in-window twin of the banner.
private struct MacControlAskCard: View {
    let desk: String
    let onAllow: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(MacControlCopy.askTitle(desk: desk)).font(.headline)
            Text(MacControlCopy.askBody).font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack {
                Spacer()
                Button(MacControlCopy.allowTitle, action: onAllow).keyboardShortcut(.defaultAction)
            }
        }
        .padding(14)
    }
}

/// Settings → Mac. Without a bridge it says why, in one sentence.
private struct MacSettingsTab: View {
    @ObservedObject var access: MacAccess
    @AppStorage("macKeepInMenuBar") private var keepInMenuBar = true
    @State private var draftName: String?
    @Environment(\.openWindow) private var openWindow

    var body: some View {
        if let host = access.host {
            MacAccessSettingsView(
                config: Binding(get: { host.config }, set: { host.setPolicy($0) }),
                macName: Binding(get: { draftName ?? host.macName }, set: { draftName = $0 }),
                keepInMenuBar: $keepInMenuBar,
                startAtLogin: Binding(get: { SMAppService.mainApp.status == .enabled },
                                      set: { on in try? on ? SMAppService.mainApp.register() : SMAppService.mainApp.unregister() }),
                state: host.state, deckHost: host.deckHost,
                actions: MacAccessSettingsActions(
                    revoke: { host.answer(desk: $0, .revoke) },
                    openActivity: { openWindow(id: "mac-activity") },
                    openFullDiskAccess: {
                        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles") {
                            NSWorkspace.shared.open(url)
                        }
                    },
                    startControl: { host.startControl(scope: $0) },
                    stopControl: { host.stopControl() },
                    openPrivacyPane: { access.openPrivacyPane($0) },
                    openLiveView: host.liveViewName.map { name in
                        { access.openScreen(ExpandedScreen(desk: name, displayName: host.macName)) }
                    },
                    permissions: {
                        MacPermissions(accessibility: MacInputInjector.accessibilityGranted,
                                       screenRecording: MacScreenStreamer.screenRecordingGranted)
                    }))
            .onDisappear { if let name = draftName { host.rename(name) } }
        } else {
            Text(MacAccess.channel == .appStore
                 ? "This copy of Shaliach is from the App Store, which cannot let desks use this Mac. The direct download can."
                 : "Connect to your deck first; then desks can ask to use this Mac.")
                .padding(24)
        }
    }
}

/// A SwiftPM executable has no bundle, so nothing else asks AppKit for a real
/// window and a Dock icon.
final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        NSApp.activate(ignoringOtherApps: true)
        // Silent and free unless DECK_DIAGNOSE=1. See Diagnostics.
        Diagnostics.startReporting()
        // Quitting mid "Sign in on this Mac" closes that Chrome and wipes its
        // signed-in profile (SignInWindows).
        SignInWindows.closeAllOnTerminate()
        // The bridge dials out from here on; nothing runs once the app quits.
        MainActor.assumeIsolated { MacAccess.shared.host?.start() }
    }

    /// While Mac access is on the menu-bar extra keeps the app (and the
    /// bridge) alive; Off is today's behaviour.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        MainActor.assumeIsolated { !(MacAccess.shared.host?.keepsAppAlive ?? false) }
    }

    /// Quit kills every Mac job and reports it before the app goes.
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let host = MainActor.assumeIsolated({ MacAccess.shared.host }) else { return .terminateNow }
        Task { @MainActor in
            await host.stop()
            NSApp.reply(toApplicationShouldTerminate: true)
        }
        return .terminateLater
    }
}

@main
struct DeckAppMain: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @ObservedObject private var mac = MacAccess.shared
    @AppStorage("macKeepInMenuBar") private var keepInMenuBar = true

    var body: some Scene {
        WindowGroup {
            DeckWindow()
                .frame(minWidth: 900, minHeight: 560)
                .navigationTitle(DeckEndpoint.windowTitle(Backend.choice,
                                                          environment: ProcessInfo.processInfo.environment))
                .overlay(alignment: .top) { MacAccessOverlay(access: mac) }
                .deckThemed()
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }

        // Expand, from the take-over: the agent's screen in its own window.
        WindowGroup("Agent screen", for: ExpandedScreen.self) { $screen in
            if let screen {
                ExpandedScreenWindow(screen: screen, client: Backend.make())
                    .frame(minWidth: 640, minHeight: 420)
                    .deckThemed()
            }
        }
        // ~90% of the screen (`ComputerLayout`): the point is to see what the
        // agent does. Frames saved under the old name were 1280x840.
        .defaultSize(ComputerLayout.defaultWindowSize(
            visibleFrame: NSScreen.main?.visibleFrame.size ?? CGSize(width: 1440, height: 900)))

        Window("Mac Activity", id: "mac-activity") {
            MacActivityView(rows: mac.state.recent, runningIds: Set(mac.state.running.map(\.id)))
                .frame(minWidth: 520, minHeight: 360)
        }

        MacMenuBarScene(
            state: mac.state,
            actions: MacMenuBarActions(
                stopJob: { id in mac.host?.stopJob(id) },
                pause: { mac.host?.pause() },
                resume: { mac.host?.resume() },
                openActivity: { mac.openActivity() },
                quit: { NSApp.terminate(nil) },
                stopControl: { mac.host?.stopControl() }),
            isInserted: Binding(get: { mac.host != nil && keepInMenuBar && mac.state.mode != .off },
                                set: { keepInMenuBar = $0 }))

        Settings {
            TabView {
                DeckSettingsView().tabItem { Text("Deck") }
                MacSettingsTab(access: mac).tabItem { Text("Mac") }
            }
            .deckThemed()
        }
    }
}

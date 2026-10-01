import SwiftUI
import AppKit
import DeckKit
import DeckUI

/// Which deck the app talks to.
///
/// The deck on this Mac by default. `DECK_URL` names a different one and
/// `DECK_FIXTURE=1` asks for the mock -- neither is something you can land on
/// by accident, because an app opened from the Dock carries no environment.
private enum Backend {
    /// Decided in DeckKit so it can be tested; see `DeckEndpoint`.
    static let choice = DeckEndpoint.resolve(
        environment: ProcessInfo.processInfo.environment,
        // The Base URL typed into Settings. It was written and never read, so
        // the field looked like the place you point the app and was inert.
        stored: UserDefaults.standard.string(forKey: "deckBaseURL"))

    static func make() -> DeckClient {
        switch choice {
        case .live(let url):
            return HTTPDeckClient(baseURL: url, tokens: KeychainTokenStore())
        case .fixture:
            return FixtureDeckClient()
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
            return HTTPDeskShell(baseURL: url, tokens: KeychainTokenStore())
        case .fixture:
            return nil
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
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

@main
struct DeckAppMain: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        WindowGroup {
            DeckRootView(client: Backend.make(), shell: Backend.makeShell())
                .frame(minWidth: 900, minHeight: 560)
                .navigationTitle(Backend.choice.windowTitle)
        }
        .windowToolbarStyle(.unified)
        .commands {
            CommandGroup(replacing: .newItem) {}
        }

        Settings {
            DeckSettingsView()
        }
    }
}

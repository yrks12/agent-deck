import SwiftUI
import DeckKit
#if os(macOS)
import AppKit
#endif

/// **OAuth Connect, one sign-in at a time.**
///
/// `connect` asks the deck for a consent URL, holds the loopback port
/// (`OAuthLoopbackListener`), and only then opens his browser. Three things
/// can end the wait, whichever comes first: the browser landing on the
/// loopback, an address pasted into the field, or the status poll seeing the
/// sign-in finished somewhere else. The deadline is the deck's `expires_at`.
///
/// The provider's tokens never reach this app. The callback's code and state
/// pass through once, to `connect/complete`, and are not kept or shown.
@MainActor
final class StoreConnectModel: ObservableObject {
    enum Stage: Equatable {
        case idle
        case starting
        /// `listening` is false when the port was busy: paste is the way in.
        case waiting(listening: Bool)
        case completing
        /// The install answer when this app completed it; nil when the
        /// status poll saw it finish elsewhere.
        case done(StoreInstallResult?)
        case failed(String)
        case expired
    }

    @Published private(set) var stage: Stage = .idle
    @Published var pasted = ""
    @Published private(set) var pasteProblem: String?

    private let client: StoreClient
    private let openURL: (URL) -> Void
    private let onInstalled: () -> Void
    private var start: StoreConnectStart?
    private var listener: OAuthLoopbackListener?
    private var tasks: [Task<Void, Never>] = []
    static var pollInterval: UInt64 = 2_000_000_000

    init(client: StoreClient, openURL: @escaping (URL) -> Void, onInstalled: @escaping () -> Void) {
        self.client = client
        self.openURL = openURL
        self.onInstalled = onInstalled
    }

    var isWaiting: Bool { if case .waiting = stage { return true } else { return false } }

    func connect(id: String, desks: StoreDesks) async {
        cancel()
        stage = .starting
        pasteProblem = nil
        let start: StoreConnectStart
        do {
            start = try await client.storeConnect(id: id, desks: desks)
        } catch {
            stage = .failed(StorePanelModel.deckError(error).userFacingText)
            return
        }
        self.start = start
        let deadline = start.expiresDate ?? Date().addingTimeInterval(600)
        let listener = OAuthLoopbackListener(
            port: OAuthLoopback.port(fromRedirectURI: start.redirectURI) ?? OAuthLoopback.defaultPort)
        var listening = true
        do { try await listener.start() } catch { listening = false }
        self.listener = listening ? listener : nil
        stage = .waiting(listening: listening)
        openURL(start.authorizeURL)

        if listening {
            tasks.append(Task { [weak self] in
                do {
                    let url = try await listener.callback(until: deadline)
                    await self?.complete(url.absoluteString)
                } catch OAuthLoopbackError.timedOut {
                    self?.expireIfWaiting()
                } catch {}
            })
        }
        let state = start.state
        tasks.append(Task { [weak self] in await self?.poll(state: state, until: deadline) })
    }

    func reopenBrowser() {
        if let url = start?.authorizeURL { openURL(url) }
    }

    func submitPasted() async {
        guard let url = OAuthLoopback.pastedCallbackURL(pasted) else {
            pasteProblem = "That isn't the address a sign-in ends on. It starts with http://127.0.0.1 and has state= in it."
            return
        }
        pasted = ""
        pasteProblem = nil
        await complete(url)
    }

    func cancel() {
        tasks.forEach { $0.cancel() }
        tasks = []
        listener?.cancel()
        listener = nil
        pasted = ""
    }

    private func expireIfWaiting() {
        if isWaiting { stage = .expired; cancel() }
    }

    private func complete(_ callbackURL: String) async {
        guard isWaiting else { return }
        stage = .completing
        listener?.cancel()
        do {
            let result = try await client.storeConnectComplete(callbackURL: callbackURL)
            stage = .done(result)
            onInstalled()
        } catch {
            stage = .failed(StorePanelModel.deckError(error).userFacingText)
        }
        cancel()
    }

    private func poll(state: String, until deadline: Date) async {
        while !Task.isCancelled {
            try? await Task.sleep(nanoseconds: Self.pollInterval)
            guard !Task.isCancelled, isWaiting else { return }
            if Date() >= deadline { return expireIfWaiting() }
            guard let status = try? await client.storeConnectStatus(state: state), isWaiting else { continue }
            switch status.phase {
            case .waiting: continue
            case .done:
                stage = .done(nil)
                onInstalled()
                cancel()
                return
            case .failed(let text):
                stage = .failed(text)
                cancel()
                return
            case .expired:
                return expireIfWaiting()
            }
        }
    }

    #if os(macOS)
    static func openInBrowser(_ url: URL) { NSWorkspace.shared.open(url) }
    #endif
}

/// The Connect control: the button, the wait, the paste fallback, the result.
struct StoreConnectSection: View {
    @ObservedObject var connect: StoreConnectModel
    /// Nil while the draft is not ready (no desk chosen, warning unconfirmed).
    let desks: StoreDesks?
    let problem: String?
    let itemID: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            switch connect.stage {
            case .idle, .failed, .expired:
                if case .failed(let text) = connect.stage {
                    FlatLabel(text, systemImage: "exclamationmark.triangle")
                        .font(.callout).foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if case .expired = connect.stage {
                    Text("The sign-in expired before it finished. Press Connect to start again.")
                        .font(.callout).foregroundStyle(.secondary)
                }
                HStack(spacing: 10) {
                    Button {
                        guard let desks else { return }
                        Task { await connect.connect(id: itemID, desks: desks) }
                    } label: {
                        Text("Connect").frame(minWidth: 80)
                    }
                    .buttonStyle(.deckPrimary)
                    .disabled(desks == nil)
                    Text(problem ?? "Opens the sign-in page in your browser.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            case .starting:
                progress("Starting the sign-in")
            case .waiting(let listening):
                progress("Waiting for you to finish signing in in your browser")
                if !listening {
                    Text("This Mac couldn't listen for the browser's answer (the port is busy). When the browser stops on an address starting with http://127.0.0.1, paste it below.")
                        .font(.caption).foregroundStyle(.orange)
                        .fixedSize(horizontal: false, vertical: true)
                }
                pasteField
                HStack(spacing: 12) {
                    Button("Open the sign-in page again") { connect.reopenBrowser() }
                        .buttonStyle(.link)
                    Button("Cancel") { connect.cancel(); }
                        .buttonStyle(.link)
                }
                .font(.caption)
            case .completing:
                progress("Finishing the install")
            case .done(let result):
                FlatLabel("Connected", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.callout.weight(.medium))
                if let result {
                    ForEach(result.reload, id: \.desk) { reload in
                        Text(StoreBrowser.reloadLine(reload)).font(.callout).foregroundStyle(.secondary)
                    }
                } else {
                    Text("Finished in another window. The desks pick it up as they reload.")
                        .font(.callout).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func progress(_ text: String) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(text).font(.callout).foregroundStyle(.secondary)
        }
    }

    private var pasteField: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                TextField("Paste the address your browser ended on", text: $connect.pasted)
                    .deckField()
                    .accessibilityLabel("Paste the address your browser ended on")
                    .onSubmit { Task { await connect.submitPasted() } }
                Button("Finish") { Task { await connect.submitPasted() } }
                    .disabled(connect.pasted.trimmingCharacters(in: .whitespaces).isEmpty)
            }
            if let problem = connect.pasteProblem {
                Text(problem).font(.caption).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
    }
}

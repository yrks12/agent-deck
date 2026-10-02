import SwiftUI
import DeckKit
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// Signing in to a Claude account, drawn once for both apps (compiled into the
// iPhone target by `ios/project.yml`). The flow itself is
// `AccountLoginModel` in DeckKit; these draw its states in the shared palette.

/// Which account to sign in: an existing one (sign in again) or a new one.
struct AccountSignInRequest: Identifiable, Equatable {
    let id: String
    let label: String
}

/// The system browser and clipboard, one place for each platform.
enum AccountBrowser {
    static func open(_ url: URL) {
        #if os(macOS)
        NSWorkspace.shared.open(url)
        #else
        UIApplication.shared.open(url)
        #endif
    }

    static var clipboardText: String? {
        #if os(macOS)
        NSPasteboard.general.string(forType: .string)
        #else
        UIPasteboard.general.string
        #endif
    }
}

/// **The sign-in sheet**: the browser opens on Claude's page, then "Paste the
/// code Claude shows you", then it polls until the deck says done.
struct AccountSignInSheet: View {
    @StateObject private var model: AccountLoginModel
    let dismiss: () -> Void
    @State private var code = ""

    init(client: AccountLoginClient, request: AccountSignInRequest,
         onDone: @escaping () -> Void, dismiss: @escaping () -> Void) {
        _model = StateObject(wrappedValue: AccountLoginModel(
            client: client, id: request.id, label: request.label,
            openURL: { AccountBrowser.open($0) }, onDone: onDone))
        self.dismiss = dismiss
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Sign in: \(model.label)").font(.headline)
            content
        }
        .padding(20)
        #if os(macOS)
        .frame(width: 380)
        #endif
        .background(DeckPalette.canvas)
        .task { await model.start() }
    }

    @ViewBuilder
    private var content: some View {
        switch model.state {
        case .idle, .starting:
            progress("Asking the deck for a sign-in link…")
        case .awaitingCode(let problem):
            VStack(alignment: .leading, spacing: 10) {
                Text("Claude opened in your browser. Sign in there, then:")
                    .font(.subheadline).foregroundStyle(.secondary)
                Text("Paste the code Claude shows you").font(.callout.weight(.semibold))
                HStack(spacing: 8) {
                    TextField("Code", text: $code)
                        .deckField()
                        .autocorrectionDisabledForCode()
                        .accessibilityLabel("Code from Claude")
                        .onSubmit { Task { await model.submit(code: code) } }
                    Button("Paste from clipboard") { code = AccountBrowser.clipboardText ?? code }
                        .buttonStyle(.deckSecondary)
                }
                if let problem {
                    HStack(alignment: .top, spacing: 6) {
                        Circle().fill(DeckPalette.waiting).frame(width: 7, height: 7).padding(.top, 5)
                        Text(problem).font(.caption).fixedSize(horizontal: false, vertical: true)
                    }
                    .accessibilityElement(children: .combine)
                }
                HStack {
                    Button("Open Claude again") { model.reopenBrowser() }.buttonStyle(.deckSecondary)
                    Spacer()
                    Button("Cancel", action: dismiss).buttonStyle(.deckSecondary)
                    Button("Submit") { Task { await model.submit(code: code) } }
                        .buttonStyle(.deckPrimary)
                        .disabled(code.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        case .verifying:
            progress("Checking the code…")
        case .done:
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    Text("✓").font(.title2.weight(.bold)).foregroundStyle(DeckPalette.working)
                    Text("Signed in to \(model.label)").font(.callout.weight(.semibold))
                }
                .accessibilityElement(children: .combine)
                Button("Done", action: dismiss).buttonStyle(.deckPrimary)
            }
        case .failed(let text):
            retryBlock(text)
        case .expired:
            retryBlock(AccountLoginError.expired.userFacingText)
        }
    }

    private func progress(_ text: String) -> some View {
        HStack(spacing: 10) {
            ProgressView().controlSize(.small)
            Text(text).font(.callout).foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
    }

    private func retryBlock(_ text: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .top, spacing: 6) {
                Circle().fill(DeckPalette.waiting).frame(width: 8, height: 8).padding(.top, 5)
                Text(text).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
            .accessibilityElement(children: .combine)
            HStack {
                Spacer()
                Button("Close", action: dismiss).buttonStyle(.deckSecondary)
                Button("Try again") { code = ""; Task { await model.retry() } }.buttonStyle(.deckPrimary)
            }
        }
    }
}

private extension View {
    /// A one-time code is not prose: no autocorrect, no capitalising.
    func autocorrectionDisabledForCode() -> some View {
        #if os(macOS)
        autocorrectionDisabled()
        #else
        autocorrectionDisabled().textInputAutocapitalization(.never)
        #endif
    }
}

/// **"Claude accounts"** in Settings: each account with "Sign in again", and
/// "Add account". Hidden by its caller on a deck that does not serve accounts.
struct ClaudeAccountsList: View {
    let usage: ClaudeUsage
    let begin: (AccountSignInRequest) -> Void
    @State private var adding = false
    @State private var newLabel = ""

    private var newID: String? {
        AccountSignIn.newID(for: newLabel, existing: usage.accounts.map(\.id))
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(usage.accounts) { account in
                HStack(alignment: .center, spacing: 8) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(account.label).font(.callout.weight(.semibold)).lineLimit(1)
                        Text(account.plan ?? account.kind.capitalized)
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 6)
                    Button("Sign in again") {
                        begin(AccountSignInRequest(id: account.id, label: account.label))
                    }
                    .buttonStyle(.deckSecondary)
                    .accessibilityLabel("Sign in again to \(account.label)")
                }
            }
            if adding {
                TextField("Account name, for example Work", text: $newLabel)
                    .deckField()
                    .accessibilityLabel("New account name")
                HStack {
                    Spacer()
                    Button("Cancel") { adding = false; newLabel = "" }.buttonStyle(.deckSecondary)
                    Button("Continue") {
                        guard let id = newID else { return }
                        let label = newLabel.trimmingCharacters(in: .whitespacesAndNewlines)
                        adding = false
                        newLabel = ""
                        begin(AccountSignInRequest(id: id, label: label))
                    }
                    .buttonStyle(.deckPrimary)
                    .disabled(newID == nil)
                }
            } else {
                Button("Add account") { adding = true }.buttonStyle(.deckSecondary)
            }
        }
    }
}

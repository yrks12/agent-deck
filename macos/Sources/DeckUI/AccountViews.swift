import SwiftUI
import DeckKit

// The two-accounts surfaces, drawn once for both apps: compiled into DeckUI
// for the Mac and into the iPhone target (`ios/project.yml`). Every decision
// (who gets a badge, what is disabled and why, when a banner shows) is
// `AccountsPresentation`'s in DeckKit; these only draw it, in the shared
// palette. No system blue and no stock controls.

/// Which account a desk is on, beside its title.
struct AccountBadgeChip: View {
    let text: String

    var body: some View {
        Text(text)
            .font(.caption2.weight(.semibold))
            .lineLimit(1)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .overlay(Capsule().strokeBorder(DeckPalette.cardStroke, lineWidth: 1))
            .foregroundStyle(.secondary)
            .accessibilityLabel("Account: \(text)")
    }
}

/// **"Move to account…"**, for a row's context menu (right-click on the Mac,
/// long-press on the phone). Draws nothing on a deck with fewer than two
/// accounts. A desk that is not idle gets the menu with every choice off and
/// the reason as its first line, so it is never a silently dead control.
struct MoveToAccountMenu: View {
    let agent: Agent
    let usage: ClaudeUsage?
    let move: (String) -> Void

    var body: some View {
        let options = AccountsPresentation.moveOptions(
            for: agent, accounts: usage?.accounts ?? [], defaultID: usage?.policy?.defaultAccount)
        if !options.isEmpty {
            Menu("Move to account…") {
                if let reason = AccountsPresentation.moveDisabledReason(for: agent) {
                    Text(reason)
                    Divider()
                }
                ForEach(options) { option in
                    Button(option.isCurrent ? "\(option.label) (current)" : option.label) {
                        move(option.id)
                    }
                    .disabled(!option.isEnabled)
                }
            }
        }
    }
}

/// A move in flight, or the deck's reason it did not work, under the desk it
/// was about. Tapping the problem dismisses it.
struct AccountMoveNotice: View {
    let status: AccountMoveStatus
    let dismiss: () -> Void

    var body: some View {
        switch status {
        case .moving:
            Text("Moving to the other account…")
                .font(.caption).foregroundStyle(.secondary)
                .accessibilityLabel("Moving this desk to the other account")
        case .failed(let text):
            Button(action: dismiss) {
                HStack(alignment: .top, spacing: 6) {
                    Circle().fill(DeckPalette.waiting).frame(width: 7, height: 7).padding(.top, 4)
                    Text(text).font(.caption).multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Could not move: \(text)")
            .accessibilityHint("Dismiss")
        }
    }
}

/// One line per account whose login runs out within three days (or already
/// has). Only he can sign in again, so it says the command.
struct AccountExpiryBanner: View {
    let warnings: [AccountsPresentation.ExpiryWarning]
    /// "Sign in again" for that account; `nil` hides the button (a deck that
    /// cannot sign in from the app).
    var onSignIn: ((AccountSignInRequest) -> Void)?

    var body: some View {
        if !warnings.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(warnings) { warning in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .top, spacing: 10) {
                            Circle().fill(DeckPalette.waiting).frame(width: 9, height: 9).padding(.top, 4)
                            Text(warning.text)
                                .font(.subheadline.weight(.semibold))
                                .fixedSize(horizontal: false, vertical: true)
                            Spacer(minLength: 0)
                        }
                        .accessibilityElement(children: .combine)
                        if let onSignIn {
                            Button("Sign in again") {
                                onSignIn(AccountSignInRequest(id: warning.accountID, label: warning.label))
                            }
                            .buttonStyle(.deckSecondary)
                            .accessibilityLabel("Sign in again to \(warning.label)")
                        }
                    }
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)
            .frame(maxWidth: .infinity, alignment: .leading)
            .card(radius: CGFloat(DeckTokens.bannerRadius))
        }
    }
}

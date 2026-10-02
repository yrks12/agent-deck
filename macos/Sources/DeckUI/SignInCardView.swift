import SwiftUI
import DeckKit

/// **One waiting thing, drawn the same everywhere on the Mac.**
///
/// Owner: *"Don't have the login on the screen, only on the side?"* The side
/// pane drew "Use my Chrome login" and "Sign in fresh with passkey"; the desk's
/// own thread drew nothing. This is the one card both places draw now, and its
/// buttons come from `SignInCard.actions` — the same ordered list the phone
/// draws — so the thread, the side pane and the phone cannot drift apart.
///
/// A value, never an observer (see `AttentionTrayView` rule 3): every input is
/// a plain value and every tap is a closure.
struct SignInCardView: View, Equatable {
    let item: AttentionItem
    /// Where the Mac sign-in for this card is, if one is under way.
    var signIn: MacSignInPhase?
    var canSignInOnMac = false
    var macBrowserLabel: String?
    var canTakeOver = false
    /// Whether to draw the "Open <desk>" link (the side pane does; the desk's
    /// own thread is already there).
    var showsOpenDesk = true
    var answer: (AttentionItem, AttentionAction) -> Void = { _, _ in }
    var signInVerb: (AttentionItem, MacSignInVerb) -> Void = { _, _ in }
    var openDesk: (String) -> Void = { _ in }
    var takeOver: (String) -> Void = { _ in }

    static func == (a: SignInCardView, b: SignInCardView) -> Bool {
        a.item == b.item && a.signIn == b.signIn && a.canSignInOnMac == b.canSignInOnMac
            && a.macBrowserLabel == b.macBrowserLabel && a.canTakeOver == b.canTakeOver
            && a.showsOpenDesk == b.showsOpenDesk
    }

    private var surface: SignInCard.Surface {
        .mac(browser: macBrowserLabel, canSignInHere: canSignInOnMac)
    }

    var actions: [SignInCardAction] {
        SignInCard.actions(for: item, surface: surface, canTakeOver: canTakeOver)
    }

    /// The button titles, in drawing order — what the tests read.
    var drawnLabels: [String] {
        actions.map { SignInCard.label($0, item: item, surface: surface) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .center, spacing: 6) {
                Text(item.deskLine)
                    .font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if item.isHandoff {
                    Text("Only you can do this")
                        .font(.caption2.weight(.semibold))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                        .accessibilityHidden(true)
                }
                Spacer(minLength: 0)
            }

            Text(item.request)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)

            if !item.whereLine.isEmpty {
                Text(item.whereLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !item.situation.isEmpty {
                FlatLabel(item.situation, systemImage: "clock.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if !item.evidence.isEmpty {
                FlatLabel(item.evidence, systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let phase = signIn {
                progress(phase)
            }
            ForEach(Array(actions.enumerated()), id: \.element.id) { index, action in
                if signIn != nil && action.method != nil {
                    EmptyView()
                } else if pairsAnswers, case .answer(let a) = action, a.verb == .handoff(.skipped) {
                    EmptyView()  // drawn in the row beside "done"
                } else if pairsAnswers, case .answer(let a) = action, a.verb == .handoff(.done) {
                    answerRow
                } else {
                    button(action, primary: index == 0)
                }
            }

            if showsOpenDesk, let label = item.openDeskLabel, let name = item.deskName {
                Button(label) { openDesk(name) }
                    .buttonStyle(.link)
                    .font(.caption)
                    .accessibilityHint("Opens that desk's conversation and its screen.")
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(item.spoken)
    }

    /// **Two answers are a row** on a plain handoff (no "Allow always"): skip
    /// first, the ordinary answer last, so the thumb lands on the one he
    /// almost always wants — and each sentence is drawn under the row.
    private var pairsAnswers: Bool {
        let verbs = Set(item.actions.map(\.verb))
        return verbs == [.handoff(.done), .handoff(.skipped)]
    }

    private var answerRow: some View {
        let pair = [AttentionAction.Verb.handoff(.skipped), .handoff(.done)]
            .compactMap { verb in item.actions.first { $0.verb == verb } }
        return VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                ForEach(pair) { action in
                    Button { answer(item, action) } label: {
                        Text(action.label)
                            .font(.subheadline.weight(.medium))
                            .lineLimit(1)
                            .minimumScaleFactor(0.85)
                            .frame(maxWidth: .infinity)
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(AttentionButtonStyle(kind: action.verb == .handoff(.done) ? .primary : .quiet))
                    .disabled(!action.isAvailable)
                    .help(action.summary)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(item.spokenAction(action))
                    .accessibilityAddTraits(.isButton)
                }
            }
            ForEach(pair) { action in
                (Text(action.label).fontWeight(.semibold)
                 + Text("  \u{2014} " + AttentionTrayView.consequence(of: action)))
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .accessibilityHidden(true)
            }
        }
    }

    private func button(_ action: SignInCardAction, primary: Bool) -> some View {
        let label = SignInCard.label(action, item: item, surface: surface)
        let hint = SignInCard.hint(action, item: item, surface: surface)
        var available = true
        if case .answer(let a) = action { available = a.isAvailable }
        return Button {
            tap(action)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(label)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(2)
                    .fixedSize(horizontal: false, vertical: true)
                Text(AttentionTrayView.consequence(of: label, hint))
                    .font(.caption2)
                    .opacity(0.7)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(AttentionButtonStyle(kind: primary ? .primary : .quiet))
        .disabled(!available)
        .help(hint)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spoken(action, label: label, hint: hint))
        .accessibilityAddTraits(.isButton)
    }

    private func spoken(_ action: SignInCardAction, label: String, hint: String) -> String {
        if case .answer(let a) = action { return item.spokenAction(a) }
        return "\(label) for \(item.deskLine). \(hint)"
    }

    private func tap(_ action: SignInCardAction) {
        switch action {
        case .chromeLogin: signInVerb(item, .useLogin)
        case .freshPasskey: signInVerb(item, .start)
        case .answer(let a): answer(item, a)
        case .takeOver: if let name = item.deskName { takeOver(name) }
        }
    }

    /// A sign-in under way on this Mac, in words.
    @ViewBuilder
    private func progress(_ phase: MacSignInPhase) -> some View {
        let host = item.signInOnMacURL?.host ?? "the site"
        switch phase {
        case .importing:
            FlatLabel("Taking your login and giving it to every desk...",
                      systemImage: "arrow.triangle.2.circlepath")
                .font(.caption)
        case .offerPasskeyInstead(let why):
            FlatLabel(why, systemImage: "info.circle")
                .font(.caption).fixedSize(horizontal: false, vertical: true)
            Button {
                signInVerb(item, .start)
            } label: {
                FlatLabel("Sign in fresh with passkey", systemImage: "person.badge.key")
            }
            .buttonStyle(.deckPrimary)
        case .waitingForYou:
            Text("Sign in to \(host) in the Chrome window that just opened (Touch ID or your iPhone), then press Share: every desk gets it.")
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 8) {
                Button("Cancel") { signInVerb(item, .cancel) }
                Button("Share") { signInVerb(item, .share) }
                    .buttonStyle(.deckPrimary)
            }
        case .sharing:
            FlatLabel("Giving the sign-in to every desk...", systemImage: "arrow.triangle.2.circlepath")
                .font(.caption)
        case .shared(let desks):
            FlatLabel(desks == 1 ? "Signed in on 1 desk." : "Signed in on \(desks) desks.",
                      systemImage: "checkmark.circle")
                .font(.caption)
        case .failed(let why):
            FlatLabel(why, systemImage: "exclamationmark.triangle")
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
            Button("Try again") { signInVerb(item, .start) }
                .font(.caption)
        }
    }
}

/// The card's two looks. The ordinary answer is a light pill with dark text -
/// the one bright thing on an orange card - and the quiet one is a dim pill, so
/// skipping is never the easy tap. Colour is never the signal: both carry their
/// words.
struct AttentionButtonStyle: ButtonStyle {
    enum Kind { case primary, quiet }
    let kind: Kind
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(kind == .primary ? Color.black.opacity(0.88) : Color.primary)
            .padding(.horizontal, 8)
            .padding(.vertical, 8)
            .background(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(kind == .primary ? Color.white.opacity(0.94) : Color.white.opacity(0.12)))
            .opacity(isEnabled ? (configuration.isPressed ? 0.75 : 1) : 0.4)
    }
}

/// What a tap on the sign-in part of a card asks for.
enum MacSignInVerb: Hashable, Sendable {
    case useLogin, start, share, cancel
}

import SwiftUI
import DeckKit

/// **"Needs your attention" — pinned above the inspector, for the whole deck.**
///
/// The inspector opens on a Name/Title/Description form, which is not what a
/// manager looks at. What he looks for is: *is anything stuck on me?* Today the
/// answer is drawn only inside the conversation that raised it, so an ask on a
/// desk he is not reading is invisible, and the agent tells him on WhatsApp
/// instead — *"agents asking me on whatsup to confirm but not on screen whats
/// the point"*.
///
/// Three rules this view exists to keep:
///
/// 1. **Nothing waiting draws nothing.** No header, no "No items", no padding.
///    A strip that is usually an empty box is a strip he stops reading, and the
///    day it is not empty he will not see it either.
/// 2. **Every button says what it will do before it is pressed.** The
///    consequence ships with the option (§12's `summary`) and is drawn under
///    the label. "Always allow" on its own writes a standing permission on his
///    Mac without naming it, which is the most dangerous control this app
///    could draw.
/// 3. **It is a value, not an observer.** `DeckStore.composerDraft` is
///    `@Published`, so one character typed into the conversation is one publish
///    on the store; an `@ObservedObject` anywhere in the always-open inspector
///    re-runs its body on every one of them. `InspectorIsQuietTests` counts
///    that — 21 body evaluations across 20 keystrokes for the observing shape
///    against 1 for this one — and sweeps this file for the mistake.
struct AttentionTrayView: View, Equatable {
    let items: [AttentionItem]
    /// Excluded from `==` on purpose: a closure is a new value on every draw,
    /// so comparing it would make every comparison false and the skip useless.
    let answer: (AttentionItem, AttentionAction) -> Void
    /// Takes him to the desk named, by wire name. Excluded from `==` for the
    /// same reason `answer` is.
    let openDesk: (String) -> Void
    /// "Sign in on this Mac", per card id. Drawn, so it is in `==`.
    var signIns: [String: MacSignInPhase] = [:]
    /// Whether this deck connection can take a sign-in made here at all.
    var canSignInOnMac = false
    /// The browser he uses, e.g. "Chrome". Non-nil enables the primary "Use my
    /// Mac's login" action. Drawn, so it is in `==`.
    var macBrowserLabel: String? = nil
    /// Start, share or cancel a sign-in on this Mac. Out of `==`, like `answer`.
    var signIn: (AttentionItem, MacSignInVerb) -> Void = { _, _ in }

    static func == (a: AttentionTrayView, b: AttentionTrayView) -> Bool {
        a.items == b.items && a.signIns == b.signIns && a.canSignInOnMac == b.canSignInOnMac
            && a.macBrowserLabel == b.macBrowserLabel
    }

    var body: some View {
        if !items.isEmpty {
            VStack(alignment: .leading, spacing: 12) {
                header
                ForEach(items) { item in
                    card(item)
                }
            }
            .padding(14)
            // A warm card on the dark pane, not a flat wash: the fill is what
            // makes it the first thing the eye lands on, the hairline is what
            // stops it bleeding into the screen picture underneath.
            .background(
                LinearGradient(
                    colors: [Color.orange.opacity(0.20), Color.orange.opacity(0.10)],
                    startPoint: .top, endPoint: .bottom),
                in: RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous))
            .overlay(
                RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous)
                    .strokeBorder(Color.orange.opacity(0.45), lineWidth: 1))
            .padding(.horizontal, 12)
            .padding(.top, 12)
            // One announced region, read before the desk's own settings — the
            // thing that is blocking him is the thing he should hear first.
            .accessibilityElement(children: .contain)
            .accessibilityLabel(spokenHeader)
        }
    }

    /// Colour is never the signal on its own: the word and the symbol both say
    /// it, and the count says how much of it there is.
    private var header: some View {
        // `.center`, never a text baseline. An SF Symbol and a capsule have no
        // line of text to sit on, so a baseline-aligned row sends SwiftUI to a
        // hidden NSTextField for a fallback and sets its font on every pass —
        // the construct `LayoutSettlesTests` sweeps the whole view layer for.
        HStack(alignment: .center, spacing: 6) {
            Image(systemName: "hand.raised.fill")
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text("Needs your attention")
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.orange)
                .accessibilityAddTraits(.isHeader)
            Spacer(minLength: 0)
            if items.count > 1 {
                Text("\(items.count)")
                    .font(.caption.weight(.semibold))
                    .padding(.horizontal, 7)
                    .padding(.vertical, 2)
                    .background(.quaternary, in: Capsule())
                    .accessibilityHidden(true)
            }
        }
    }

    private var spokenHeader: String {
        items.count == 1
            ? "Needs your attention: one thing is waiting on you"
            : "Needs your attention: \(items.count) things are waiting on you"
    }

    /// One waiting thing: which desk, what it needs, where, what has already
    /// happened, and the deck's own options with their sentences.
    private func card(_ item: AttentionItem) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            // The desk comes first. He is reading another conversation, so
            // "which of my agents is this" is the question before "what".
            HStack(alignment: .center, spacing: 6) {
                Text(item.deskLine)
                    .font(.subheadline.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                // Said in a word, because the two are different work for him: a
                // permission he can grant from this chair, a handover he has to
                // get up and do. Never colour alone.
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

            // **The load-bearing line of a handoff.** What has already happened
            // is what decides whether he opens a laptop now or after dinner —
            // a payment halfway through is not a login screen sitting idle.
            if !item.situation.isEmpty {
                FlatLabel(item.situation, systemImage: "clock.badge.exclamationmark")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Only ever drawn for an ask nothing could attribute, and then it
            // is the only handle he has on it. Silence here would be the app
            // hiding the fact that it does not know whose question this is.
            if !item.evidence.isEmpty {
                FlatLabel(item.evidence, systemImage: "questionmark.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            actions(item)

            if canSignInOnMac, item.signInOnMacURL != nil {
                macSignIn(item)
            }

            // **Where he goes to do it.** Only on a step he has to do himself,
            // and only when there is a desk to open — a button that led
            // nowhere would cost him the one thing this strip is saving.
            if let label = item.openDeskLabel, let name = item.deskName {
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

    /// **His passkey is on this Mac, not on the desk's computer.** So the
    /// sign-in happens here, in a throwaway Chrome window, and is handed to
    /// every desk. Two taps: open it; after signing in, share it.
    @ViewBuilder
    private func macSignIn(_ item: AttentionItem) -> some View {
        let host = item.signInOnMacURL?.host ?? "the site"
        VStack(alignment: .leading, spacing: 6) {
            switch signIns[item.askID] {
            case nil:
                // Primary: the login he already has. He hit "not logged in" with
                // the fresh window, so this leads.
                if let browser = macBrowserLabel {
                    Button {
                        signIn(item, .useLogin)
                    } label: {
                        FlatLabel("Use my \(browser) login", systemImage: "person.crop.circle.badge.checkmark")
                    }
                    .buttonStyle(.borderedProminent)
                    .accessibilityHint("Takes your existing \(host) login from \(browser) on this Mac and gives it to every desk.")
                    Text("Uses the login you already have in \(browser). Only \(host)'s cookies leave this Mac.")
                        .font(.caption2).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                // Fallback: sign in fresh with a passkey.
                Button {
                    signIn(item, .start)
                } label: {
                    FlatLabel(macBrowserLabel == nil ? "Sign in on this Mac (passkey)"
                              : "Sign in fresh with passkey",
                              systemImage: "person.badge.key")
                }
                .accessibilityHint("Opens \(host) in a separate Chrome window on this Mac. Sign in with Touch ID or your iPhone there; then every desk gets the sign-in.")
            case .importing?:
                FlatLabel("Taking your login and giving it to every desk...",
                          systemImage: "arrow.triangle.2.circlepath")
                    .font(.caption)
            case .offerPasskeyInstead(let why)?:
                FlatLabel(why, systemImage: "info.circle")
                    .font(.caption).fixedSize(horizontal: false, vertical: true)
                Button {
                    signIn(item, .start)
                } label: {
                    FlatLabel("Sign in fresh with passkey", systemImage: "person.badge.key")
                }
                .buttonStyle(.borderedProminent)
            case .waitingForYou?:
                Text("Sign in to \(host) in the Chrome window that just opened (Touch ID or your iPhone), then press Share: every desk gets it.")
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                HStack(spacing: 8) {
                    Button("Cancel") { signIn(item, .cancel) }
                    Button("Share") { signIn(item, .share) }
                        .buttonStyle(.borderedProminent)
                }
            case .sharing?:
                FlatLabel("Giving the sign-in to every desk...", systemImage: "arrow.triangle.2.circlepath")
                    .font(.caption)
            case .shared(let desks)?:
                FlatLabel(desks == 1 ? "Signed in on 1 desk." : "Signed in on \(desks) desks.",
                          systemImage: "checkmark.circle")
                    .font(.caption)
            case .failed(let why)?:
                FlatLabel(why, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .fixedSize(horizontal: false, vertical: true)
                Button("Try again") { signIn(item, .start) }
                    .font(.caption)
            }
        }
    }

    /// **Two answers are a row; three are a column.** A handoff has exactly two
    /// ("skip" and "I'm done"), and side by side they read as one decision. A
    /// permission has three, each with a sentence that needs the width.
    ///
    /// In a row the quiet answer is first and the ordinary one last, so the
    /// thumb lands on the one he almost always wants. The deck's sentence for
    /// each is still drawn before any tap - under the row, not inside a button
    /// too narrow to hold it.
    @ViewBuilder
    private func actions(_ item: AttentionItem) -> some View {
        if item.actions.count == 2 {
            let ordered = Self.ordered(item.actions)
            VStack(alignment: .leading, spacing: 8) {
                HStack(spacing: 8) {
                    ForEach(ordered) { action in
                        button(item, action, showsSummary: false)
                    }
                }
                ForEach(ordered) { action in
                    (Text(action.label).fontWeight(.semibold)
                     + Text("  \u{2014} " + Self.consequence(of: action)))
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHidden(true)
                }
            }
        } else {
            // The deck's own order: the safe answer first, the standing rules
            // after it.
            ForEach(item.actions) { action in
                button(item, action, showsSummary: true)
            }
        }
    }

    /// The deck writes each summary as "<label> - <what happens>". Under a row
    /// of buttons that already say the label, only the second half is news.
    static func consequence(of action: AttentionAction) -> String {
        for dash in [" \u{2014} ", " - ", " \u{2013} "] {
            let prefix = action.label + dash
            if action.summary.hasPrefix(prefix) {
                return String(action.summary.dropFirst(prefix.count))
            }
        }
        return action.summary
    }

    /// The ordinary answer - carry on, or allow this once - goes last.
    static func ordered(_ actions: [AttentionAction]) -> [AttentionAction] {
        actions.enumerated().sorted { a, b in
            let (ka, kb) = (isOrdinary(a.element) ? 1 : 0, isOrdinary(b.element) ? 1 : 0)
            return ka == kb ? a.offset < b.offset : ka < kb
        }.map(\.element)
    }

    private static func isOrdinary(_ action: AttentionAction) -> Bool {
        switch action.verb {
        case .permission(.once), .handoff(.done): return true
        default: return false
        }
    }

    private func button(_ item: AttentionItem, _ action: AttentionAction,
                        showsSummary: Bool) -> some View {
        Button {
            answer(item, action)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(action.label)
                    .font(.subheadline.weight(.medium))
                    .lineLimit(showsSummary ? 2 : 1)
                    .minimumScaleFactor(0.85)
                    .fixedSize(horizontal: false, vertical: showsSummary)
                // The deck's own sentence for what this reply does, drawn
                // before the tap and never truncated to one line - being able
                // to read it is the entire point of drawing it.
                if showsSummary {
                    Text(Self.consequence(of: action))
                        .font(.system(.caption2, design: .monospaced))
                        .opacity(0.7)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
            }
            .frame(maxWidth: .infinity, alignment: showsSummary ? .leading : .center)
            .multilineTextAlignment(showsSummary ? .leading : .center)
            .contentShape(Rectangle())
        }
        .buttonStyle(AttentionButtonStyle(kind: Self.isOrdinary(action) ? .primary : .quiet))
        // The deck refuses this one here - credentials, payments, anything
        // irreversible. Drawn disabled *with its reason*, never greyed out in
        // silence, which reads as a bug rather than as a deliberate rule.
        .disabled(!action.isAvailable)
        .help(action.summary)
        // Spoken as one control. The label alone would hide the standing rule
        // from the people who most need it read out, and with two desks
        // waiting it would not even say which one it answers.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(item.spokenAction(action))
        .accessibilityAddTraits(.isButton)
    }
}

/// The card's two looks. The ordinary answer is a light pill with dark text -
/// the one bright thing on an orange card - and the quiet one is a dim pill, so
/// skipping is never the easy tap. Colour is never the signal: both carry their
/// words.
private struct AttentionButtonStyle: ButtonStyle {
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

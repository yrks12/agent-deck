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
    /// Whether this deck can open a desk's screen. Drawn, so it is in `==`.
    var canTakeOver = false
    /// Opens the named desk's screen. Out of `==`, like `answer`.
    var takeOver: (String) -> Void = { _ in }

    static func == (a: AttentionTrayView, b: AttentionTrayView) -> Bool {
        a.items == b.items && a.signIns == b.signIns && a.canSignInOnMac == b.canSignInOnMac
            && a.macBrowserLabel == b.macBrowserLabel && a.canTakeOver == b.canTakeOver
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

    /// One waiting thing, drawn by the card the desk's own thread draws too
    /// (`SignInCardView`), so the two places cannot offer different buttons.
    private func card(_ item: AttentionItem) -> some View {
        SignInCardView(
            item: item,
            signIn: signIns[item.askID],
            canSignInOnMac: canSignInOnMac,
            macBrowserLabel: macBrowserLabel,
            canTakeOver: canTakeOver,
            answer: answer,
            signInVerb: signIn,
            openDesk: openDesk,
            takeOver: takeOver)
    }

    /// The deck writes each summary as "<label> - <what happens>". Under a
    /// button that already says the label, only the second half is news.
    static func consequence(of label: String, _ summary: String) -> String {
        for dash in [" \u{2014} ", " - ", " \u{2013} "] {
            let prefix = label + dash
            if summary.hasPrefix(prefix) {
                return String(summary.dropFirst(prefix.count))
            }
        }
        return summary
    }

    static func consequence(of action: AttentionAction) -> String {
        consequence(of: action.label, action.summary)
    }
}

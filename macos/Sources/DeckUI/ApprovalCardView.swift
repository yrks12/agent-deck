import SwiftUI
import DeckKit

/// The approval card, inline in the conversation.
///
/// Every button states the rule it would write, on its own row, before it is
/// pressed. That is not decoration: "Always allow" creates a standing
/// permission on this Mac, and a control that grants one without naming it is
/// the most dangerous thing this app could ship.
struct ApprovalCardView: View {
    let card: ApprovalCard
    /// Nil while an answer is in flight, so nothing can be double-granted.
    let answer: (ApprovalOption) -> Void
    /// §23 "Always, up to a limit…": `nil` back when the deck made it.
    var stand: ((StandingFromCard) async -> String?)? = nil
    @State private var askingLimit = false
    @State private var showDetails = false
    @State private var isAnswering = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            // `.top`, never `.firstTextBaseline`. An SF Symbol has no line of
            // text to sit on, so a baseline-aligned stack sends SwiftUI to a
            // hidden NSTextField for a fallback and sets its font on every
            // pass — dirtying the window's constraints and scheduling the next
            // one. `.top` also keeps the icon beside the FIRST line of a title
            // that wraps, which is what the baseline was for.
            HStack(alignment: .top, spacing: 8) {
                Image(systemName: symbol)
                    .foregroundStyle(tone)
                    .accessibilityHidden(true)
                    .padding(.top, 2)
                Text(card.title)
                    .font(.headline)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 8)
                pill
            }

            Text(card.runsOn)
                .font(.subheadline)
                .foregroundStyle(.secondary)

            // **What the tap actually did**, in the deck's own terms: the rule
            // that was written, and whether the desk was told to carry on
            // (§12's `resumed`). Without this the card simply disappeared, and
            // a standing permission on his Mac left nothing on screen at all.
            if let outcome = card.outcome {
                FlatLabel(outcome, systemImage: "checkmark.seal")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            // Bounded like a message bubble, and for the same reason: this
            // card is drawn in the same LazyVStack as the transcript, so an
            // unbounded reason on one re-measures everything around it. §12
            // composes a short sentence today; a deck that starts sending the
            // agent's own reasoning would otherwise put a wall of text back in
            // the transcript by a route nothing was watching.
            if !card.why.isEmpty {
                LongTextBody(text: card.why) { shown, _ in
                    Text(shown)
                        .font(.callout)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            if !card.details.isEmpty {
                DisclosureGroup(isExpanded: $showDetails) {
                    Text(card.details)
                        .font(.system(.caption, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.top, 4)
                } label: {
                    Text(card.disclosureTitle).font(.callout)
                }
                .accessibilityHint("Shows the exact command this agent wants to run")
            }

            // A settled question keeps its record and loses its buttons. One
            // that still offered to grant would be an invitation to grant the
            // same standing permission twice.
            if card.isAnswerable {
                Divider()

                VStack(spacing: 6) {
                    ForEach(card.options) { option in
                        optionButton(option)
                        // Beside "Always": the same grant, with a daily limit.
                        if option.reply == .always { standingButton }
                    }
                    if !card.options.contains(where: { $0.reply == .always }) { standingButton }
                }
            }
        }
        .padding(14)
        .background(Color(nsColor: .controlBackgroundColor),
                    in: RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous)
                .strokeBorder(tone.opacity(0.55), lineWidth: 1)
        )
        .frame(maxWidth: 480)
    }

    /// The pill. **Never colour alone** — the word is always there and the
    /// symbol beside the title carries the same meaning again, so nothing here
    /// depends on telling orange from green.
    ///
    /// A still shape, like everything else that rests on this pane: the strip
    /// below it is a word and a glyph for the same reason, and a card-heavy
    /// transcript is exactly where an animated badge would put the whole
    /// `LazyVStack` back into a re-measure loop.
    private var pill: some View {
        Text(card.statusPill)
            .font(.caption2.weight(.semibold))
            .foregroundStyle(.primary)
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(tone.opacity(0.16), in: Capsule())
            .fixedSize()
            .accessibilityLabel("Status: \(card.statusPill)")
    }

    /// One symbol per state, so the card is legible at a glance and without
    /// colour. `DeskStatus` makes the same promise for the strip.
    private var symbol: String {
        switch card.status {
        case .waitingOnYou: return "hand.raised.fill"
        case .allowedOnce: return "checkmark.circle"
        case .alwaysAllowed: return "checkmark.seal.fill"
        case .denied: return "nosign"
        case .settledElsewhere: return "arrow.uturn.left.circle"
        }
    }

    private var tone: Color {
        switch card.status {
        case .waitingOnYou: return .orange
        case .allowedOnce, .alwaysAllowed: return .green
        case .denied: return .secondary
        case .settledElsewhere: return .secondary
        }
    }

    private func optionButton(_ option: ApprovalOption) -> some View {
        Button {
            isAnswering = true
            answer(option)
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                Text(option.label)
                    .font(.body.weight(.medium))
                // The deck's own sentence for what this reply writes, before
                // the tap. Never truncated to one line: the whole point is
                // that it can be read.
                Text(option.ruleText)
                    .font(.system(.caption, design: .monospaced))
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .multilineTextAlignment(.leading)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .foregroundStyle(tint(option))
        .background(option.reply == .once ? DeckPalette.field : DeckPalette.card,
                    in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
            .strokeBorder(DeckPalette.cardStroke, lineWidth: 1))
        .opacity(option.isAvailable ? 1 : 0.5)
        // An option the deck will not offer here is drawn disabled *with its
        // reason underneath* — never greyed out silently, which would read as
        // a bug rather than as a deliberate "this one always asks".
        .disabled(isAnswering || !option.isAvailable)
        // Spoken as one control: the label alone would hide the standing rule
        // from anyone using VoiceOver, which is exactly the group that most
        // needs it read out.
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(option.label). \(spokenRule(option))")
        .accessibilityAddTraits(.isButton)
    }

    @ViewBuilder private var standingButton: some View {
        if card.offersStanding, let stand, let option = card.standingOption {
            Button { askingLimit = true } label: {
                VStack(alignment: .leading, spacing: 2) {
                    Text(StandingPresentation.cardOption).font(.body.weight(.medium))
                    Text(option.summary)
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .multilineTextAlignment(.leading)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .foregroundStyle(.orange)
            .background(DeckPalette.card, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(DeckPalette.cardStroke, lineWidth: 1))
            .disabled(isAnswering)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(StandingPresentation.cardOption) \(option.summary)")
            .accessibilityAddTraits(.isButton)
            .sheet(isPresented: $askingLimit) {
                StandingLimitSheet(option: option, submit: { body in
                    isAnswering = true
                    let problem = await stand(body)
                    if problem != nil { isAnswering = false }
                    return problem
                }, onDone: { askingLimit = false })
            }
        }
    }

    private func tint(_ option: ApprovalOption) -> Color {
        switch option.reply {
        case .once: return .primary
        case .always: return .orange
        case .never: return .secondary
        }
    }

    /// Backticks are silence in VoiceOver, so the deck's sentence is read with
    /// the scope said out loud in front of it.
    private func spokenRule(_ option: ApprovalOption) -> String {
        let sentence = option.summary.replacingOccurrences(of: "`", with: "")
        guard option.isAvailable else { return "Not available. \(sentence)" }
        let scope = option.isStanding ? "From now on" : "This time only"
        return "\(scope): \(sentence)"
    }
}

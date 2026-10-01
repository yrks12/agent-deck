import SwiftUI
import DeckKit

/// **A question the desk asked as buttons (K4).**
///
/// The reference's widget: the prompt, a line of help, two to four options, and — when
/// the desk allows it — his own words instead. Answered, it says what he chose
/// and offers nothing more to press; skipped (he typed instead), it says so and
/// keeps the buttons, because a skipped card can still be answered (§6.5).
///
/// On the desk's side of the pane, like the desk's bubble, because the desk is
/// the one asking. Fixed-width buttons in a wrapping column rather than a row:
/// four 40-character labels do not fit across a 380pt column.
struct DecisionCardView: View {
    let decision: Decision
    let answer: (String) -> Void

    @State private var custom = ""

    var body: some View {
        HStack(spacing: 0) {
            VStack(alignment: .leading, spacing: 10) {
                Text(decision.prompt)
                    .font(.body.weight(.semibold))
                    .fixedSize(horizontal: false, vertical: true)
                if let help = decision.help, !help.isEmpty {
                    Text(help)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
                if let status = decision.statusLine {
                    statusRow(status)
                }
                if decision.offersChoices {
                    choices
                }
            }
            .padding(14)
            .background(ThreadColors.agentBubble,
                        in: RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous)
                .strokeBorder(.quaternary))
            .frame(maxWidth: 440, alignment: .leading)
            Spacer(minLength: BubbleMetrics.oppositeGutter)
        }
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("decision-card")
        .accessibilityLabel("Decision: \(decision.prompt)")
    }

    private var choices: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(decision.options.enumerated()), id: \.offset) { _, option in
                Button { answer(option.value) } label: {
                    Text(option.label)
                        .font(.callout.weight(option.style == .primary ? .semibold : .regular))
                        .lineLimit(2)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(.horizontal, 12)
                        .padding(.vertical, 7)
                        .foregroundStyle(foreground(option.style))
                        .background(background(option.style),
                                    in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(option.value)
                .accessibilityIdentifier("decision-option")
                .accessibilityLabel(option.label)
                .accessibilityHint("Answers with: \(option.value)")
            }
            if decision.allowCustom {
                HStack(spacing: 6) {
                    TextField("Or say something else…", text: $custom)
                        .textFieldStyle(.plain)
                        .padding(.horizontal, 10)
                        .padding(.vertical, 6)
                        .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 9))
                        .onSubmit(sendCustom)
                    if !custom.trimmingCharacters(in: .whitespaces).isEmpty {
                        Button(action: sendCustom) {
                            Image(systemName: "arrow.up.circle.fill").font(.title3)
                        }
                        .buttonStyle(.plain)
                        .accessibilityLabel("Send your own answer")
                    }
                }
            }
        }
    }

    /// What happened to the card: a tick for his answer, a quieter arrow for
    /// one he moved past.
    private func statusRow(_ status: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: decision.state == .answered ? "checkmark.circle.fill" : "arrow.uturn.right")
                .foregroundStyle(decision.state == .answered ? Color.green : Color.secondary)
            Text(status)
                .font(.callout)
                .foregroundStyle(decision.state == .answered ? Color.primary : Color.secondary)
                .fixedSize(horizontal: false, vertical: true)
        }
        .accessibilityElement(children: .combine)
    }

    private func sendCustom() {
        let text = custom.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        answer(text)
        custom = ""
    }

    private func foreground(_ style: DecisionOption.Style) -> Color {
        switch style {
        case .primary: return Color(nsColor: .windowBackgroundColor)
        case .default: return .primary
        case .danger: return .red
        }
    }

    private func background(_ style: DecisionOption.Style) -> Color {
        switch style {
        case .primary: return .primary
        case .default, .danger: return ThreadColors.ownerBubble
        }
    }
}

import SwiftUI
import AppKit
import DeckKit

/// **`pr15` and `:8083` are things he pastes, not prose.**
///
/// `ref-03-links-code-rollups.jpg` draws every inline code span as a pink
/// monospaced chip inside the bubble: `/healthz`, `studio-preview-pr15`,
/// `188f1458`. Ours drew them in the bubble's own body colour, so an id he was
/// meant to act on was indistinguishable from the sentence around it — in a
/// wall of Hebrew and English, which is how his desks write.
///
/// `MarkdownBody` already marks those runs and memoises the parse. This only
/// decides what they look like, which is a `DeckUI` question: `DeckKit` imports
/// no UI framework, by rule.
enum MarkdownStyle {

    /// Apple's pink taken toward black in the light appearance, exactly as
    /// `AttentionPalette` does with orange and green, and resolved by AppKit at
    /// draw time — so a theme switch and increased-contrast both follow the OS
    /// rather than a snapshot taken at launch. Plain `systemPink` measures
    /// under 4.5:1 on a light bubble; `BubbleTypographyTests` pins both.
    static let codeTint = Color(nsColor: NSColor(name: nil) { appearance in
        guard appearance.bestMatch(from: [.aqua, .darkAqua]) != .darkAqua else {
            return .systemPink
        }
        return NSColor.systemPink.blended(withFraction: 0.42, of: .black) ?? .systemPink
    })

    /// The chip, over whatever `MarkdownBody` decided the runs were.
    ///
    /// Returns the argument untouched when there is no code in it — which is
    /// most messages — so a plain reply pays for one `contains` and no copy.
    static func styled(_ text: AttributedString) -> AttributedString {
        guard text.runs.contains(where: isCode) else { return text }
        var out = text
        for run in text.runs where isCode(run) {
            out[run.range].foregroundColor = codeTint
            out[run.range].backgroundColor = Color.primary.opacity(0.08)
        }
        return out
    }

    private static func isCode(_ run: AttributedString.Runs.Run) -> Bool {
        run.inlinePresentationIntent?.contains(.code) == true
    }
}

/// **What a desk actually wrote, drawn the way it meant it.**
///
/// `MarkdownBody` has already decided what the blocks are; this only draws
/// them. The split matters: deciding is pure, testable and memoised in DeckKit,
/// and nothing here re-parses anything on a body evaluation — that is the
/// mistake that cost the owner a day on this exact pane.
///
/// A fenced block is drawn as a slab with its own background and a monospaced
/// font, because a diff or a shell line that reflows like prose is one he
/// cannot read and cannot copy.
struct MarkdownText: View {
    let text: String
    /// Collapsed, the whole preview is drawn as **one** `Text`. A line limit
    /// applies per `Text`, so a preview that came back as a list and a fence
    /// would be eight lines each and the collapsed row's height would depend on
    /// the message again — which is exactly what the collapse exists to stop.
    var asSinglePassage = false
    /// **Off in his own bubble.** That one is accent blue with white text, and
    /// a pink chip on it fails contrast in both appearances. The monospaced
    /// run is still there — it is the tint that is the risk, and he is not the
    /// one writing container ids into this app anyway.
    var tintsCode = true

    var body: some View {
        if asSinglePassage {
            Text(styled(MarkdownBody.singlePassage(of: text)))
                .multilineTextAlignment(.leading)
                .fixedSize(horizontal: false, vertical: true)
                .font(.body)
        } else {
            blocksView
        }
    }

    private func styled(_ rendered: AttributedString) -> AttributedString {
        tintsCode ? MarkdownStyle.styled(rendered) : rendered
    }

    private var blocksView: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(Array(MarkdownBody.blocks(of: text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .prose(let rendered):
                    Text(styled(rendered))
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)

                case .listItem(let marker, let rendered):
                    HStack(alignment: .top, spacing: 6) {
                        // `.top`, never a text baseline — the rule this app
                        // already paid for twice.
                        Text(marker)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                        Text(styled(rendered))
                            .multilineTextAlignment(.leading)
                            .fixedSize(horizontal: false, vertical: true)
                    }

                case .code(let body, _):
                    Text(body)
                        .font(.system(.callout, design: .monospaced))
                        .multilineTextAlignment(.leading)
                        .fixedSize(horizontal: false, vertical: true)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                        .background(.quaternary.opacity(0.5),
                                    in: RoundedRectangle(cornerRadius: 6))
                }
            }
        }
        .font(.body)
    }
}

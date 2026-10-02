import Foundation

/// **A wall of pasted text is shown as a preview until he asks for it.**
///
/// His words, watching his own app peg a core: *"app stuck again"*, *"when i
/// scroll to his answer"*, *"we dont klnow how to handle long text"*. The
/// sample taken at that moment has `TextField|NSTextView|TextEditor` at **83**
/// frames — hotter than the alignment chain that three previous fixes chased —
/// and the app was talking to the deck **zero** times a minute because it had
/// no cycles left to.
///
/// Three suspects were cut before this one — a spinner, `Label`, the window
/// chrome — and each was measurably real and none was sufficient. Making the
/// long row *cheaper* was tried too: a definite width took one size query from
/// 38.77ms to 0.70ms and the app still pegged. So this stops trying to make an
/// unbounded layout cheap and removes the unbounded layout: above the
/// threshold the transcript lays out a few hundred characters, not tens of
/// thousands, and the rest is one button away.
///
/// **Nothing is lost, and that is the constraint this policy exists under.**
/// He pasted that mandate deliberately. Expanded shows every character;
/// `MessageClipboard` copies every character whether it is expanded or not;
/// and the spoken label a screen reader hears is the whole message, unchanged.
public enum LongMessage {

    /// Above this many characters a message is drawn collapsed.
    ///
    /// Chosen so an ordinary reply — a paragraph or three, which is most of
    /// what a desk says — is never touched, and the pasted mandates that wedge
    /// the pane always are. A message at this length lays out in about a
    /// millisecond; at 20,000 it was tens.
    public static let collapseAboveCharacters = 1_200

    /// How much text the collapsed bubble is given to draw.
    ///
    /// Deliberately larger than `collapsedLines` can show: the line limit is
    /// what fixes the height, and a preview that always overflows it is what
    /// makes that height the *same* height for a 2,000-character message and a
    /// 200,000-character one. If this were tuned down to "just enough", the
    /// row would start varying with the text again.
    public static let previewCharacters = 700

    /// The collapsed bubble is exactly this many lines tall, whatever is in it.
    /// That is what stops the transcript's total height depending on the length
    /// of any one message — which is what made scrolling to his answer wedge
    /// the pane.
    public static let collapsedLines = 8

    /// **What he reads, and nothing else.**
    ///
    /// This said `Show full message — 1,916 more characters`. The reference's frames end
    /// the preview on a plain `…` and put a quiet **Show more** underneath, and
    /// that is the difference between a conversation and a log viewer: he is
    /// deciding whether to read the rest, not auditing a byte count.
    ///
    /// The count is not deleted — see `spokenExpandLabel`. It moves off the
    /// screen and into the label a screen reader hears, because somebody using
    /// VoiceOver cannot see that the bubble is short and "Show more" alone does
    /// not distinguish a sentence from a dissertation.
    public static let expandTitle = "Show more"
    public static let collapseTitle = "Show less"
    public static let copyTitle = "Copy full message"

    /// What the control announces: what it does first, then how much is behind
    /// it. Leading with the action is the rule for every control in this app —
    /// a label that opens with a number is one nobody can skim by.
    public static func spokenExpandLabel(of text: String) -> String {
        "\(expandTitle), \(hiddenSummary(of: text))"
    }

    /// Whether this text is drawn collapsed at all.
    public static func isLong(_ text: String) -> Bool {
        text.count > collapseAboveCharacters
    }

    /// What the collapsed bubble draws: the opening of the message, cut at a
    /// word boundary so it does not end mid-word, with an ellipsis.
    ///
    /// Returns the text unchanged when it is not long — a caller must be able
    /// to ask for the preview of anything without having to check first.
    public static func preview(of text: String) -> String {
        guard isLong(text) else { return text }
        let head = text.prefix(previewCharacters)
        // Back up to the last space so the preview ends on a word. If there is
        // no space at all — a single enormous token, a base64 blob — the hard
        // cut is the right answer and the ellipsis says so.
        guard let lastSpace = head.lastIndex(where: { $0 == " " || $0 == "\n" }) else {
            return MarkdownBody.balanced(String(head)) + "…"
        }
        let cut = head[head.startIndex..<lastSpace]
            .trimmingCharacters(in: .whitespacesAndNewlines)
        // **Balanced before it is shown.** Agents write Markdown, and a cut
        // lands wherever it lands: inside `**` the rest of the preview is bold,
        // inside a fence the rest of the conversation is a code block, inside a
        // link the raw syntax is drawn. See `MarkdownBody.balanced`.
        return MarkdownBody.balanced(cut) + "…"
    }

    /// What is being hidden, said in the terms he would use to decide whether
    /// to open it. Never "…" alone: a control with no size on it is a control
    /// nobody presses.
    public static func hiddenSummary(of text: String) -> String {
        let remaining = max(text.count - preview(of: text).count, 0)
        let formatter = NumberFormatter()
        formatter.numberStyle = .decimal
        let count = formatter.string(from: NSNumber(value: remaining)) ?? "\(remaining)"
        return "\(count) more characters"
    }
}

import SwiftUI
import AppKit
import DeckKit

/// **Puts a wall of pasted text on screen without laying all of it out.**
///
/// The transcript pegged a core whenever a very long message was on it — his
/// words, *"when i scroll to his answer"*, and a sample with the text engine as
/// the hottest chain in the process. Three attempts to make that row *cheaper*
/// each removed something real and none of them was enough, because the cost is
/// not a size query: it is laying out line fragments for tens of thousands of
/// characters, and nothing makes that free.
///
/// So this does not draw them. Collapsed, the body is handed
/// `LongMessage.preview` — a few hundred characters — and clamped to
/// `LongMessage.collapsedLines`, so the row is the same height whatever the
/// message weighs and the transcript's total height stops depending on how much
/// he pasted. Expanded, it is handed the whole string, and that may be slow;
/// he asked for it.
///
/// **Nothing is lost.** Expanding shows every character, the context menu
/// copies every character whether it is expanded or not — selection can only
/// reach what is drawn, which would otherwise make a collapsed bubble silently
/// copy the preview — and the spoken label a screen reader hears is set by the
/// caller from the full text and is unaffected by any of this.
struct LongTextBody<Content: View>: View {
    let text: String
    /// Renders whichever slice of the text is being shown. The caller decides
    /// what that looks like — the transcript draws links and file paths
    /// differently from prose, a tool-call card does not.
    /// The slice to draw, and whether it is the collapsed one — a collapsed
    /// body has to be a single passage for the line clamp to fix its height.
    @ViewBuilder let content: (String, Bool) -> Content

    @State private var isExpanded: Bool

    init(text: String, startExpanded: Bool = false,
         @ViewBuilder content: @escaping (String, Bool) -> Content) {
        self.text = text
        self.content = content
        _isExpanded = State(initialValue: startExpanded)
    }

    private var isLong: Bool { LongMessage.isLong(text) }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            content(isLong && !isExpanded ? LongMessage.preview(of: text) : text,
                    isLong && !isExpanded)
                // The clamp is what fixes the height. Without it the preview's
                // own length would still decide how tall the row is, and a
                // transcript whose height depends on its content is the thing
                // that made scrolling to his answer an event.
                .lineLimit(isLong && !isExpanded ? LongMessage.collapsedLines : nil)

            if isLong {
                Button {
                    isExpanded.toggle()
                } label: {
                    // **Quiet, and carrying no number.** The preview above it
                    // already ends on `…`, which is the truncation mark; saying
                    // "1,916 more characters" underneath was the same fact told
                    // twice, the second time in the voice of a log viewer.
                    Text(isExpanded ? LongMessage.collapseTitle : LongMessage.expandTitle)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                // Said out loud as its own control, and **this is where the
                // count went**. The bubble around it speaks the whole message,
                // so this is the only thing here that has to announce an action
                // rather than content — and somebody who cannot see that the
                // bubble is short needs to know whether it hides a sentence or
                // a dissertation.
                .accessibilityLabel(isExpanded
                                    ? LongMessage.collapseTitle
                                    : LongMessage.spokenExpandLabel(of: text))
            }
        }
        // Copy is on the whole body, long or short, so there is one gesture to
        // learn rather than one that appears only on big messages.
        .contextMenu {
            Button(LongMessage.copyTitle) { MessageClipboard.copy(text) }
        }
    }
}

/// **Copy takes every character, whatever is on screen.**
///
/// `.textSelection(.enabled)` can only reach text that was drawn, so once a
/// long message is collapsed a select-all-and-copy would quietly take the
/// preview. That is the worst available failure: he finds out after pasting it
/// somewhere that mattered.
///
/// The pasteboard is a parameter so this is testable without touching the one
/// the owner is actually using.
enum MessageClipboard {
    static func copy(_ text: String, to pasteboard: NSPasteboard = .general) {
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }
}

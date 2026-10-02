import Foundation

/// What Return does. Two answers, and the point is that there is never a third.
public enum ComposerKeyPress: Equatable, Sendable {
    case send
    case newline
}

/// **How big the box is, and what the keys do.**
///
/// Measured live, 2026-09-03: *"long message i cant scroll/edit easyliy"*. He
/// pastes a multi-paragraph mandate — about eight lines — into a composer that
/// stopped at six, so the top of what he had written was not reachable, and
/// nothing on screen said whether Return would send the half-finished thing.
///
/// Both halves are decided here rather than in the view, for the reason the
/// rest of this package splits that way: the caption the user reads and the
/// behaviour he gets have to come from the same value, or they drift apart and
/// the caption becomes a lie about the app.
public struct ComposerBox: Equatable, Sendable {

    /// One line, closed. A chat composer that opens tall reads as a form.
    public static let minimumLines = 1

    /// Where growing stops and scrolling starts.
    ///
    /// Twelve, not six: eight is the length of the mandate he actually pastes,
    /// and a box that exactly fits the longest thing you ever write leaves no
    /// room to *edit* it. Past this the box holds still and the text moves
    /// inside it — a composer that keeps growing pushes the transcript, and
    /// then itself, off the bottom of the window.
    public static let maximumLines = 12

    /// **On screen, next to the box.** Undiscoverable is the same as absent:
    /// he had no way to know which key would send a paste he was still writing,
    /// and there is no unsend.
    public static let sendHint = "Return to send · Shift-Return for a new line"

    /// Lines the box draws, already clamped.
    public var lines: Int
    /// The draft is taller than the box, so it is scrolling rather than growing.
    /// Said out loud because a clipped draft and a short one look identical.
    public var isScrolling: Bool

    public init(lines: Int, isScrolling: Bool) {
        self.lines = lines
        self.isScrolling = isScrolling
    }

    /// Counted on the newlines he typed.
    ///
    /// Wrapped lines are counted **by SwiftUI**, against the real width and the
    /// real font, and clamped to the same `maximumLines` this states — see the
    /// composer in `ThreadView`. So this is the floor: a draft this says is
    /// scrolling is scrolling, and one it says fits may still wrap and scroll
    /// on a narrow window. Nothing here guesses at a glyph width.
    public static func make(for draft: String) -> ComposerBox {
        // `omittingEmptySubsequences: false` so blank lines between paragraphs
        // are counted, and so a trailing newline has already made room for the
        // line he is about to type — otherwise the caret leaves the box the
        // instant he presses Shift-Return.
        let typed = draft.split(separator: "\n", omittingEmptySubsequences: false).count
        let wanted = max(typed, minimumLines)
        return ComposerBox(
            lines: min(wanted, maximumLines),
            isScrolling: wanted > maximumLines
        )
    }

    /// The one place Return is decided.
    ///
    /// **Return sends. Shift-Return and Option-Return make a new line.** Chosen
    /// that way round because this is a chat window and almost every message is
    /// one line, so the common case should cost one key. Option is honoured as
    /// well as Shift because it is the other Mac habit for this, and sending a
    /// half-written mandate on the wrong finger is the same accident twice.
    public static func returnPress(shift: Bool, option: Bool) -> ComposerKeyPress {
        (shift || option) ? .newline : .send
    }
}

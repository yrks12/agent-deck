import SwiftUI
import DeckKit

/// **Who this conversation is with, and whether the stream is up — in the
/// pane, not in the window's chrome.**
///
/// Both of these used to be written into AppKit from `ThreadView.body`: the
/// desk went into `.navigationSubtitle`, which is an `NSTextField` in the
/// titlebar, and the connection dot went into a `ToolbarItem`, which is an
/// `NSControl`. That closes a circuit — `setFont:` ->
/// `_invalidateEffectiveFont` -> `invalidateIntrinsicContentSize` ->
/// `setNeedsUpdateConstraints` -> `NSWindow._postWindowNeedsUpdateConstraints`
/// -> layout -> `body` -> write it again — and it never converges. Measured
/// hands-off against the owner's deck: 100% CPU from 45 seconds after launch,
/// with the app so busy it stopped talking to the deck entirely. The same
/// build with those two writes removed sat at 1.2 / 1.2 / 0.0 / 3.4 / 0.0.
///
/// Drawn here, both are ordinary SwiftUI. A layout pass ends.
///
/// **The height is deliberately independent of everything that changes.** The
/// title is the tallest thing in the row and the title does not move when the
/// stream drops, so a reconnect cannot resize the transcript underneath —
/// `WindowChromeTests` measures that rather than trusting it. This app has
/// already paid for a strip whose size moved: it re-measures every row of the
/// transcript's `LazyVStack` each time it does.
///
/// **Drawn like the phone's thread header** (owner, 2026-09-30): the desk's
/// face, its name and title, and under them one status line — waiting for
/// you, a stream that is down, or what the desk is doing — decided by
/// `ThreadHeaderStatus` in DeckKit, the rule the phone's header uses too. The
/// status line is always one line, so its height still never moves.
struct ThreadHeader: View {
    let title: String
    let subtitle: String?
    /// `nil` on a pane with no stream behind it — the "+" thread exists before
    /// there is a desk to be connected to.
    let connection: ConnectionState?
    /// The desk's character. `nil` draws no face (a pane with no desk yet).
    var look: AvatarLook? = nil
    var attention: RowAttention = .quiet
    /// The desk's own state word ("Idle", "Asleep") for the status line.
    var agentState: String = ""

    private var status: StatusLine {
        ThreadHeaderStatus.line(attention: attention, connection: connection, loaded: true,
                                agentState: agentState)
    }

    var body: some View {
        HStack(spacing: 10) {
            if let look {
                AvatarView(look: look, attention: attention, isDimmed: false, localAvatarURL: nil,
                           size: CGFloat(DeckTokens.headerAvatar))
            }
            // One announcement rather than fragments: a screen reader should
            // hear "Chief, Chief of staff, Working", not stop between them.
            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(title)
                        .font(.headline)
                        .lineLimit(1)
                    if let subtitle, !subtitle.isEmpty {
                        TitleChip(title: subtitle)
                            .fixedSize()
                            .accessibilityHidden(true)
                    }
                }
                // A space when there is nothing to say, so the row keeps its
                // height (`WindowChromeTests`).
                StatusLineText(line: status.text.isEmpty ? StatusLine(" ", tone: .neutral) : status,
                               font: .caption)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel(spoken)

            Spacer(minLength: 8)

            // The dot he asked for, in the pane he is already reading. It says
            // its own state out loud; see `ConnectionIndicator`.
            if let connection {
                ConnectionIndicator(state: connection)
            }
        }
        .padding(.horizontal, 14)
        // A floor, not a fixed frame: at a large accessibility text size the
        // row must be allowed to grow rather than clip the desk's name.
        .frame(minHeight: 44)
    }

    private var spoken: String {
        var parts = [title]
        if let subtitle, !subtitle.isEmpty { parts.append(subtitle) }
        if !status.text.trimmingCharacters(in: .whitespaces).isEmpty { parts.append(status.text) }
        return parts.joined(separator: ". ")
    }
}

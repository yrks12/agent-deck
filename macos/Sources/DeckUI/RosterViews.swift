import SwiftUI
import DeckKit

// **The roster's pieces, drawn by both apps from this one file.** Compiled
// into DeckUI for the Mac and straight into the iPhone target (see
// `ios/project.yml`), so the chief's card, a desk's row, the unread count, the
// attention banner and the pulse line render the same on both — not two
// implementations kept in step by hand.
//
// Mac rules that bind here too: no view aligns on a text baseline (a baseline
// stack beside a Spacer or a capsule spun the Mac at 98% CPU, see
// `LayoutSettlesTests`), and nothing animates on its own.

/// The words under a name, in the one colour they may carry.
struct StatusLineText: View {
    let line: StatusLine
    var font: Font = .footnote.weight(.semibold)

    var body: some View {
        Text(line.text)
            .font(font)
            .foregroundStyle(DeckPalette.tone(line.tone))
            .lineLimit(1)
    }
}

/// **The chief of staff: a big face, the name, what it is doing, and what it
/// last said** — the one desk he talks to, as a card above the roster.
struct ChiefHero: View {
    let row: SidebarRow
    var isSelected = false
    /// The Mac's short window: a smaller face and one line of preview, so the
    /// desks under it stay on screen.
    var compact = false
    private var avatarSize: CGFloat { compact ? 56 : CGFloat(DeckTokens.heroAvatar) }

    var body: some View {
        HStack(alignment: .center, spacing: 16) {
            AvatarView(look: row.look, attention: row.attention, isDimmed: row.agent.state == .offline,
                       localAvatarURL: row.localAvatarURL, size: avatarSize)
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 6) {
                    Text(row.agent.displayName).font(.title2.weight(.bold)).lineLimit(1)
                    if row.isUnread { UnreadBadge(count: row.unreadCount) }
                }
                Text("Chief of staff" + (row.agent.title.isEmpty ? "" : " · \(row.agent.title)"))
                    .font(.footnote.weight(.medium))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if let line = row.attention.statusLine {
                    StatusLineText(line: line)
                }
                Text(row.previewText)
                    .font(.subheadline)
                    .foregroundStyle(.primary.opacity(0.85))
                    .lineLimit(compact ? 1 : 2)
            }
            Spacer(minLength: 0)
        }
        .padding(compact ? 12 : 16)
        .card(radius: CGFloat(DeckTokens.heroRadius))
        .overlay(
            RoundedRectangle(cornerRadius: CGFloat(DeckTokens.heroRadius), style: .continuous)
                .strokeBorder(Color.primary.opacity(isSelected ? 0.35 : 0), lineWidth: 1.5))
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenLabel)
    }

    private var spokenLabel: String {
        var parts = ["\(row.agent.displayName), chief of staff"]
        if let word = row.attention.spokenWord { parts.append(word) }
        if row.isUnread { parts.append("\(row.unreadCount) unread") }
        parts.append(row.previewText)
        return parts.joined(separator: ", ")
    }
}

/// **One desk in the roster**: its face, its name and title, when it last
/// spoke, whether it is waiting for him, what it said, and how much is unread.
struct RosterRowContent: View {
    let row: SidebarRow
    var avatarSize = CGFloat(DeckTokens.rowAvatar)
    /// How many lines of preview; the phone's default is two (one when the
    /// desk is waiting, under the orange line).
    var previewLines = 2
    /// The Claude account this desk runs on, when the deck has two or more.
    var accountBadge: String?

    var body: some View {
        HStack(alignment: .top, spacing: 14) {
            GroupedAvatarView(row: row, size: avatarSize)
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .center, spacing: 6) {
                    Text(row.agent.displayName)
                        .font(.body.weight(row.isUnread ? .bold : .semibold))
                        .lineLimit(1)
                    if !row.agent.title.isEmpty {
                        TitleChip(title: row.agent.title).fixedSize()
                    }
                    if let accountBadge { AccountBadgeChip(text: accountBadge).fixedSize() }
                    Spacer(minLength: 6)
                    Text(row.timestampLabel)
                        .font(.caption)
                        .foregroundStyle(row.isUnread ? .primary : .secondary)
                        .monospacedDigit()
                }
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 2) {
                        if row.attention == .waitingForYou, let line = row.attention.statusLine {
                            StatusLineText(line: line, font: .subheadline.weight(.semibold))
                        }
                        Text(row.previewText.isEmpty ? " " : row.previewText)
                            .font(.subheadline)
                            .foregroundStyle(.secondary)
                            .lineLimit(row.attention == .waitingForYou ? 1 : previewLines)
                    }
                    Spacer(minLength: 6)
                    if row.isUnread { UnreadBadge(count: row.unreadCount) }
                }
            }
        }
        .contentShape(Rectangle())
    }
}

struct UnreadBadge: View {
    let count: Int
    var body: some View {
        Text(count > 99 ? "99+" : "\(count)")
            .font(.caption2.weight(.bold))
            .monospacedDigit()
            .foregroundStyle(DeckPalette.canvas)
            .padding(.horizontal, 7)
            .padding(.vertical, 3)
            .background(DeckPalette.ink, in: Capsule())
            .accessibilityLabel("\(count) unread")
    }
}

/// "4 things need your attention", as a card. `hint` is where they are: the
/// Attention tab on the phone, the side panel on the Mac.
struct AttentionBanner: View {
    let count: Int
    var hint = "Attention tab"
    var body: some View {
        HStack(spacing: 10) {
            Circle().fill(DeckPalette.waiting).frame(width: 9, height: 9)
            Text(count == 1 ? "1 thing needs your attention" : "\(count) things need your attention")
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
            Spacer(minLength: 4)
            Text(hint).font(.caption).foregroundStyle(.secondary).lineLimit(1)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 12)
        .card(radius: CGFloat(DeckTokens.bannerRadius))
        .contentShape(Rectangle())
    }
}

/// **The live roster status line**: a few overlapping faces of the desks
/// that are busy or waiting, then "2 working · 1 waiting on you".
struct RosterPulse: View {
    let summary: RosterPulseSummary

    init(rows: [SidebarRow]) {
        summary = RosterPulseSummary(rows: rows)
    }

    /// The sentence, with only the "waiting on you" part in orange.
    private var text: Text {
        guard summary.workingText != nil || summary.waitingText != nil else { return Text(summary.line) }
        var out = Text(summary.workingText ?? "")
        if let waiting = summary.waitingText {
            if summary.workingText != nil { out = out + Text(" · ") }
            out = out + Text(waiting).foregroundColor(DeckPalette.waiting)
        }
        return out
    }

    var body: some View {
        HStack(spacing: 10) {
            HStack(spacing: -8) {
                ForEach(summary.faces) { row in
                    // Still faces: the words carry who is working, and a
                    // face ticking here re-lays the whole roster column each
                    // time (`TheConversationStaysQuietWhileItsDeskWorksTests`).
                    AvatarView(look: row.look, attention: .quiet,
                               isDimmed: row.agent.state == .offline, localAvatarURL: nil, size: 24)
                        .background(Circle().fill(DeckPalette.canvas).padding(2))
                }
            }
            text
                .font(.footnote.weight(.medium))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .minimumScaleFactor(0.85)
            Spacer(minLength: 0)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(summary.line)
    }
}

/// **What a desk is for** — its `description`, one line under its name, drawn
/// by both apps' conversation headers. Always one line tall: a space when the
/// desk has none, so moving between two desks never re-measures what is under
/// the header (`DeskDescriptionTests`, `WindowChromeTests`).
struct DeskSummaryText: View {
    let text: String?
    var font: Font = .caption

    var body: some View {
        Text(text ?? " ")
            .font(font)
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .truncationMode(.tail)
            .accessibilityHidden(text == nil)
    }
}

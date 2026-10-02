import SwiftUI
import DeckKit

/// **Quote-replies on the Mac**: what a bubble wears so he can answer it, the
/// quote a reply carries, and the strip over the composer. What is quotable,
/// what the strip says and where a tap goes are decided in DeckKit
/// (`ReplyDraft`, `QuoteJump`); these only draw it, in the shared palette.

/// Hover → a Reply button beside the bubble. Right-click → Reply. A click
/// selects it for ⌘R. The button sits in an overlay, so showing it never
/// re-measures the row.
struct ReplyAffordance: ViewModifier {
    let message: Message
    let isSelected: Bool
    let reply: ((Message) -> Void)?
    let select: ((String) -> Void)?

    @State private var hovering = false

    func body(content: Content) -> some View {
        if let reply, ReplyDraft.canQuote(message) {
            content
                .background(
                    RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous)
                        .fill(isSelected ? DeckPalette.field : Color.clear)
                        .padding(-3)
                )
                .overlay(alignment: message.isFromUser ? .leading : .trailing) {
                    Button { reply(message) } label: {
                        Image(systemName: "arrowshape.turn.up.left")
                            .font(.system(size: 11, weight: .semibold))
                            .foregroundStyle(DeckPalette.ink)
                            .frame(width: 26, height: 26)
                            .background(DeckPalette.card, in: Circle())
                            .overlay(Circle().strokeBorder(DeckPalette.cardStroke))
                    }
                    .buttonStyle(.plain)
                    .help("Reply (⌘R)")
                    .accessibilityLabel("Reply to this message")
                    .opacity(hovering ? 1 : 0)
                    .allowsHitTesting(hovering)
                }
                .onHover { inside in if hovering != inside { hovering = inside } }
                .simultaneousGesture(TapGesture().onEnded { select?(message.id) })
                .contextMenu {
                    Button { reply(message) } label: { FlatLabel("Reply", systemImage: "arrowshape.turn.up.left") }
                }
                .accessibilityAction(named: "Reply") { reply(message) }
        } else {
            content
        }
    }
}

extension View {
    func replyable(_ message: Message, isSelected: Bool = false,
                   reply: ((Message) -> Void)?, select: ((String) -> Void)? = nil) -> some View {
        modifier(ReplyAffordance(message: message, isSelected: isSelected, reply: reply, select: select))
    }
}

/// The quote a reply carries, at the top of its bubble. A tap scrolls to the
/// original.
struct QuoteBlock: View {
    let quote: MessageQuote
    var open: ((MessageQuote) -> Void)?

    var body: some View {
        Button { open?(quote) } label: {
            HStack(alignment: .top, spacing: 7) {
                Capsule().fill(DeckPalette.ink.opacity(0.55)).frame(width: 3)
                VStack(alignment: .leading, spacing: 1) {
                    Text(quote.authorLabel(displayName: Agent.displayName(forWireName:)))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(DeckPalette.ink)
                    Text(quote.excerpt)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .truncationMode(.tail)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, 5)
            .padding(.horizontal, 7)
            .background(DeckPalette.canvas.opacity(0.55),
                        in: RoundedRectangle(cornerRadius: 8, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(open == nil)
        .help("Show the original message")
        .accessibilityLabel("Replying to \(quote.authorLabel(displayName: Agent.displayName(forWireName:))): \(quote.excerpt)")
        .accessibilityHint("Scrolls to the original message")
    }
}

/// Above the composer while he is replying: who, the first line, and ×.
struct ReplyStrip: View {
    let target: ReplyTarget
    let cancel: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 8) {
            Capsule().fill(DeckPalette.ink.opacity(0.55)).frame(width: 3, height: 30)
            VStack(alignment: .leading, spacing: 1) {
                Text("Replying to \(target.authorLabel)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(DeckPalette.ink)
                Text(target.firstLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 0)
            Button(action: cancel) {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(DeckPalette.ink)
                    .frame(width: 22, height: 22)
                    .background(.quaternary, in: Circle())
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.cancelAction)
            .help("Cancel the reply (Esc)")
            .accessibilityLabel("Cancel reply")
        }
        .padding(.horizontal, 16)
        .padding(.top, 8)
        .accessibilityElement(children: .contain)
    }
}

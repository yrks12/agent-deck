import SwiftUI
import DeckKit

/// **Quote-replies on the phone**: swipe a bubble right, or long-press it and
/// pick Reply. What is quotable, what the strip says and where a tap on a quote
/// goes are DeckKit's (`ReplyDraft`, `QuoteJump`) — the Mac answers the same.

/// Swipe right past the threshold → reply, with one light tap as it arms.
/// Long-press → a menu with Reply and Copy.
struct SwipeToReply: ViewModifier {
    let message: Message
    let reply: ((Message) -> Void)?

    /// How far the bubble has to travel before letting go replies.
    static let threshold: CGFloat = 56
    static let maxTravel: CGFloat = 76

    @State private var offset: CGFloat = 0
    @State private var armed = false

    func body(content: Content) -> some View {
        if let reply, ReplyDraft.canQuote(message) {
            content
                .offset(x: offset)
                .background(alignment: .leading) {
                    Image(systemName: "arrowshape.turn.up.left.fill")
                        .font(.system(size: 15, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .padding(.leading, 6)
                        .opacity(Double(min(offset / Self.threshold, 1)))
                        .scaleEffect(armed ? 1.15 : 0.85)
                        .accessibilityHidden(true)
                }
                .simultaneousGesture(
                    DragGesture(minimumDistance: 18, coordinateSpace: .local)
                        .onChanged { value in
                            let dx = value.translation.width
                            // Only a mostly-sideways pull to the right: a
                            // scroll must stay a scroll.
                            guard dx > 0, abs(dx) > abs(value.translation.height) * 1.4 else { return }
                            offset = min(dx, Self.maxTravel)
                            let nowArmed = offset >= Self.threshold
                            if nowArmed != armed {
                                armed = nowArmed
                                if nowArmed { Haptics.tap() }
                            }
                        }
                        .onEnded { _ in
                            if armed { reply(message) }
                            armed = false
                            withAnimation(.spring(response: 0.28, dampingFraction: 0.8)) { offset = 0 }
                        }
                )
                .contextMenu {
                    Button { reply(message) } label: {
                        Label("Reply", systemImage: "arrowshape.turn.up.left")
                    }
                    Button { UIPasteboard.general.string = message.text } label: {
                        Label("Copy", systemImage: "doc.on.doc")
                    }
                }
                .accessibilityAction(named: "Reply") { reply(message) }
        } else {
            content
        }
    }
}

extension View {
    func swipeToReply(_ message: Message, reply: ((Message) -> Void)?) -> some View {
        modifier(SwipeToReply(message: message, reply: reply))
    }
}

/// The quote a reply carries, at the top of its bubble. Tap → the original.
struct PhoneQuoteBlock: View {
    let quote: MessageQuote
    var open: ((MessageQuote) -> Void)?

    var body: some View {
        Button { open?(quote) } label: {
            HStack(alignment: .top, spacing: 8) {
                Capsule().fill(PhoneTheme.ink.opacity(0.55)).frame(width: 3)
                VStack(alignment: .leading, spacing: 2) {
                    Text(quote.authorLabel(displayName: Agent.displayName(forWireName:)))
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(PhoneTheme.ink)
                    Text(quote.excerpt)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .multilineTextAlignment(.leading)
                }
                Spacer(minLength: 0)
            }
            .fixedSize(horizontal: false, vertical: true)
            .padding(.vertical, 6)
            .padding(.horizontal, 8)
            .background(PhoneTheme.canvas.opacity(0.55),
                        in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        }
        .buttonStyle(.plain)
        .disabled(open == nil)
        .accessibilityLabel("Replying to \(quote.authorLabel(displayName: Agent.displayName(forWireName:))): \(quote.excerpt)")
        .accessibilityHint("Scrolls to the original message")
    }
}

/// Above the composer while he is replying: who, the first line, and ×.
struct PhoneReplyStrip: View {
    let target: ReplyTarget
    let cancel: () -> Void

    var body: some View {
        HStack(alignment: .center, spacing: 10) {
            Capsule().fill(PhoneTheme.ink.opacity(0.55)).frame(width: 3, height: 32)
            VStack(alignment: .leading, spacing: 1) {
                Text("Replying to \(target.authorLabel)")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(PhoneTheme.ink)
                Text(target.firstLine)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 0)
            Button {
                Haptics.tap()
                cancel()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .bold))
                    .foregroundStyle(PhoneTheme.ink)
                    .frame(width: 26, height: 26)
                    .background(PhoneTheme.field, in: Circle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("Cancel reply")
        }
        .padding(.horizontal, 14)
        .padding(.top, 8)
        .accessibilityElement(children: .contain)
    }
}

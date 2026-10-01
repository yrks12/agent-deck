import Foundation

/// One piece of a message, already decided. A fenced block is a *block* — its
/// own monospaced slab — and everything else is a run of attributed prose.
public enum MarkdownBlock: Hashable, Sendable {
    case prose(AttributedString)
    /// The marker is drawn, not parsed back out: "•" or "3.".
    case listItem(marker: String, AttributedString)
    case code(String, language: String?)
}

/// **Agents write Markdown, so this reads it.**
///
/// His words, reading a reply from his own desk: *"we dont handle .md output
/// style on messgae we see things like `**something bold but not **`"*. Every
/// desk on this Mac is told to bold the one thing that matters, and that
/// emphasis was arriving as literal asterisks.
///
/// **Blocks first, inline second.** Fences are split off by hand before
/// anything is parsed, because `AttributedString`'s Markdown initialiser
/// flattens a code block into an inline run — a twenty-line diff would arrive
/// as one unreadable paragraph. What is left is parsed a line at a time with
/// `.inlineOnlyPreservingWhitespace`, which keeps his line breaks (a desk's
/// report is a list of findings, not a paragraph) and still resolves bold,
/// italic, code and links.
///
/// **Parsed once.** This is more expensive than the span scan it replaces —
/// measured at 0.28 / 1.1 / 2.85 / 6.81 ms for 2k / 8k / 20k / 50k characters
/// *before* Markdown was involved — and it sits under the pane whose rebuilds
/// cost the owner a day. The memo is what makes it safe to call from a view
/// body; `MarkdownRenderingTests` pins it at one parse per distinct message.
public enum MarkdownBody {

    // MARK: rendering

    public static func blocks(of text: String) -> [MarkdownBlock] {
        if let remembered = memo.value(for: text) { return remembered }
        Diagnostics.count("markdown.parse")
        let parsed = parse(text)
        memo.remember(parsed, for: text)
        return parsed
    }

    private static func parse(_ text: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var prose: [String] = []

        func flushProse() {
            guard !prose.isEmpty else { return }
            blocks += proseBlocks(prose)
            prose = []
        }

        var fence: (language: String?, lines: [String])?
        for line in text.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("```") {
                if var open = fence {
                    blocks.append(.code(open.lines.joined(separator: "\n"), language: open.language))
                    open.lines = []
                    fence = nil
                } else {
                    flushProse()
                    let tag = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                    fence = (tag.isEmpty ? nil : tag, [])
                }
                continue
            }
            if fence != nil {
                fence?.lines.append(line)
            } else {
                prose.append(line)
            }
        }
        // An unclosed fence still closes here rather than swallowing the rest.
        // `balanced` normally prevents this; a message that genuinely ends
        // mid-fence must still draw.
        if let open = fence {
            flushProse()
            blocks.append(.code(open.lines.joined(separator: "\n"), language: open.language))
        }
        flushProse()
        return blocks
    }

    /// Prose lines, with list items lifted out so a bullet is a bullet rather
    /// than a stray hyphen at the start of a line.
    private static func proseBlocks(_ lines: [String]) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []

        func flush() {
            let joined = paragraph.joined(separator: "\n")
                .trimmingCharacters(in: .whitespacesAndNewlines)
            paragraph = []
            guard !joined.isEmpty else { return }
            blocks.append(.prose(inline(joined)))
        }

        for line in lines {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if let marker = listMarker(trimmed) {
                flush()
                blocks.append(.listItem(marker: marker.shown, inline(marker.rest)))
            } else {
                paragraph.append(line)
            }
        }
        flush()
        return blocks
    }

    private static func listMarker(_ line: String) -> (shown: String, rest: String)? {
        for bullet in ["- ", "* ", "+ "] where line.hasPrefix(bullet) {
            return ("•", String(line.dropFirst(bullet.count)))
        }
        // "1. ", "12) " — the number he wrote, kept, because a desk numbering
        // its findings means those numbers.
        let digits = line.prefix { $0.isNumber }
        guard !digits.isEmpty, digits.count <= 3 else { return nil }
        let after = line.dropFirst(digits.count)
        guard after.hasPrefix(". ") || after.hasPrefix(") ") else { return nil }
        return ("\(digits).", String(after.dropFirst(2)))
    }

    /// One run of prose. Falls back to the raw text rather than throwing: a
    /// message that will not parse must still be readable.
    private static func inline(_ text: String) -> AttributedString {
        (try? AttributedString(
            markdown: text,
            options: .init(
                allowsExtendedAttributes: true,
                interpretedSyntax: .inlineOnlyPreservingWhitespace,
                failurePolicy: .returnPartiallyParsedIfPossible)))
            ?? AttributedString(text)
    }

    /// **The whole thing as one run of prose.**
    ///
    /// A collapsed bubble is clamped to `LongMessage.collapsedLines`, and a
    /// line limit applies per `Text` — so a preview that came back as a list
    /// and a fence would be eight lines *each* and the row's height would start
    /// depending on the message again, which is the thing the collapse exists
    /// to stop. Collapsed, the preview is one passage: markers become their
    /// bullet, a fence becomes its own lines, and the clamp bites once.
    public static func singlePassage(of text: String) -> AttributedString {
        var out = AttributedString()
        for (index, block) in blocks(of: text).enumerated() {
            if index > 0 { out += AttributedString("\n") }
            switch block {
            case .prose(let rendered):
                out += rendered
            case .listItem(let marker, let rendered):
                out += AttributedString("\(marker) ") + rendered
            case .code(let body, _):
                var run = AttributedString(body)
                run.inlinePresentationIntent = .code
                out += run
            }
        }
        return out
    }

    // MARK: the cut

    /// **Makes a truncated message safe to render.**
    ///
    /// `LongMessage` collapses above 1,200 characters, and that cut lands
    /// wherever it lands. Inside `**`, everything after it would be bold to the
    /// end of the preview; inside a fence, the rest of the conversation becomes
    /// one code block; inside a link, `[half a label](htt` is drawn literally.
    ///
    /// Closes what can be closed and removes what cannot.
    public static func balanced(_ text: String) -> String {
        var out = text

        // A link is removed rather than closed: inventing the other half of a
        // URL would put a destination on screen that nobody wrote.
        if let opened = out.range(of: "[", options: .backwards),
           out.range(of: ")", range: opened.upperBound..<out.endIndex) == nil {
            out = String(out[out.startIndex..<opened.lowerBound])
        }

        let fences = out.components(separatedBy: "```").count - 1
        if fences % 2 == 1 { out += "\n```" }

        // Emphasis, longest marker first so `**` is not seen as two `*`.
        let fenced = out.components(separatedBy: "```")
        out = fenced.enumerated().map { index, part in
            // Odd indexes are inside a fence: markers there are literal text.
            guard index % 2 == 0 else { return part }
            var piece = part
            for marker in ["***", "**", "*", "`", "_"] where piece.components(separatedBy: marker).count - 1 == 1 {
                if let last = piece.range(of: marker, options: .backwards) {
                    piece = piece.replacingCharacters(in: last, with: "")
                }
            }
            return piece
        }.joined(separator: "```")

        return out
    }

    // MARK: the memo

    /// Small on purpose: this exists so a redraw is free, not so the whole
    /// day's conversation is held twice.
    private static let memo = Memo(limit: 240)

    /// Test hook. A cache that cannot be cleared cannot be measured.
    public static func forgetEverything() { memo.clear() }

    private final class Memo: @unchecked Sendable {
        private let lock = NSLock()
        private var store: [String: [MarkdownBlock]] = [:]
        private var order: [String] = []
        private let limit: Int

        init(limit: Int) { self.limit = limit }

        func value(for key: String) -> [MarkdownBlock]? {
            lock.lock(); defer { lock.unlock() }
            return store[key]
        }

        func remember(_ value: [MarkdownBlock], for key: String) {
            lock.lock(); defer { lock.unlock() }
            if store[key] == nil {
                order.append(key)
                if order.count > limit { store[order.removeFirst()] = nil }
            }
            store[key] = value
        }

        func clear() {
            lock.lock(); defer { lock.unlock() }
            store = [:]
            order = []
        }
    }
}

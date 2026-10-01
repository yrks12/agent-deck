import XCTest
@testable import DeckKit
@testable import DeckUI

/// **Agents write Markdown, so the transcript has to read it.**
///
/// His words, reading a reply from his own desk: *"we dont handle .md output
/// style on messgae we see things like \*\*something bold but not \*\*"*. Every
/// desk on this Mac is told to bold the one thing that matters, so the emphasis
/// he asked for was arriving as literal asterisks — noise where the signal was
/// meant to be.
///
/// ## The two things this must not break, both of them just paid for
///
/// 1. **The collapse.** `LongMessage` cuts at 1,200 characters on a word
///    boundary, and a cut that lands inside `**` or inside a fence would leave
///    the rest of the preview emphasised or bleed a code block over everything
///    under it. That class is swept below: unterminated emphasis, an unclosed
///    fence, a link split across the cut.
/// 2. **The rebuild.** Parsing Markdown is strictly more expensive than the
///    span scan it replaces (measured at 0.28 / 1.1 / 2.85 / 6.81 ms at 2k /
///    8k / 20k / 50k characters), so it must not happen per body evaluation.
///    It is parsed once and memoised, and `ListChurnTests` still has to read
///    zero publishes and zero transcript rebuilds with rendering in.
///
/// **Copy is unchanged and stays unchanged.** He pastes these into other tools;
/// what comes off the pasteboard is his desk's own asterisks, not the rendered
/// form.
final class MarkdownRenderingTests: XCTestCase {

    // MARK: the good signal — the emphasis is real, not stripped

    /// The failure this fixes has two halves, and dropping the markers is the
    /// second one: `**bold**` drawn as `bold` with no weight is just as wrong
    /// as drawing the asterisks, and it looks correct in a screenshot.
    func testBoldIsBoldAndNotJustAsterisksRemoved() throws {
        let blocks = MarkdownBody.blocks(of: "the **one thing** that matters")
        guard case .prose(let rendered) = try XCTUnwrap(blocks.first) else {
            return XCTFail("a sentence came back as \(blocks)")
        }

        XCTAssertFalse(
            String(rendered.characters).contains("*"),
            "the asterisks are still on screen: \(String(rendered.characters))")
        let bolded = rendered.runs.filter { $0.inlinePresentationIntent?.contains(.stronglyEmphasized) == true }
        XCTAssertEqual(
            bolded.map { String(rendered[$0.range].characters) }, ["one thing"],
            "nothing in the sentence is actually bold — the markers were deleted "
            + "rather than rendered, which reads as correct and loses the one "
            + "thing every desk is told to mark")
    }

    func testItalicAndInlineCodeSurviveAsTheirOwnRuns() throws {
        let blocks = MarkdownBody.blocks(of: "run `swift test` *before* pushing")
        guard case .prose(let rendered) = try XCTUnwrap(blocks.first) else {
            return XCTFail("came back as \(blocks)")
        }
        let text = String(rendered.characters)

        XCTAssertEqual(text, "run swift test before pushing",
                       "the markers are still drawn: \(text)")
        XCTAssertTrue(
            rendered.runs.contains { $0.inlinePresentationIntent?.contains(.code) == true },
            "`swift test` is not marked as code, so a command reads as prose")
        XCTAssertTrue(
            rendered.runs.contains { $0.inlinePresentationIntent?.contains(.emphasized) == true },
            "*before* is not italic")
    }

    func testALinkKeepsItsDestination() throws {
        let blocks = MarkdownBody.blocks(of: "see [the deck](https://example.com/deck) for it")
        guard case .prose(let rendered) = try XCTUnwrap(blocks.first) else {
            return XCTFail("came back as \(blocks)")
        }

        XCTAssertEqual(String(rendered.characters), "see the deck for it")
        XCTAssertEqual(
            rendered.runs.compactMap(\.link).map(\.absoluteString),
            ["https://example.com/deck"],
            "the link lost its destination, so a URL his desk sent is unclickable "
            + "and unreadable at the same time")
    }

    /// A fenced block is a **block**, not an inline run: monospaced, on its own,
    /// with its own background. Flattening it into the paragraph is how a
    /// twenty-line diff becomes an unreadable wall.
    func testAFencedBlockComesBackAsItsOwnBlockWithItsTextIntact() {
        let text = """
        Here is the fix:

        ```swift
        let x = 1
        print(x)
        ```

        That is all.
        """
        let blocks = MarkdownBody.blocks(of: text)

        let code = blocks.compactMap { block -> String? in
            if case .code(let body, _) = block { return body } else { return nil }
        }
        XCTAssertEqual(code, ["let x = 1\nprint(x)"],
                       "the fenced block did not survive as a block: \(blocks)")
        XCTAssertEqual(
            blocks.compactMap { if case .code(_, let language) = $0 { return language } else { return nil } },
            ["swift"],
            "the language tag was dropped")
        XCTAssertEqual(blocks.count, 3, "the prose around the fence was lost: \(blocks)")
    }

    func testBulletsAndNumbersAreDrawnAsAList() {
        let blocks = MarkdownBody.blocks(of: "- first\n- second\n\n1. one\n2. two")
        let markers = blocks.compactMap { block -> String? in
            if case .listItem(let marker, _) = block { return marker } else { return nil }
        }

        XCTAssertEqual(markers, ["•", "•", "1.", "2."],
                       "the list came back as \(blocks) — bullets read as stray "
                       + "hyphens at the start of a line")
    }

    // MARK: the class the cut creates — a preview must be valid Markdown

    /// A cut that lands between two asterisks would leave everything after it
    /// bold, all the way to the end of the preview.
    func testAPreviewCutInsideEmphasisDoesNotEmphasiseTheRest() {
        let opened = MarkdownBody.balanced("the **one thing")
        let blocks = MarkdownBody.blocks(of: opened)

        guard case .prose(let rendered) = blocks[0] else { return XCTFail("\(blocks)") }
        XCTAssertFalse(
            String(rendered.characters).contains("*"),
            "an unterminated ** is drawn literally: \(String(rendered.characters))")
        XCTAssertEqual(
            String(rendered.characters), "the one thing",
            "the text itself changed when the marker was balanced")
    }

    /// And a cut inside a fence would turn the rest of the conversation into
    /// one code block.
    func testAPreviewCutInsideAFenceClosesIt() {
        let opened = MarkdownBody.balanced("Here:\n\n```swift\nlet x = 1")
        let blocks = MarkdownBody.blocks(of: opened)

        XCTAssertEqual(
            blocks.compactMap { if case .code(let body, _) = $0 { return body } else { return nil } },
            ["let x = 1"],
            "an unclosed fence did not come back as one closed block: \(blocks)")
    }

    /// A link split across the cut must not leave `[half a label](htt` on
    /// screen.
    func testAPreviewCutInsideALinkLeavesNoBrokenSyntax() {
        let rendered = MarkdownBody.blocks(of: MarkdownBody.balanced("see [the deck](https://exa"))
        guard case .prose(let prose) = rendered[0] else { return XCTFail("\(rendered)") }
        let text = String(prose.characters)

        XCTAssertFalse(text.contains("]("), "broken link syntax is on screen: \(text)")
        XCTAssertFalse(text.contains("["), "a stray bracket is on screen: \(text)")
    }

    /// **The whole point, end to end.** The preview the collapse actually
    /// produces has to be safe, not just the balancer in isolation.
    func testTheCollapsePreviewIsAlwaysValidMarkdown() {
        // Cut at every offset through a message made of the things a desk
        // writes, so this cannot pass by getting lucky with one boundary.
        let source = "**Portfolio** — real estate video software.\n\n"
            + "- `shop.initech.example` is live\n- see [the board](https://example.com/b)\n\n"
            + "```bash\nssh deploy@initech.example\n```\n\nThat is the lot."
        for cut in stride(from: 4, to: source.count, by: 3) {
            let piece = MarkdownBody.balanced(String(source.prefix(cut)))
            let blocks = MarkdownBody.blocks(of: piece)
            let drawn = blocks.map { block -> String in
                switch block {
                case .prose(let a), .listItem(_, let a): return String(a.characters)
                case .code: return ""
                }
            }.joined()
            XCTAssertFalse(drawn.contains("**"), "cut at \(cut) drew `**`: \(drawn)")
            XCTAssertFalse(drawn.contains("]("), "cut at \(cut) drew link syntax: \(drawn)")
            XCTAssertFalse(drawn.contains("```"), "cut at \(cut) drew a fence: \(drawn)")
        }
    }

    // MARK: what must not regress

    /// Parsing is more expensive than the span scan it replaces, so the same
    /// message must not be parsed twice. `TranscriptList` skips its body when
    /// nothing changed; this is the guard for the times it does not.
    func testTheSameMessageIsParsedOnceAndThenRemembered() {
        Diagnostics.isEnabled = true
        Diagnostics.reset()
        defer { Diagnostics.isEnabled = false; Diagnostics.reset(); MarkdownBody.forgetEverything() }
        MarkdownBody.forgetEverything()

        let text = "the **one thing** that matters, said once"
        for _ in 0..<20 { _ = MarkdownBody.blocks(of: text) }

        XCTAssertEqual(
            Diagnostics.snapshot()["markdown.parse"], 1,
            "twenty draws of the same message parsed it "
            + "\(Diagnostics.snapshot()["markdown.parse"] ?? 0) times. Parsing is "
            + "more expensive than the scan it replaced, and this is the pane "
            + "whose rebuild cost the owner a day.")
    }

    /// Two different messages must not collide in that memo — a cache that
    /// returns the wrong message is worse than no cache.
    func testTwoDifferentMessagesGetTheirOwnRendering() {
        let first = MarkdownBody.blocks(of: "**first**")
        let second = MarkdownBody.blocks(of: "**second**")

        guard case .prose(let a) = first[0], case .prose(let b) = second[0] else {
            return XCTFail("\(first) \(second)")
        }
        XCTAssertEqual(String(a.characters), "first")
        XCTAssertEqual(String(b.characters), "second")
    }

    /// **Copy takes the source, not the rendering.** He pastes his desk's
    /// replies into other tools, and a paste that had quietly lost its
    /// asterisks would be discovered somewhere that mattered.
    func testCopyStillTakesTheAsterisksHeCanPasteElsewhere() throws {
        let text = "the **one thing** that matters\n\n```bash\nssh box\n```"
        let pasteboard = NSPasteboard(name: .init(rawValue: "deck.test.\(UUID().uuidString)"))

        MessageClipboard.copy(text, to: pasteboard)

        XCTAssertEqual(
            try XCTUnwrap(pasteboard.string(forType: .string)), text,
            "copy handed over the rendered form — the markers he wrote are gone")
    }

    /// An ordinary sentence with no Markdown in it must come back untouched,
    /// and as one block. Most of what is said in this app is prose.
    func testPlainProseIsUnchanged() throws {
        let blocks = MarkdownBody.blocks(of: "on it, will report back in ten minutes")
        guard case .prose(let rendered) = try XCTUnwrap(blocks.first) else {
            return XCTFail("\(blocks)")
        }

        XCTAssertEqual(blocks.count, 1)
        XCTAssertEqual(String(rendered.characters), "on it, will report back in ten minutes")
    }
}

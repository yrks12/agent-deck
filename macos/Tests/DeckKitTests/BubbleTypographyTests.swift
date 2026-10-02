import XCTest
import SwiftUI
import AppKit
@testable import DeckKit
@testable import DeckUI

/// **What a desk wrote has to look like what it meant, inside the bubble.**
///
/// `.grok-reference/grok-03-links-code-rollups.jpg`: `/healthz`,
/// `studio-preview-pr15`, `pr15`, `:8083` and `188f1458` are drawn as pink
/// monospaced chips **inside** the bubble, and
/// `https://github.com/acme/studio/pull/15` is blue, underlined and
/// clickable. Both matter to him for the same reason: those are the two things
/// in a desk's reply that he acts on — an id he pastes somewhere, and a link he
/// opens — and prose-shaped text hides both.
///
/// **The parser is not touched, and measuring first is why.**
///
/// The obvious reading of the frames is that ours needs an autolinker: Markdown
/// only links `[label](url)` and his desks paste bare URLs. One was written,
/// and then deleting it again changed **nothing** — every assertion below stayed
/// green. `AttributedString`'s own Markdown parser already autolinks a bare
/// `https://`, so the link half of `grok-03` was working before anyone looked,
/// and an `NSDataDetector` pass over every message would have been forty lines
/// of code doing what Foundation already did. What is left here is the pin, so
/// the next person can see that it works rather than adding it again.
///
/// The one thing that really was missing is the chip, and that is a drawing
/// question: `MarkdownBody` already marks the runs and memoises the parse, and
/// `DeckKit` imports no UI framework by rule, so the colour lives in `DeckUI`.
final class BubbleTypographyTests: XCTestCase {

    // MARK: a URL a desk wrote is a link he can open

    /// Nobody types `[label](url)` into a terminal session — they paste the
    /// URL — and a URL drawn as prose is one he has to select, copy and paste
    /// into a browser. **Already true**; pinned so it stays true.
    func testABareURLInAReplyIsALinkHeCanClick() throws {
        let rendered = try prose(of:
            "פורמט: https://github.com/acme/studio/pull/15 בדרך.")

        let links = rendered.runs.compactMap(\.link)
        XCTAssertEqual(
            links.map(\.absoluteString),
            ["https://github.com/acme/studio/pull/15"],
            "a URL his desk pasted came back as \(links) — it is drawn as prose, "
            + "so opening it means selecting it, copying it and pasting it into "
            + "a browser")
    }

    /// The text is unchanged: linking is an attribute over the words he wrote,
    /// never a rewrite of them.
    func testLinkingAURLChangesNoneOfTheWordsAroundIt() throws {
        let source = "PR open at https://github.com/acme/shop/pull/9 — merge after CI."
        let rendered = try prose(of: source)

        XCTAssertEqual(
            String(rendered.characters), source,
            "linking rewrote the line to '\(String(rendered.characters))'")
    }

    /// A markdown link still wins: it has a label, and replacing that label
    /// with the raw URL would be a regression.
    func testAMarkdownLinkIsStillDrawnWithItsOwnLabel() throws {
        let rendered = try prose(of: "see [the PR](https://github.com/acme/shop/pull/9)")

        XCTAssertEqual(
            String(rendered.characters), "see the PR",
            "a markdown link lost its label: '\(String(rendered.characters))'")
        XCTAssertEqual(
            rendered.runs.compactMap(\.link).map(\.absoluteString),
            ["https://github.com/acme/shop/pull/9"],
            "a markdown link lost its destination")
    }

    /// **The sweep, and the half that matters most.** Anything that is not a
    /// web address is left alone — a path, a `1.2.3` version, a `desk:name`
    /// thread id, a port, a Hebrew line. Turning one of those into a link is
    /// worse than missing one, because it is a control that does nothing.
    ///
    /// An email is deliberately not in this list: `AttributedString`'s own
    /// Markdown parser already links one as `mailto:`, that predates this and
    /// it opens Mail, which is a control that works. What is asserted is that
    /// nothing here becomes a **web** link this app invented.
    func testNothingThatIsNotAWebAddressIsTurnedIntoALink() throws {
        for line in [
            "the build is 1.2.3 and the tag is v1.2.3",
            "it is in ~/Projects/acme/Sources/Main.swift",
            "thread direct:cos, peer:cos|acme",
            "run it on :8083 then check /healthz",
            "מוכן ל-E2E של UX #15",
            "container studio-preview-pr15 on 188f1458",
        ] {
            let rendered = try prose(of: line)
            let web = rendered.runs.compactMap(\.link)
                .filter { ($0.scheme ?? "").hasPrefix("http") }
            XCTAssertEqual(
                web.map(\.absoluteString), [],
                "'\(line)' produced a web link. A control that does nothing is "
                + "worse than a URL he has to copy.")
        }
    }

    // MARK: inline code is a chip, not prose

    /// `pr15` and `:8083` are ids he pastes. They have to be findable in a wall
    /// of Hebrew and English at a glance, which is what the chip is for.
    func testInlineCodeIsDrawnAsAMonospacedTintedChip() throws {
        let rendered = try prose(of: "מוכן ל-E2E של UX #15 (`studio-preview-`) על `pr15`.")

        let chips = rendered.runs.filter {
            $0.inlinePresentationIntent?.contains(.code) == true
        }
        XCTAssertEqual(
            chips.count, 2,
            "the two inline code spans came back as \(chips.count) runs, so the "
            + "parser is not marking them and there is nothing to style")

        for chip in chips {
            XCTAssertNotNil(
                MarkdownStyle.styled(rendered).runs.first(where: {
                    $0.range == chip.range
                })?.foregroundColor,
                "an inline code span is drawn in the bubble's own text colour, "
                + "so `pr15` in a wall of prose reads as prose")
        }
    }

    /// Styling is an overlay: not one character of what the desk wrote changes,
    /// and neither does anything that is not code.
    func testStylingCodeLeavesEveryOtherRunAlone() throws {
        let rendered = try prose(of: "**bold** and `code` and plain")
        let styled = MarkdownStyle.styled(rendered)

        XCTAssertEqual(
            String(styled.characters), String(rendered.characters),
            "styling rewrote the message to '\(String(styled.characters))'")
        let tinted = styled.runs.filter { $0.foregroundColor != nil }
        XCTAssertEqual(
            tinted.count, 1,
            "\(tinted.count) runs were tinted in a line with one code span in "
            + "it, so the chip colour is leaking over his desk's prose")
    }

    /// A message with no code in it is handed straight back — no walk, no copy.
    func testAMessageWithNoCodeInItIsUntouched() throws {
        let rendered = try prose(of: "on it, will report at 21:00")

        XCTAssertEqual(
            MarkdownStyle.styled(rendered), rendered,
            "a plain reply came back changed by the code styling")
    }

    // MARK: the chip has to be readable, in both appearances

    /// **4.5:1 on the bubble it is drawn on, light and dark.** A chip he cannot
    /// read is worse than no chip: it takes an id he was going to copy and
    /// makes it the least legible thing in the message.
    func testTheCodeChipClearsTheContrastBarOnABubbleInBothAppearances() throws {
        var failures: [String] = []
        for (label, name) in [("light", NSAppearance.Name.aqua), ("dark", .darkAqua)] {
            let text = try resolved(NSColor(MarkdownStyle.codeTint), name)
            let bubble = try resolved(.controlBackgroundColor, name)
            let measured = try ratio(text, on: bubble)
            if measured < 4.5 {
                failures.append(String(format: "%@ measured %.2f:1", label, measured))
            }
        }
        XCTAssertEqual(
            failures, [],
            "the inline code chip is under the 4.5:1 WCAG asks of body text on "
            + "an agent's bubble: \(failures.joined(separator: ", "))")
    }

    /// **The calibration.** The darkening above is only worth having if plain
    /// system pink really does fail — a green against a bar nothing could fail
    /// proves nothing.
    func testTheBarIsRealBecausePlainSystemPinkFailsItOnALightBubble() throws {
        let plain = try resolved(.systemPink, .aqua)
        let bubble = try resolved(.controlBackgroundColor, .aqua)
        let measured = try ratio(plain, on: bubble)

        XCTAssertLessThan(
            measured, 4.5,
            String(format:
                "plain system pink measured %.2f:1 on a light bubble, so it would "
                + "have passed and the darkening is unnecessary — check the "
                + "formula before deleting it", measured))
    }

    /// And it must still *be* pink. Darkened past recognition it stops reading
    /// as a chip and becomes a second body colour nobody can explain.
    func testTheDarkenedPinkIsStillPink() throws {
        let text = try resolved(NSColor(MarkdownStyle.codeTint), .aqua)

        XCTAssertGreaterThan(
            text.redComponent, text.greenComponent,
            "the code chip is no longer warm — it will not read as a chip")
        XCTAssertGreaterThan(text.redComponent, text.blueComponent)
    }

    // MARK: harness

    /// WCAG 2.1 relative luminance, from sRGB components.
    private func luminance(_ color: NSColor) throws -> Double {
        let srgb = try XCTUnwrap(
            color.usingColorSpace(.sRGB), "\(color) has no sRGB representation")
        func channel(_ raw: CGFloat) -> Double {
            let value = Double(raw)
            return value <= 0.040_45 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(srgb.redComponent)
            + 0.7152 * channel(srgb.greenComponent)
            + 0.0722 * channel(srgb.blueComponent)
    }

    private func ratio(_ foreground: NSColor, on background: NSColor) throws -> Double {
        let first = try luminance(foreground)
        let second = try luminance(background)
        return (max(first, second) + 0.05) / (min(first, second) + 0.05)
    }

    /// Resolves a colour the way the screen will, in one appearance.
    private func resolved(_ color: NSColor, _ name: NSAppearance.Name) throws -> NSColor {
        let appearance = try XCTUnwrap(NSAppearance(named: name))
        var out = color
        appearance.performAsCurrentDrawingAppearance { out = color.usingColorSpace(.sRGB) ?? color }
        return out
    }

    /// The first prose block of a message, as `MarkdownBody` decided it.
    private func prose(of text: String) throws -> AttributedString {
        for block in MarkdownBody.blocks(of: text) {
            if case .prose(let rendered) = block { return rendered }
        }
        throw XCTSkip("no prose block in \(text)")
    }
}

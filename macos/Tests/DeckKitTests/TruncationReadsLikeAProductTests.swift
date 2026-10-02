import XCTest
import AppKit
@testable import DeckKit
@testable import DeckUI

/// **A cut-off message should read like a product, not like a diagnostic.**
///
/// Ours printed `Show full message — 1,916 more characters` under every wall of
/// pasted text. Grok's frames end the preview on a plain `…` and put a quiet
/// **Show more** underneath, and that is the difference between a conversation
/// and a log viewer: he is deciding whether to read the rest, not auditing a
/// byte count.
///
/// **The collapse itself stays exactly as it is** — 1,200 characters, a
/// 700-character preview cut on a word boundary, eight lines. That policy beat
/// a real freeze (`LongMessage`, `LongMessageLayoutTests`) and nothing here
/// touches it. Only the affordance changes.
///
/// ## The one thing that must NOT get quieter
///
/// Somebody reading this with VoiceOver cannot see that the bubble is short,
/// and "Show more" alone does not say whether it hides a sentence or a
/// dissertation. So the count moves off the screen and into the spoken label —
/// it is not deleted. That split is the whole point of this file, and it is why
/// the assertions come in pairs.
final class TruncationReadsLikeAProductTests: XCTestCase {

    // MARK: what he reads

    func testTheButtonHeSeesSaysShowMoreAndCountsNothing() {
        XCTAssertEqual(
            LongMessage.expandTitle, "Show more",
            "the affordance under a cut-off message reads "
            + "'\(LongMessage.expandTitle)'. Grok's is a quiet 'Show more', and "
            + "he is deciding whether to read the rest, not auditing a byte "
            + "count.")
        XCTAssertEqual(
            LongMessage.collapseTitle, "Show less",
            "closing it again reads '\(LongMessage.collapseTitle)'")
        XCTAssertFalse(
            LongMessage.expandTitle.contains(where: \.isNumber),
            "there is a number on the button he reads: '\(LongMessage.expandTitle)'")
    }

    /// The preview's own ending is the truncation mark. `612 more characters`
    /// was doing that job in words.
    func testAPreviewEndsOnAPlainEllipsis() {
        let preview = LongMessage.preview(of: Self.wall)

        XCTAssertTrue(
            preview.hasSuffix("…"),
            "a cut-off message ends '\(preview.suffix(24))' rather than on the "
            + "one character that says there is more")
        XCTAssertFalse(
            preview.contains("more characters"),
            "the preview itself is counting characters at him: "
            + "'\(preview.suffix(48))'")
    }

    // MARK: what a screen reader hears — the count, still

    func testTheSpokenAffordanceStillSaysHowMuchIsBehindIt() {
        let spoken = LongMessage.spokenExpandLabel(of: Self.wall)

        XCTAssertTrue(
            spoken.hasPrefix(LongMessage.expandTitle),
            "the control announces itself as '\(spoken)' — it has to lead with "
            + "what it does")
        XCTAssertTrue(
            spoken.contains("more characters"),
            "'\(spoken)' does not say how much is hidden. Somebody reading this "
            + "with VoiceOver cannot see that the bubble is short, and 'Show "
            + "more' alone does not distinguish a sentence from a dissertation.")
        XCTAssertTrue(
            spoken.contains("1,922"),
            "the spoken label lost the number: '\(spoken)'")
    }

    /// The count is exact, not "lots". A separator, because he reads these on a
    /// Mac with a UK locale and 1916 is not a number anyone parses at a glance.
    func testTheCountIsTheRealNumberOfCharactersLeft() {
        let hidden = LongMessage.hiddenSummary(of: Self.wall)
        let remaining = Self.wall.count - LongMessage.preview(of: Self.wall).count

        XCTAssertEqual(
            hidden, "1,922 more characters",
            "what is hidden reads '\(hidden)' for a message with \(remaining) "
            + "characters left in it")
    }

    // MARK: the constraint the whole collapse lives under

    /// **Nothing is lost.** He pasted that mandate deliberately, and a quieter
    /// affordance must not become a quieter truncation.
    func testTheCollapseThresholdAndThePreviewAreExactlyWhatTheyWere() {
        XCTAssertEqual(
            LongMessage.collapseAboveCharacters, 1_200,
            "the collapse threshold moved. That policy beat a real freeze and "
            + "this change was only ever about the affordance under it.")
        XCTAssertEqual(
            LongMessage.previewCharacters, 700,
            "the preview length moved")
        XCTAssertEqual(
            LongMessage.collapsedLines, 8,
            "the line clamp moved, and the clamp is what fixes the row's height")
    }

    func testCopyStillTakesEveryCharacterOfTheSource() {
        let pasteboard = NSPasteboard(name: .init(rawValue: "deck.truncation.test"))
        MessageClipboard.copy(Self.wall, to: pasteboard)

        XCTAssertEqual(
            pasteboard.string(forType: .string), Self.wall,
            "copying a collapsed message took \(pasteboard.string(forType: .string)?.count ?? 0) "
            + "characters of \(Self.wall.count). He finds that out after pasting "
            + "it somewhere that mattered.")
    }

    /// An ordinary reply — a paragraph or three, which is most of what a desk
    /// says — is not collapsed and gets no affordance at all.
    func testAnOrdinaryReplyIsNotGivenAShowMoreAtAll() {
        let ordinary = String(repeating: "a short answer to a short question. ", count: 8)

        XCTAssertFalse(
            LongMessage.isLong(ordinary),
            "a \(ordinary.count)-character reply was collapsed, so he now has to "
            + "press a button to read three sentences")
        XCTAssertEqual(
            LongMessage.preview(of: ordinary), ordinary,
            "a reply that is not collapsed came back changed")
    }

    /// **The sweep.** Every length either side of the threshold, including the
    /// pathological one — a single enormous token with no space in it, where
    /// there is no word boundary to cut on.
    func testEveryLengthEitherSideOfTheThresholdEndsWithSomethingReadable() {
        for length in [1, 700, 1_199, 1_200, 1_201, 5_000, 50_000] {
            let text = String(repeating: "word ", count: max(length / 5, 1))
            let preview = LongMessage.preview(of: text)
            if LongMessage.isLong(text) {
                XCTAssertTrue(
                    preview.hasSuffix("…"),
                    "a \(text.count)-character message previews as "
                    + "'\(preview.suffix(20))' with nothing saying there is more")
            } else {
                XCTAssertEqual(
                    preview, text,
                    "a \(text.count)-character message was truncated below the "
                    + "\(LongMessage.collapseAboveCharacters)-character threshold")
            }
        }

        let blob = String(repeating: "A", count: 4_000)
        XCTAssertTrue(
            LongMessage.preview(of: blob).hasSuffix("…"),
            "a 4,000-character token with no space in it previews as "
            + "'\(LongMessage.preview(of: blob).suffix(20))'")
    }

    /// A wall of pasted text with 1,922 characters behind the preview — the shape
    /// of the mandates he actually pastes into these threads.
    static let wall = String(repeating:
        "The order is set by distance-to-revenue, not by which lane is loudest, "
        + "because the product already exists and the only gap is selling it. ",
        count: 19)
}

import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **A long message must not cost more to lay out than a long message.**
///
/// His words, 2026-09-06, scrolled to the mandate he had pasted into his own
/// thread: *"again when i get to this long message its stuck"*.
///
/// ## Why 374 green tests did not catch it
///
/// `LayoutSettlesTests` varies the transcript by **number of rows** — 40, 200,
/// 800, 2000 — and every row in it is one short line. It reported ~8-10ms flat
/// and concluded the transcript was not a driver. That measurement is correct
/// and it is about the wrong axis. He has *one* row of several thousand
/// characters, and that single row is what wedges the pane. **This file varies
/// LENGTH.**
///
/// ## What was measured, 2026-09-06, headless at a 900pt pane
///
/// One ideal-size query on the real `MessageBubble`, by character count:
///
/// ```
///  chars |  with fixedSize | without | ratio | height with | height without
///    200 |          1.46ms |  0.38ms |  3.8x |        48pt |           48pt
///  2,000 |          4.88ms |  0.94ms |  5.2x |       496pt |          496pt
///  8,000 |         33.33ms | 10.05ms |  3.3x |      1952pt |         1952pt
/// 20,000 |         43.70ms |  5.43ms |  8.0x |      4912pt |         4912pt
/// 50,000 |        147.82ms | 32.09ms |  4.6x |     12272pt |        12272pt
/// ```
///
/// The height columns are **identical at every length**. So
/// `.fixedSize(horizontal: false, vertical: true)` on the message body did not
/// change one wrapped line — it only made answering "how big are you" three to
/// eight times more expensive. Inside a vertical `ScrollView` the height
/// proposal is already unbounded, so the modifier that exists to stop `Text`
/// truncating had nothing to stop.
///
/// At 44ms a query, any container asking more than ~22 times a second is
/// pegged, and the owner's sample has `LayoutEngineBox.sizeThatFits` and
/// `ViewLayoutEngine.sizeThatFits` as its two hottest frames.
///
/// ## What this file can and cannot prove — read before trusting it
///
/// It measures **cost per layout query**, which is deterministic and shows up
/// headless. It does **not** reproduce the 100% CPU loop: an off-screen window
/// has no display cycle to re-enter SwiftUI, and a 50s watch of the real
/// `DeckRootView` with a 20,000-character message reported **2 layout passes**
/// whether the message was 200 characters or 20,000. Confirming the spin is
/// gone needs the real app on the owner's Mac against his real thread, and
/// that measurement has NOT been taken here.
///
/// **The ramp is 30-40 seconds.** Measured on the real app by the coordinator:
/// a build that ends up pegged reads 0.6%, 0.0%, 9.4% before it reaches 99%.
/// Any live check shorter than 40 seconds reports a false pass, and doing that
/// twice is what put a broken build in his hands.
///
/// ## MAKING THE LONG ROW CHEAPER WAS NOT ENOUGH, AND HE NAMED THE TRIGGER
///
/// The definite width above took one size query from 38.77ms to 0.70ms and is
/// still worth having. It did not fix the app. Measured on the merged build,
/// hands-off, three minutes:
///
/// ```
/// t=20s 99.9%  t=40s 99.3%  …  t=180s 99.4%   STAT RN
/// sample: FallbackAlignment/setFont 52 · TextField|NSTextView|TextEditor 83
///         requests to the deck in that minute: 0
/// ```
///
/// The text engine is the hottest named chain in the process, hotter than the
/// alignment work three earlier fixes chased, and the app had no cycles left to
/// talk to its own deck. His words, watching it: *"app stuck again"*, *"when i
/// scroll to his answer"*, *"we dont klnow how to handle long text"*.
///
/// A `sizeThatFits` measurement cannot see this. Laying out line fragments for
/// a 20,000-character attributed string is not a size query, so a file that
/// only times size queries can be entirely green — as this one was — while the
/// row costs a core to draw.
///
/// **So the decision is to stop drawing it.** Above `LongMessage
/// .collapseAboveCharacters` the transcript lays out a few hundred characters
/// and a button; the rest happens when he asks for it. That removes the
/// unbounded layout instead of trying to make it cheap, and it is what every
/// chat client does with a wall of pasted text.
///
/// The tests below changed shape with it. "Ten times the characters, ten times
/// the height" was the right assertion for a pane that draws everything and is
/// the wrong one now — the whole point is that the collapsed height is the
/// **same number** at 2,000 characters and 200,000. What replaces it is
/// stricter about the thing that actually matters: nothing he pasted is lost.
///
/// **This file still cannot see the runtime loop.** It measures cost per query
/// and layout heights, both deterministic and both visible headless. An
/// off-screen window has no display cycle to re-enter SwiftUI. The only thing
/// that can call the app quiet is
/// `YOS_SCREEN_IS_FREE=1 Scripts/idle-cpu.sh <deck-url>`.
@MainActor
final class LongMessageLayoutTests: XCTestCase {

    /// A multi-paragraph mandate — the shape of thing he actually pastes, not
    /// one repeated character. Word lengths and paragraph breaks are what the
    /// text engine's cost is made of.
    static func mandate(characters: Int) -> String {
        let paragraph = """
        Take the roster end to end and make the conversation say what is happening \
        without me having to go and look somewhere else for it, because the whole \
        point of this window is that I can tell at a glance whether a desk is \
        working or stuck or finished. Every state needs a still picture and no \
        view may align on a text baseline.
        """
        var out = ""
        while out.count < characters { out += paragraph + "\n\n" }
        return String(out.prefix(characters))
    }

    private func message(_ text: String) -> Message {
        Message(id: "m1", cursor: "0001", threadID: "direct:chief",
                author: "chief", role: .agent,
                sentAt: Date(timeIntervalSince1970: 1_700_000_000), text: text)
    }

    // MARK: DETECTOR — the cost of one row must stay near the text's own cost

    /// **THE detector. A long row must cost what a short row costs — under
    /// EVERY size question the layout system can ask it.**
    ///
    /// Both question kinds are swept, and that is the whole point of this test
    /// rather than a detail of it. A wrapping `Text` is asked two different
    /// things during one layout: "how big would you *like* to be" (an ideal
    /// query, no width proposed) and "how tall are you *at this width*". The
    /// two have opposite cost profiles, and **the first attempt at this fix
    /// optimised one and ruined the other**:
    ///
    /// ```
    ///                        ideal query      proposed width
    ///   fixedSize (shipped)      26.6x                  ~1x
    ///   fixedSize removed          ~1x                  16x     <- the bad fix
    ///   definite width             1.0x                 1.0x    <- this
    /// ```
    ///
    /// A detector that measured only the ideal query would have gone green on
    /// the middle row and handed him a build that stalls on every real layout
    /// pass instead of every ideal one. So this asks both, and the property is
    /// **flatness in length**, normalised against a short row on the same Mac
    /// — never a wall-clock ceiling, which is a statement about this machine on
    /// this afternoon.
    func testALongRowCostsWhatAShortRowCostsUnderEverySizeQuery() {
        for proposedWidth in [nil, CGFloat(900)] {
            let named = proposedWidth == nil ? "ideal width" : "a proposed 900pt pane"
            let short = median(7) {
                self.milliseconds(MessageBubble(message: self.message(Self.mandate(characters: 200))),
                                  width: proposedWidth)
            }
            XCTAssertGreaterThan(
                short, 0,
                "the short row measured as zero at \(named), so the ratio below "
                + "divides by noise and this test proves nothing")

            for characters in [20_000, 50_000] {
                let long = median(7) {
                    self.milliseconds(MessageBubble(message: self.message(Self.mandate(characters: characters))),
                                      width: proposedWidth)
                }
                XCTAssertLessThan(
                    long, short * 6,
                    String(format:
                        "at %@, a %d-character message costs %.2fms where a "
                        + "200-character one costs %.2fms — %.1fx. He is scrolled to "
                        + "exactly this row when the pane wedges, and the two hottest "
                        + "frames in his sample are sizeThatFits.",
                        named, characters, long, short, long / max(short, 0.001)))
            }
        }
    }

    /// **NOT the detector for this defect — it passes while the app is
    /// broken, and it is labelled so nobody reads it as cover.**
    ///
    /// Measured red-state: cost per character is roughly flat, because the
    /// fault is a constant multiplier on an already-linear cost, not a change
    /// of order. What this guards is the *different* fault of a genuinely
    /// superlinear row — the one where 20,000 characters costs a hundred times
    /// 2,000 rather than ten times — which no other test here would see.
    func testTheCostPerCharacterDoesNotClimbWithLength() {
        var perCharacter: [(Int, Double)] = []
        for characters in [2_000, 8_000, 20_000] {
            let text = Self.mandate(characters: characters)
            let ms = median(5) { self.idealMilliseconds(MessageBubble(message: self.message(text))) }
            perCharacter.append((characters, ms / Double(characters) * 1000))
        }

        let shortest = perCharacter.first!.1
        let longest = perCharacter.last!.1
        XCTAssertLessThan(
            longest, shortest * 4,
            "the cost per 1,000 characters climbs from "
            + String(format: "%.3fms at 2,000 to %.3fms at 20,000", shortest, longest)
            + " — the row gets disproportionately more expensive the more he "
            + "writes, which is the shape of the complaint: short threads are "
            + "fine and the long message is where it stops.")
    }

    // MARK: the good signal that matters most — none of his text is lost

    /// **THE fix, measured: a collapsed row is the same height whatever is in
    /// it.**
    ///
    /// This is what stops the transcript's total height depending on the length
    /// of any one message, and it is the property that makes scrolling to his
    /// answer stop being an event. Exact equality, not a tolerance: the
    /// collapsed bubble draws a fixed number of lines of a fixed-length
    /// preview, so any difference at all means the length has leaked back into
    /// the layout.
    func testACollapsedMessageIsTheSameHeightWhateverItsLength() {
        let heights = [2_000, 20_000, 200_000].map { bubbleHeight(characters: $0, pane: 900) }

        XCTAssertGreaterThan(
            heights[0], 1,
            "the collapsed bubble did not lay out at all (\(heights)), so the "
            + "equality below is the equality of three empty rows")
        XCTAssertEqual(
            Set(heights).count, 1,
            "collapsed heights are \(heights) for 2,000 / 20,000 / 200,000 "
            + "characters. The transcript's height still depends on how much he "
            + "pasted, which is the thing that wedges the pane when he scrolls "
            + "to it.")
    }

    /// **And the same is true of a message written the way a desk writes.**
    ///
    /// The fixture above is plain prose, and every reply he actually gets has
    /// bold, a list and a fenced block in it. This is the same property asked
    /// of the shape the app really draws.
    ///
    /// **Be straight about what it does not prove.** A line limit applies per
    /// `Text`, so a collapsed preview drawn as several blocks could in
    /// principle be eight lines *each*; `MarkdownBody.singlePassage` draws it as
    /// one instead. This test passes either way — the preview is always the
    /// first 700 characters, so its block structure does not vary with the
    /// length of the message behind it. Single-passage is kept because one
    /// `Text` and one clamp is an invariant anyone can check by reading it, not
    /// because a measurement here distinguished the two.
    func testACollapsedMarkdownMessageIsTheSameHeightWhateverItsLength() {
        let heights = [2_000, 20_000, 200_000].map {
            bubbleHeight(of: Self.markdownMandate(characters: $0), pane: 900)
        }

        XCTAssertGreaterThan(heights[0], 1, "the collapsed bubble did not lay out (\(heights))")
        XCTAssertEqual(
            Set(heights).count, 1,
            "a message with bold, a list and a fenced block in it collapses to "
            + "\(heights) at 2,000 / 20,000 / 200,000 characters. Agents write "
            + "Markdown — this is the shape of every reply he gets.")
    }

    /// And it is genuinely smaller than the thing it replaced — a "collapse"
    /// that drew the whole message anyway would pass the test above by drawing
    /// three identical walls.
    func testACollapsedMessageIsShorterThanTheMessageItIsHiding() {
        let collapsed = bubbleHeight(characters: 20_000, pane: 900)
        let expanded = bubbleHeight(characters: 20_000, pane: 900, expanded: true)

        XCTAssertGreaterThan(expanded, collapsed * 10,
                             String(format: "collapsed %.0fpt, expanded %.0fpt — the "
                                    + "collapsed row is not hiding anything",
                                    collapsed, expanded))
    }

    // MARK: the good signal that matters most — none of his text is lost

    /// **Expanded, every character he pasted is laid out.**
    ///
    /// He pasted that mandate deliberately, and a pane that got fast by
    /// dropping half of it would be a worse product than the slow one. Ten
    /// times the characters, ten times the height — the assertion that used to
    /// guard the collapsed bubble now guards the expanded one, which is where
    /// the whole message lives.
    func testEveryLineOfALongMessageIsStillLaidOutOnceHeAsksForIt() {
        let short = bubbleHeight(characters: 2_000, pane: 900, expanded: true)
        let long = bubbleHeight(characters: 20_000, pane: 900, expanded: true)

        XCTAssertGreaterThan(short, 1, "the expanded short message did not lay out at all")
        let ratio = long / short
        XCTAssertGreaterThan(
            ratio, 8.5,
            String(format:
                "ten times the characters produced only %.1fx the height "
                + "(%.0fpt vs %.0fpt). His message is being truncated even after he "
                + "asked to see it — losing what he pasted is worse than a slow pane.",
                ratio, long, short))
        XCTAssertLessThan(
            ratio, 12,
            String(format: "%.1fx the height for ten times the characters — the "
                   + "row is being laid out more than once, or padded", ratio))
    }

    /// **And copy takes the whole thing, collapsed or not.**
    ///
    /// Selection can only reach what is drawn, so a collapsed bubble would
    /// otherwise silently copy the preview — the quietest possible way to lose
    /// his mandate, discovered only once he had pasted it somewhere else.
    func testCopyingACollapsedMessageTakesEveryCharacter() throws {
        let text = Self.mandate(characters: 20_000)
        let pasteboard = NSPasteboard(name: .init(rawValue: "deck.test.\(UUID().uuidString)"))

        MessageClipboard.copy(text, to: pasteboard)

        let copied = try XCTUnwrap(pasteboard.string(forType: .string),
                                   "nothing was put on the pasteboard at all")
        XCTAssertEqual(
            copied.count, 20_000,
            "copy took \(copied.count) of 20,000 characters — the collapsed "
            + "preview, not the message. He would find that out after pasting it "
            + "somewhere that mattered.")
        XCTAssertEqual(copied, text, "the text came back altered")
    }

    /// The preview cuts on a word, and says how much it is holding back. A
    /// control that says only "…" is one nobody presses.
    func testThePreviewEndsOnAWordAndSaysWhatItIsHiding() {
        let text = Self.mandate(characters: 20_000)
        let preview = LongMessage.preview(of: text)

        XCTAssertTrue(preview.hasSuffix("…"), "the preview does not say it is cut")
        XCTAssertFalse(
            preview.dropLast().hasSuffix(" "),
            "the preview ends on a space before its ellipsis")
        XCTAssertLessThanOrEqual(preview.count, LongMessage.previewCharacters + 1)
        XCTAssertTrue(
            text.hasPrefix(preview.dropLast().trimmingCharacters(in: .whitespacesAndNewlines)),
            "the preview is not the opening of his message — it has been "
            + "reordered or reflowed, so what he reads first is not what he wrote")
        XCTAssertTrue(
            LongMessage.hiddenSummary(of: text).contains("more characters"),
            "the button says nothing about how much is behind it: "
            + "\(LongMessage.hiddenSummary(of: text))")
    }

    /// An ordinary reply is never collapsed. Most of what a desk says is a
    /// paragraph or three, and putting a "Show full message" button under those
    /// would make the conversation unreadable to save nothing.
    func testAnOrdinaryReplyIsNotCollapsedAtAll() {
        XCTAssertFalse(LongMessage.isLong(Self.mandate(characters: 400)))
        XCTAssertFalse(LongMessage.isLong(Self.mandate(characters: 1_200)))
        XCTAssertTrue(LongMessage.isLong(Self.mandate(characters: 1_201)))
        XCTAssertEqual(
            LongMessage.preview(of: "on it"), "on it",
            "a short reply is rewritten on its way to the screen")
    }

    /// **The class, not the one view.** His pasted messages and the desk's
    /// long replies are the same code path — a bubble is a bubble — but a
    /// tool-call card's reason is a second place unbounded text reaches the
    /// transcript, and it is drawn in the same LazyVStack.
    func testAToolCallCardWithAWallOfTextInItIsBoundedToo() {
        let heights = [2_000, 20_000, 200_000].map { cardHeight(whyCharacters: $0, pane: 900) }

        XCTAssertGreaterThan(heights[0], 1, "the card did not lay out (\(heights))")
        XCTAssertEqual(
            Set(heights).count, 1,
            "a tool-call card whose reason is 2,000 / 20,000 / 200,000 characters "
            + "is \(heights) tall. The card sits in the same transcript as the "
            + "bubbles and re-measures with them.")
    }

    /// **A two-word reply must still look like a two-word reply.**
    ///
    /// The fix gives long messages a *definite* width. Applied to everything it
    /// would put "on it" in a 560pt slab, which is not a chat window. The rule
    /// is that the pin starts above `BubbleWidth.pinAboveCharacters`, and above
    /// that a message already fills the bubble at any pane width — so the pin
    /// is invisible exactly where it is needed and absent everywhere else.
    func testAShortReplyIsStillDrawnAtItsOwnWidth() {
        // Just under the pin, and the same text just over it. Below the
        // threshold the message may use the whole pane, so it wraps into fewer
        // lines than 560pt would give it; above, it is pinned. Height is what
        // makes that visible.
        let unpinned = bubbleHeight(characters: 190, pane: 900)
        let pinned = bubbleHeight(characters: 210, pane: 900)

        XCTAssertGreaterThan(unpinned, 0, "the short bubble did not draw at all")
        XCTAssertLessThanOrEqual(
            unpinned, pinned,
            String(format:
                "a 190-character reply is %.0fpt tall and a 210-character one is "
                + "%.0fpt. The shorter message is wrapping into MORE lines than the "
                + "longer one, which means the width pin has been applied below its "
                + "threshold and the conversation now reads as a column of slabs.",
                unpinned, pinned))
    }

    /// **A narrow window must not clip his message off the side.**
    ///
    /// The pin is a *definite* width, and a definite width wider than the pane
    /// is content a vertical `ScrollView` will not let him scroll to. So it is
    /// taken from the container rather than hardcoded, and this is the check
    /// that it really tracks: at a 420pt pane a long message must still be
    /// taller than a short one and must still lay out.
    ///
    /// Asked of the **expanded** bubble, because that is now the only place a
    /// huge string is laid out at all — collapsed, there is nothing long enough
    /// to fall off the side of anything.
    func testALongMessageIsNotClippedOffTheSideOfANarrowWindow() {
        let short = bubbleHeight(characters: 12, pane: 420)
        let long = bubbleHeight(characters: 8_000, pane: 420, expanded: true)

        XCTAssertGreaterThan(short, 0, "nothing drew at 420pt")
        XCTAssertGreaterThan(
            long, short * 20,
            String(format:
                "at a 420pt pane an 8,000-character message is only %.0fpt tall "
                + "against %.0fpt for twelve characters. It is being laid out at a "
                + "width the window does not have, so most of what he pasted is off "
                + "the right-hand side of a view that only scrolls vertically.",
                long, short))
    }

    /// And the whole conversation still reports a real size with that row in
    /// it, so nothing above is measuring a pane that failed to draw.
    func testThePaneStillLaysOutWithTheLongMessageInIt() async {
        let deck = await store(oneMessageOf: 20_000)
        let probe = Probe(rootView: ThreadView(store: deck))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        probe.layoutSubtreeIfNeeded()

        // Header and composer. Not 100 any more: the "Idle — its session is
        // up" strip no longer draws on a quiet desk (D2), and the Return hint
        // under the box is gone.
        XCTAssertGreaterThan(
            probe.fittingSize.height, 60,
            "the pane holding his long message has no height, so every "
            + "measurement in this file is the cost of drawing nothing")
        guard case .loaded(let screen) = deck.thread else {
            return XCTFail("no conversation is open")
        }
        XCTAssertEqual(screen.rows.first?.message.text.count, 20_000,
                       "the row under test is not the long one")
    }

    // MARK: harness

    private final class Probe<Content: View>: NSHostingView<Content> {
        var passes = 0
        override func layout() { passes += 1; super.layout() }
    }

    private func hostedWindow<V: View>(_ probe: NSHostingView<V>) -> NSWindow {
        // Parked far off every display, never ordered front, activation
        // prohibited. This process cannot take his screen even by accident.
        NSApplication.shared.setActivationPolicy(.prohibited)
        probe.frame = NSRect(x: 0, y: 0, width: 900, height: 700)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 900, height: 700),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        return window
    }

    private func teardown(_ window: NSWindow) {
        window.orderOut(nil)
        window.contentView = nil
    }

    /// Milliseconds for one size query. `width: nil` is the ideal query —
    /// `StackLayout.prioritize` -> `ScrollViewUtilities.sizeThatFits`, the one
    /// at the top of his sample. A width is the question every real layout
    /// pass asks. Both have to be cheap; optimising one at the other's expense
    /// is the mistake this file exists to have caught.
    private func milliseconds<V: View>(_ view: V, width: CGFloat?) -> Double {
        let probe = Probe(rootView: AnyView(
            width.map { AnyView(view.frame(width: $0)) } ?? AnyView(view)))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        let began = CFAbsoluteTimeGetCurrent()
        _ = probe.fittingSize
        return (CFAbsoluteTimeGetCurrent() - began) * 1000
    }

    private func idealMilliseconds<V: View>(_ view: V) -> Double {
        milliseconds(view, width: nil)
    }

    /// The **real bubble's** wrapped height in a real pane. This is the number
    /// that says whether any of his text went missing.
    private func bubbleHeight(characters: Int) -> Double {
        let probe = Probe(rootView: MessageBubble(message: message(Self.mandate(characters: characters)))
            .frame(width: 900))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        return probe.fittingSize.height
    }

    /// The bubble's height at a stated pane width. Height is the observable
    /// that reports width here: a message pinned to 560pt wraps into more lines
    /// than the same message allowed to use the whole pane, so a short reply
    /// that has been wrongly pinned shows up as extra height.
    /// A mandate written the way a desk actually writes one.
    static func markdownMandate(characters: Int) -> String {
        let unit = """
        **Portfolio** — the roster end to end, so the conversation says what is \
        happening without me going to look for it.

        - `shop.initech.example` is live and answering
        - see [the board](https://example.com/board) for the rest

        ```bash
        ssh deploy@initech.example
        ```

        """
        var out = ""
        while out.count < characters { out += unit }
        return String(out.prefix(characters))
    }

    private func bubbleHeight(of text: String, pane: CGFloat, expanded: Bool = false) -> Double {
        let probe = Probe(rootView: MessageBubble(
            message: message(text), startExpanded: expanded).frame(width: pane))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        return probe.fittingSize.height
    }

    private func bubbleHeight(characters: Int, pane: CGFloat, expanded: Bool = false) -> Double {
        let probe = Probe(rootView: MessageBubble(
            message: message(Self.mandate(characters: characters)),
            startExpanded: expanded).frame(width: pane))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        return probe.fittingSize.height
    }

    /// The same question of a tool-call card, whose reason is the other place
    /// unbounded text reaches this transcript.
    private func cardHeight(whyCharacters: Int, pane: CGFloat) -> Double {
        let card = ApprovalCard(
            approvalID: "ask1", title: "gh pr create", tool: "Bash", at: nil,
            runsOn: "Runs on Chief's computer",
            why: Self.mandate(characters: whyCharacters),
            disclosureTitle: "Show the details", details: "", options: [],
            status: .waitingOnYou
        )
        let probe = Probe(rootView: ApprovalCardView(card: card) { _ in }.frame(width: pane))
        let window = hostedWindow(probe)
        defer { teardown(window) }
        return probe.fittingSize.height
    }

    /// Median of `count` samples. One sample off a shared Mac is a coin toss;
    /// the median is what makes a threshold mean something.
    private func median(_ count: Int, _ sample: () -> Double) -> Double {
        var values: [Double] = []
        for _ in 0..<count { values.append(sample()) }
        return values.sorted()[count / 2]
    }

    private func store(oneMessageOf characters: Int) async -> DeckStore {
        let client = ScriptedDeckClient()
        var chief = makeAgent("chief")
        chief.state = .idle
        client.rosterPayload = RosterPayload(
            agents: [chief],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(
            threadID: "direct:chief",
            messages: [message(Self.mandate(characters: characters))],
            isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(client: client, approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        return store
    }
}

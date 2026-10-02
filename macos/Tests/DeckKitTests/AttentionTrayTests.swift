import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **An ask on a desk he is not looking at is invisible today, and that is the
/// whole defect.**
///
/// The deck raises a tool call on `acme-growth`; he is reading `atlas`; the
/// card is drawn inline in `acme-growth`'s conversation and nowhere else. So
/// the session sits blocked until he happens to click that desk. Meanwhile the
/// same ask reaches him on WhatsApp — *"agents asking me on whatsup to confirm
/// but not on screen whats the point"*.
///
/// The tray is the answer: one pinned strip, above everything else in the
/// inspector, carrying **every** live ask on the deck with the desk that
/// raised it, what it wants, where it would run, and the deck's own options.
///
/// Three separate things are asserted here, and they are not the same rule:
///
/// 1. an ask **attributed to a desk on the roster** is drawn inline in that
///    desk's conversation — the route that already exists, kept honest;
/// 2. an ask the deck attributes to something **no desk answers to** — a raw
///    session id in `agent`, which is what the deck publishes today — still
///    reaches him, in the tray, labelled with what the deck did know;
/// 3. **every** pending ask on the page reaches him by one of those two routes.
///    Not "this one draws": the set.
///
/// Dropping (2) silently is the shipped defect. A question nobody can see is
/// still a session sitting blocked.
@MainActor
final class AttentionTrayTests: XCTestCase {

    // MARK: fixtures

    /// §12's payload, one entry per `(id, agent)` pair. `agent` is whatever the
    /// caller says — including a raw session id, which is what the deck puts
    /// there today.
    private func page(_ entries: [(id: String, agent: String)]) throws -> ApprovalsPage {
        let json = entries.enumerated().map { index, entry in
            """
            {"id":"\(entry.id)","ts":175600000\(index).0,"agent":"\(entry.agent)","tool":"Bash",
             "subject":"gh pr list","cwd":"/Users/sam/Projects/acme",
             "cwd_short":"~/Projects/acme","status":"pending","options":[
               {"reply":"once","available":true,"rule":null,"summary":"just this time"},
               {"reply":"always","available":true,
                "summary":"allow `gh pr*` in ~/Projects/acme, never ask again",
                "rule":{"id":"r","kind":"always_allow","tool":"Bash","pattern":"gh pr*",
                        "cwd":"/Users/sam/Projects/acme","note":"from chief"}},
               {"reply":"never","available":true,
                "summary":"refuse `gh pr*` in ~/Projects/acme from now on",
                "rule":{"id":"r","kind":"deny","tool":"Bash","pattern":"gh pr*",
                        "cwd":"/Users/sam/Projects/acme","note":"from chief"}}]}
            """
        }
        return try DeckCoding.decoder.decode(
            ApprovalsPage.self,
            from: Data("{\"approvals\":[\(json.joined(separator: ","))]}".utf8))
    }

    /// Two desks, `atlas` opened first — so anything raised on `acme-growth` is
    /// on a desk he is *not* looking at, which is the case under test.
    private func store(_ client: ScriptedDeckClient) -> DeckStore {
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("atlas"), makeAgent("acme-growth")],
            threads: [makeThread("direct:atlas", agent: "atlas", at: 2),
                      makeThread("direct:acme-growth", agent: "acme-growth", at: 1)],
            sectionOrder: ["Work"])
        client.pages = [
            MessagePage(threadID: "direct:atlas", messages: [], isReadOnly: false,
                        participants: ["owner", "atlas"]),
            MessagePage(threadID: "direct:acme-growth", messages: [], isReadOnly: false,
                        participants: ["owner", "acme-growth"]),
        ]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        return DeckStore(client: client, approvalPollInterval: 600)
    }

    private func loaded(_ client: ScriptedDeckClient) async -> DeckStore {
        let deck = store(client)
        await deck.loadRoster()
        await deck.loadApprovals()
        return deck
    }

    // MARK: 1 — the headline

    /// **The tray covers the deck, not the open thread.**
    func testAnAskOnADeskHeIsNotLookingAtIsStillOnScreen() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([(id: "apr_acme", agent: "acme-growth")])
        let deck = await loaded(client)

        XCTAssertEqual(deck.selectedAgentName, "atlas", "the case needs the other desk open")
        XCTAssertTrue(deck.approvals.isEmpty, "nothing is drawn in the conversation he is reading")

        XCTAssertEqual(
            deck.attention.map(\.askID), ["apr_acme"],
            "acme-growth is stopped on a question and he is reading atlas, so "
            + "the ask reaches him nowhere on this screen. That is the defect: "
            + "a blocked session he cannot see.")

        let item = try XCTUnwrap(deck.attention.first)
        XCTAssertEqual(item.deskLine, "Acme-growth", "the card has to say which desk")
        XCTAssertTrue(item.request.contains("gh pr list"), item.request)
        XCTAssertTrue(item.request.contains("Bash"), item.request)
        XCTAssertTrue(item.whereLine.contains("~/Projects/acme"), item.whereLine)
        XCTAssertEqual(
            item.actions.map(\.summary),
            ["just this time",
             "allow `gh pr*` in ~/Projects/acme, never ask again",
             "refuse `gh pr*` in ~/Projects/acme from now on"],
            "the consequence ships with the option and has to survive to the tray")
    }

    // MARK: 2 — the ask nothing can attribute

    /// **The one that is invisible today.** The deck publishes a raw session id
    /// in `agent`; `pending(forAgent:)` filters on the desk name; so this ask
    /// matches no conversation and is drawn nowhere at all. It must still reach
    /// him, labelled with what the deck *did* send.
    func testAnAskNoDeskOnTheRosterAnswersToStillReachesHim() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page(
            [(id: "apr_orphan", agent: "0f9c2b7e-4a11-4c3d-9d5e-77ec17a1b2c3")])
        let deck = await loaded(client)

        XCTAssertTrue(deck.approvals.isEmpty,
                      "no conversation on this deck belongs to that id, correctly")
        XCTAssertEqual(
            deck.attention.map(\.askID), ["apr_orphan"],
            "an ask nothing can attribute is exactly the ask that is invisible "
            + "today. Dropping it leaves a session blocked with nothing on screen.")

        let item = try XCTUnwrap(deck.attention.first)
        XCTAssertFalse(item.isAttributed, "this one must not claim to be a desk on the roster")
        XCTAssertTrue(item.request.contains("Bash"), item.request)
        XCTAssertTrue(item.request.contains("gh pr list"), item.request)
        XCTAssertTrue(item.whereLine.contains("~/Projects/acme"), item.whereLine)
        XCTAssertTrue(
            item.evidence.contains("0f9c2b7e"),
            "the raw thing the deck sent is the only handle he has on this ask, "
            + "so it is shown rather than swallowed: \(item.evidence)")
        XCTAssertNotEqual(
            item.deskLine, "0f9c2b7e-4a11-4c3d-9d5e-77ec17a1b2c3",
            "a session id dressed up as a desk name is a worse lie than saying "
            + "the deck did not name one")
    }

    // MARK: 3 — the inline card, once the deck names the desk

    /// The backend fix lands and `agent` carries the desk name. Then the card
    /// belongs **in that desk's conversation**, and this says it is drawn there
    /// — not merely that the store holds it.
    func testWhenTheDeckNamesTheDeskTheCardIsDrawnInThatDesksConversation() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([(id: "apr_atlas", agent: "atlas")])
        let deck = await loaded(client)

        XCTAssertEqual(deck.approvals.map(\.approvalID), ["apr_atlas"])
        guard case .loaded(let screen) = deck.thread else {
            return XCTFail("the conversation never loaded")
        }
        let drawn = screen.entries.compactMap { entry -> String? in
            if case .toolCall(let card) = entry { return card.approvalID }
            return nil
        }
        XCTAssertEqual(
            drawn, ["apr_atlas"],
            "the ask is attributed to atlas and atlas's conversation is open, so "
            + "the card belongs in the transcript. \(screen.entries.count) entries drawn.")
        XCTAssertEqual(
            deck.attention.map(\.askID), ["apr_atlas"],
            "and it is still a live ask, so it is in the tray as well — he must "
            + "not have to be looking at the right thread to know it exists")
    }

    // MARK: 4 — the sweep: not one ask, every ask

    /// **The class, not the case.** Four asks, three routes, one rule: every
    /// pending ask on the page reaches him somewhere.
    func testEveryPendingAskOnTheDeckReachesHimBySomeRoute() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([
            (id: "apr_open", agent: "atlas"),
            (id: "apr_elsewhere", agent: "acme-growth"),
            (id: "apr_orphan", agent: "9f3c-a-session-not-a-desk"),
        ])
        let deck = await loaded(client)

        let reachable = Set(deck.attention.map(\.askID))
            .union(deck.approvals.map(\.approvalID))
        XCTAssertEqual(
            reachable, ["apr_open", "apr_elsewhere", "apr_orphan"],
            "\(reachable.count) of 3 pending asks are on screen. Every one of "
            + "the missing ones is a session sitting blocked with no sign of it.")
        XCTAssertEqual(
            deck.attention.map(\.askID),
            ["apr_open", "apr_elsewhere", "apr_orphan"],
            "the tray is deck-wide and in the order they were asked")
    }

    /// An ask that cannot even be decoded has no options, so it cannot be
    /// answered here — and it is still counted out loud rather than dropped.
    func testAnAskTooBrokenToDrawIsStillCountedOutLoud() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try DeckCoding.decoder.decode(
            ApprovalsPage.self,
            from: Data(#"""
            {"approvals":[{"id":"a","ts":1.0,"agent":"atlas","tool":"Bash","subject":"rm -rf x",
              "cwd":"/tmp","cwd_short":"/tmp","status":"pending",
              "options":[{"reply":"once","available":true,"rule":null}]}]}
            """#.utf8))
        let deck = await loaded(client)

        XCTAssertTrue(deck.attention.isEmpty, "there is nothing here he could answer")
        XCTAssertEqual(
            deck.approvalNotice,
            "1 approval request could not be read and is not shown here. "
            + "Answer it in the agent's own terminal.",
            "an unanswerable ask is still a blocked session and has to be said")
    }

    // MARK: 5 — answering from the tray

    /// The tray answers down the **existing** route — `DeckStore.answer` — and
    /// the question leaves the tray because the deck accepted it, not because
    /// the button was pressed.
    func testAnsweringFromTheTrayUsesTheSameRouteAndClearsThatAskOnly() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([
            (id: "apr_acme", agent: "acme-growth"),
            (id: "apr_other", agent: "acme-growth"),
        ])
        let deck = await loaded(client)

        let item = try XCTUnwrap(deck.attention.first)
        let always = try XCTUnwrap(item.actions.first { $0.verb == .permission(.always) })
        await deck.answer(item: item, with: always)

        XCTAssertEqual(client.decidedApprovals.map(\.0), ["apr_acme"],
                       "he answered a desk he was not looking at, and it landed")
        XCTAssertEqual(client.decidedApprovals.map(\.1), ["always"],
                       "the reply sent is the one whose sentence he read")
        XCTAssertEqual(deck.attention.map(\.askID), ["apr_other"],
                       "the answered ask goes; the other one is still waiting")
    }

    /// A refusal from the deck must leave the item up. A tray that emptied on a
    /// tap the deck rejected would read exactly like one that worked.
    func testAnAnswerTheDeckRefusesLeavesTheAskInTheTray() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([(id: "apr_acme", agent: "acme-growth")])
        client.decideError = .queueFailed
        let deck = await loaded(client)

        let item = try XCTUnwrap(deck.attention.first)
        await deck.answer(item: item, with: item.actions[0])

        XCTAssertEqual(deck.attention.map(\.askID), ["apr_acme"],
                       "nothing was granted, so nothing may disappear")
        XCTAssertEqual(deck.approvalProblem, DeckError.queueFailed.userFacingText)
    }

    // MARK: 6 — what it says out loud

    /// Every action names the desk and the consequence *before* it is pressed.
    /// "Always allow" on its own would grant a standing permission on his Mac
    /// without saying so, and a screen reader would announce two identical
    /// buttons on two different desks.
    func testEveryActionSaysWhichDeskItAnswersAndWhatItGrants() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try page([(id: "apr_acme", agent: "acme-growth")])
        let deck = await loaded(client)
        let item = try XCTUnwrap(deck.attention.first)

        let spoken = item.actions.map { item.spokenAction($0) }
        XCTAssertTrue(spoken.allSatisfy { $0.contains("Acme-growth") },
                      "an action that does not name its desk answers the wrong "
                      + "question when two desks are waiting: \(spoken)")
        XCTAssertTrue(
            spoken.contains { $0.contains("From now on") && $0.contains("never ask again") },
            "the standing grant has to say it is standing, and say what it "
            + "writes, out loud: \(spoken)")
        XCTAssertFalse(spoken.contains { $0.contains("`") },
                       "backticks are silence in VoiceOver: \(spoken)")
        XCTAssertTrue(item.spoken.contains("Acme-growth") && item.spoken.contains("gh pr list"),
                      "the item reads as one sentence: \(item.spoken)")
    }

    // MARK: 7 — nothing waiting draws nothing at all

    /// An empty tray is visual noise a manager learns to ignore, and something
    /// he learns to ignore is something he will ignore on the day it matters.
    /// So: no header, no "No items", no padding — zero height.
    func testAnEmptyTrayDrawsNothingAtAllAndAFullOneDraws() throws {
        let empty = height(of: AttentionTrayView(items: [], answer: { _, _ in }, openDesk: { _ in }))
        XCTAssertEqual(
            empty, 0,
            "an empty tray took \(empty)pt at the top of the inspector. It is "
            + "on screen all day with nothing in it, which is how he learns to "
            + "stop looking at the one place an ask appears.")

        let item = try XCTUnwrap(sampleItem())
        let full = height(of: AttentionTrayView(items: [item], answer: { _, _ in }, openDesk: { _ in }))
        XCTAssertGreaterThan(
            full, 60,
            "the tray with a live ask in it laid out to \(full)pt, so the "
            + "zero above says nothing")
    }

    /// The tray is a value, not an observer — the same rule the inspector
    /// around it is held to. Two trays holding the same items must compare
    /// equal, or `.equatable()` never skips and the panel is rebuilt on every
    /// character typed into the composer.
    func testTwoTraysWithTheSameAsksCompareEqualAndADifferentAskDoesNot() throws {
        let item = try XCTUnwrap(sampleItem())
        XCTAssertEqual(AttentionTrayView(items: [item], answer: { _, _ in }, openDesk: { _ in }),
                       AttentionTrayView(items: [item], answer: { _, _ in }, openDesk: { _ in }),
                       "the closures differ on every draw and must be out of `==`")
        XCTAssertNotEqual(AttentionTrayView(items: [], answer: { _, _ in }, openDesk: { _ in }),
                          AttentionTrayView(items: [item], answer: { _, _ in }, openDesk: { _ in }),
                          "an ask that arrived would never be drawn")
    }

    // MARK: probes

    private func sampleItem() throws -> AttentionItem? {
        let page = try page([(id: "apr_acme", agent: "acme-growth")])
        return page.approvals.first.map {
            AttentionItem.make(approval: $0, agents: ["acme-growth": makeAgent("acme-growth")])
        }
    }

    /// The height the view really lays out to, in a real (never shown) window.
    private func height<V: View>(of view: V) -> CGFloat {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: view.frame(width: 300))
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 300, height: 600),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let size = probe.fittingSize
        window.orderOut(nil)
        window.contentView = nil
        return size.height
    }
}

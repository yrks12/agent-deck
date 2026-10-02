import XCTest
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"Always, up to a limit…" on the Mac's approval card**: offered beside
/// "Always" when the deck says it can be, it collects a limit and posts it to
/// the card's own route; the card then says what was set.
@MainActor
final class TheMacCardOffersAStandingLimitTests: XCTestCase {
    private func source(_ path: String) throws -> String {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheLimitSheetChecksWhatTheDeckWouldRefuse() {
        XCTAssertNotNil(StandingPresentation.cardProblem(StandingFromCard(limits: StandingLimits()), kind: .runCommand))
        XCTAssertNotNil(StandingPresentation.cardProblem(StandingFromCard(limits: StandingLimits(countPerDay: 3)),
                                                         kind: .spendMoney))
        XCTAssertNotNil(StandingPresentation.cardProblem(StandingFromCard(limits: StandingLimits(countPerDay: 0)),
                                                         kind: .sendEmail))
        XCTAssertNil(StandingPresentation.cardProblem(StandingFromCard(limits: StandingLimits(countPerDay: 20)),
                                                      kind: .runCommand), "the card names the commands itself")
    }

    func testTheStoreSendsTheLimitAndTheCardSaysWhatWasSet() async throws {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await store.loadApprovals()
        let card = try XCTUnwrap(store.approvals.first { $0.approvalID == "apr_pr" })
        XCTAssertTrue(card.offersStanding, "the fixture card offers a standing limit")
        let problem = await store.stand(card: card, with: StandingFromCard(limits: StandingLimits(countPerDay: 20)))
        XCTAssertNil(problem)
        let settled = try XCTUnwrap(store.approvals.first { $0.approvalID == "apr_pr" })
        XCTAssertEqual(settled.statusPill, "Always allowed")
        XCTAssertFalse(settled.offersStanding)
        XCTAssertTrue(settled.outcome?.contains("Up to 20 commands a day") == true, settled.outcome ?? "nil")
    }

    func testTheCardDrawsTheOptionBesideAlwaysAndTheThreadWiresIt() throws {
        let card = try source("macos/Sources/DeckUI/ApprovalCardView.swift")
        XCTAssertTrue(card.contains("card.offersStanding"))
        XCTAssertTrue(card.contains("StandingPresentation.cardOption"))
        XCTAssertTrue(card.contains("StandingLimitSheet("))
        XCTAssertTrue(try source("macos/Sources/DeckUI/ThreadView.swift").contains("store.stand(card:"))
        XCTAssertTrue(try source("macos/Sources/DeckUI/DeckStore.swift").contains("standing: $0.standing"),
                      "a standing answer must reach the card that is drawn")
    }

    func testTheSheetIsSharedAndLaysOut() throws {
        let sheet = try source("macos/Sources/DeckUI/StandingLimitSheet.swift")
        XCTAssertFalse(sheet.contains("NSColor") || sheet.contains("import AppKit"), "the iPhone compiles it too")
        let option = StandingOption(isAvailable: true, kind: .runCommand, desk: "atlas",
                                    route: "/v1/approvals/a/standing", summary: "Let atlas do this up to a daily limit")
        let probe = NSHostingView(rootView: StandingLimitSheet(option: option, submit: { _ in nil }, onDone: {})
            .frame(width: 440, height: 480))
        probe.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(probe.fittingSize.height, 100)
    }
}

import XCTest
@testable import DeckKit
@testable import DeckUI

private final class StubMoney: MoneySource, @unchecked Sendable {
    var result: Result<MoneyReport?, Error> = .success(nil)
    private(set) var refreshes: [Bool] = []
    func money(refresh: Bool) async throws -> MoneyReport? {
        refreshes.append(refresh)
        return try result.get()
    }
}

/// **The Money screen on the Mac.** `GET /v1/money` answers; the Mac had no
/// screen for it. The sidebar's bottom rows open it as a sheet, its model asks
/// the deck, and Refresh asks for `?refresh=1`.
@MainActor
final class TheMacDrawsTheMoneyScreenTests: XCTestCase {

    func testTheStoreOffersMoneyWhenTheClientCanAnswer() {
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        XCTAssertNotNil(store.moneyClient)
        store.showMoney()
        XCTAssertTrue(store.isShowingMoney)
        store.dismissMoney()
        XCTAssertFalse(store.isShowingMoney)
    }

    func testADeckThatCannotAnswerHasNoMoneyRow() {
        let store = DeckStore(client: ScriptedDeckClient(), approvalPollInterval: 600)
        XCTAssertNil(store.moneyClient)
        store.showMoney()
        XCTAssertFalse(store.isShowingMoney)
    }

    func testTheModelShowsTheReportTheDeckSent() async {
        let stub = StubMoney()
        stub.result = .success(MoneyReport(state: .ready))
        let model = MoneyModel(source: stub)
        await model.load()
        XCTAssertEqual(model.phase, .loaded(MoneyReport(state: .ready)))
    }

    func testARouteTheDeckDoesNotServeIsAStateNotAnError() async {
        let model = MoneyModel(source: StubMoney())
        await model.load()
        XCTAssertEqual(model.phase, .unavailable)
    }

    func testRefreshAsksTheDeckToReReadAndAFailureKeepsWhatIsOnScreen() async {
        let stub = StubMoney()
        stub.result = .success(MoneyReport(state: .ready))
        let model = MoneyModel(source: stub)
        await model.load()
        stub.result = .failure(DeckError.decoding("boom"))
        await model.load(refresh: true)
        XCTAssertEqual(stub.refreshes, [false, true])
        XCTAssertEqual(model.phase, .loaded(MoneyReport(state: .ready)), "a failed refresh must not blank the numbers")
    }

    func testAFirstLoadThatFailsSaysSo() async {
        let stub = StubMoney()
        stub.result = .failure(DeckError.decoding("boom"))
        let model = MoneyModel(source: stub)
        await model.load()
        guard case .failed = model.phase else { return XCTFail("expected a failure, got \(model.phase)") }
    }

    func testTheSidebarOpensMoneyAndTheRootMountsIt() throws {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        func read(_ p: String) throws -> String { try String(contentsOf: repo.appendingPathComponent(p), encoding: .utf8) }
        XCTAssertTrue(try read("macos/Sources/DeckUI/SidebarView.swift").contains("onOpenMoney"))
        XCTAssertTrue(try read("macos/Sources/DeckUI/DeckRootView.swift").contains("MoneyView("))
    }
}

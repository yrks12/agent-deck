import XCTest
@testable import DeckKit

/// **The footer read "Owner / Signed in on this Mac" on his Mac** while the
/// box had `DECK_OWNER_NAME` set. The handle (`X-Deck-Owner`) is a wire id;
/// his name now rides beside it in `X-Deck-Owner-Name`, percent-encoded, and
/// the footer says it. "Owner" is only the fallback for a deck with no name.
final class TheFooterSaysHisNameTests: XCTestCase {

    override func tearDown() {
        DeckOwner.learnDisplayName(nil, reset: true)
        super.tearDown()
    }

    func testTheDecksNameIsLearnedFromItsHeaderAndDecoded() {
        DeckOwner.learnDisplayName("Zo%C3%AB%20Smith")
        XCTAssertEqual(DeckOwner.displayName, "Zoë Smith")
        DeckOwner.learnDisplayName(nil)
        XCTAssertEqual(DeckOwner.displayName, "Zoë Smith", "a response without the header does not forget him")
        DeckOwner.learnDisplayName("   ")
        XCTAssertEqual(DeckOwner.displayName, "Zoë Smith", "a blank name never replaces his")
    }

    func testTheFooterSaysHisNameAndFallsBackToOwner() {
        XCTAssertEqual(SidebarFooter.forCurrentOwner().accountTitle, "Owner")
        DeckOwner.learnDisplayName("Sam")
        let footer = SidebarFooter.forCurrentOwner()
        XCTAssertEqual(footer.accountTitle, "Sam")
        XCTAssertEqual(footer.accountLook.initials, "S")
        XCTAssertEqual(SidebarFooter.forCurrentOwner(), footer, "the same name gives the same footer")
    }

    func testTheHTTPClientLearnsHisNameFromAnyResponse() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(#"{"available":false,"windows":[]}"#.utf8)
        performer.headers = ["X-Deck-Owner": "owner", "X-Deck-Owner-Name": "Sam"]
        let tokens = InMemoryTokenStore()
        try tokens.setToken("t")
        _ = try await HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens,
                                     performer: performer).usage()
        XCTAssertEqual(DeckOwner.displayName, "Sam")
    }
}

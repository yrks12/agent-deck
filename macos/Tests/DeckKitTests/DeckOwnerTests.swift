import XCTest
@testable import DeckKit

/// The owner's wire id belongs to the deck, not to this app: "owner" until a
/// deck names another in `X-Deck-Owner`, and nothing odd ever renames him.
final class DeckOwnerTests: XCTestCase {

    override func tearDown() {
        DeckOwner.learn(DeckOwner.neutral)
        super.tearDown()
    }

    func testTheNeutralIdUntilTheDeckSaysOtherwise() {
        XCTAssertEqual(DeckOwner.name, "owner")
    }

    func testAnOlderDecksIdIsAdopted() {
        DeckOwner.learn("sam")
        XCTAssertEqual(DeckOwner.name, "sam")
    }

    func testAMissingOrOddHeaderChangesNothing() {
        DeckOwner.learn(nil)
        DeckOwner.learn("")
        DeckOwner.learn("Sam Carter")
        DeckOwner.learn("../etc")
        DeckOwner.learn(String(repeating: "a", count: 33))
        XCTAssertEqual(DeckOwner.name, "owner")
    }
}

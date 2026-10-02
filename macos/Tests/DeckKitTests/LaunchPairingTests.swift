import XCTest
@testable import DeckKit

/// The launch contract an installer depends on:
/// `Agent Deck.app --args --pair <CODE>` (or `--pair=<CODE>`) opens the Connect
/// screen with the code filled in. Never submitted by the parser.
final class LaunchPairingTests: XCTestCase {
    func testSpaceSeparatedForm() {
        XCTAssertEqual(LaunchPairing.code(from: ["/x/Agent Deck", "--pair", "ADK1.abc"]), "ADK1.abc")
    }

    func testEqualsForm() {
        XCTAssertEqual(LaunchPairing.code(from: ["app", "--pair=ADK1.abc.def"]), "ADK1.abc.def")
    }

    func testAbsentOrEmptyIsNil() {
        XCTAssertNil(LaunchPairing.code(from: ["app"]))
        XCTAssertNil(LaunchPairing.code(from: ["app", "--pair"]))
        XCTAssertNil(LaunchPairing.code(from: ["app", "--pair="]))
        XCTAssertNil(LaunchPairing.code(from: ["app", "--pair", "--other"]))
        XCTAssertNil(LaunchPairing.code(from: ["app", "--pair", "   "]))
    }

    func testSurroundingWhitespaceIsTrimmedAndFirstOccurrenceWins() {
        XCTAssertEqual(LaunchPairing.code(from: ["app", "--pair", " ADK1.one\n", "--pair", "ADK1.two"]), "ADK1.one")
    }

    func testTheProgramNameIsNeverTreatedAsTheFlag() {
        XCTAssertNil(LaunchPairing.code(from: ["--pair=ADK1.x"]))
        XCTAssertNil(LaunchPairing.code(from: []))
    }
}

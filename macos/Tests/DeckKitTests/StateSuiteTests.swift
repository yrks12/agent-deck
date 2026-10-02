import XCTest
@testable import DeckKit

/// K9: DECK_STATE_SUITE keeps every automated pairing test away from the
/// owner's real connection.
final class StateSuiteTests: XCTestCase {

    func testNoVariableMeansTheRealSettings() {
        let s = StateSuite(environment: [:])
        XCTAssertNil(s.defaultsSuiteName)
        XCTAssertEqual(s.keychainAccount, "deck-api-token")
        XCTAssertTrue(s.defaults === UserDefaults.standard)
    }

    func testAnEmptyVariableIsNotASuite() {
        XCTAssertNil(StateSuite(environment: ["DECK_STATE_SUITE": ""]).defaultsSuiteName)
    }

    func testASuiteMovesDefaultsAndKeychainAccount() {
        let s = StateSuite(environment: ["DECK_STATE_SUITE": "livepair"])
        XCTAssertEqual(s.defaultsSuiteName, "\(DeckIdentity.neutral).livepair")
        XCTAssertEqual(s.keychainAccount, "deck-api-token.livepair")
        XCTAssertFalse(s.defaults === UserDefaults.standard)
    }

    func testWritesToASuiteNeverReachTheStandardDefaults() {
        let name = "t\(UUID().uuidString.prefix(8))"
        let s = StateSuite(environment: ["DECK_STATE_SUITE": name])
        defer { s.defaults.removePersistentDomain(forName: s.defaultsSuiteName!) }
        s.defaults.set("https://x.example", forKey: "deckBaseURL")
        XCTAssertEqual(s.defaults.string(forKey: "deckBaseURL"), "https://x.example")
        XCTAssertNotEqual(UserDefaults.standard.string(forKey: "deckBaseURL"), "https://x.example")
    }

    /// A typo in the variable must not quietly fall back to the owner's real
    /// settings: odd characters are replaced, never dropped to "no suite".
    func testAnUnusualNameStillIsolates() {
        let s = StateSuite(environment: ["DECK_STATE_SUITE": "a b/../c"])
        XCTAssertNotNil(s.defaultsSuiteName)
        XCTAssertNotEqual(s.keychainAccount, "deck-api-token")
        XCTAssertFalse(s.keychainAccount.contains("/"))
        XCTAssertFalse(s.keychainAccount.contains(" "))
    }
}

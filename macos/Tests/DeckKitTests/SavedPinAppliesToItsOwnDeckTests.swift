import XCTest
@testable import DeckKit

/// The Mac's live client must carry the saved TLS pin -- but only to the deck
/// the pin was made for. An address overridden by the environment is a
/// different server and must not be held to (or trusted by) that pin.
final class SavedPinAppliesToItsOwnDeckTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!

    override func setUp() {
        suiteName = "dev.agentdeck.app.test\(UUID().uuidString.prefix(8))"
        defaults = UserDefaults(suiteName: suiteName)
    }
    override func tearDown() { defaults.removePersistentDomain(forName: suiteName) }

    private var deck: DeckConnection { DeckConnection(defaults: defaults, tokens: InMemoryTokenStore()) }

    func testNoSavedConnectionMeansNoPin() {
        XCTAssertNil(deck.savedPin(for: URL(string: "https://deck.example")!))
    }

    func testThePinBelongsToTheSavedAddressOnly() {
        defaults.set("https://deck.example", forKey: DeckConnection.urlKey)
        defaults.set("sha256/AAAA", forKey: DeckConnection.pinKey)
        XCTAssertEqual(deck.savedPin(for: URL(string: "https://deck.example")!), "sha256/AAAA")
        XCTAssertNil(deck.savedPin(for: URL(string: "https://other.example")!))
    }

    func testCAModeSavesNoPin() {
        defaults.set("https://deck.example", forKey: DeckConnection.urlKey)
        defaults.set("", forKey: DeckConnection.pinKey)
        XCTAssertNil(deck.savedPin(for: URL(string: "https://deck.example")!))
    }
}

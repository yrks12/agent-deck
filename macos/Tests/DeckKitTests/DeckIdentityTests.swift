import XCTest
@testable import DeckKit

/// The bundle id every Keychain item, defaults domain and notification id is
/// scoped to comes from the build, never from a literal in the source.
final class DeckIdentityTests: XCTestCase {

    private func bundle(named name: String, info: [String: Any]) throws -> Bundle {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent(UUID().uuidString).appendingPathComponent(name)
        let contents = root.appendingPathComponent("Contents")
        try FileManager.default.createDirectory(at: contents, withIntermediateDirectories: true)
        let data = try PropertyListSerialization.data(fromPropertyList: info, format: .xml, options: 0)
        try data.write(to: contents.appendingPathComponent("Info.plist"))
        return try XCTUnwrap(Bundle(url: root))
    }

    func testTheTestRunnerIsTheNeutralId() {
        XCTAssertEqual(DeckIdentity.bundleID, DeckIdentity.neutral)
        XCTAssertEqual(StateSuite.keychainService, DeckIdentity.neutral)
    }

    func testAnAppUsesTheIdItWasBuiltWith() throws {
        let app = try bundle(named: "Agent Deck.app", info: ["CFBundleIdentifier": "com.example.deck"])
        XCTAssertEqual(DeckIdentity.resolve(app), "com.example.deck")
    }

    func testThePhoneNamesTheSharedIdNotItsOwn() throws {
        let app = try bundle(named: "Agent Deck.app", info: [
            "CFBundleIdentifier": "com.example.deck.ios", "DeckBundleID": "com.example.deck"])
        XCTAssertEqual(DeckIdentity.resolve(app), "com.example.deck")
    }

    func testAnUnexpandedBuildSettingIsNotAnId() throws {
        let app = try bundle(named: "Agent Deck.app", info: [
            "CFBundleIdentifier": "com.example.deck", "DeckBundleID": "${DECK_BUNDLE_ID}"])
        XCTAssertEqual(DeckIdentity.resolve(app), "com.example.deck")
    }

    func testNotAnAppIsTheNeutralId() throws {
        let other = try bundle(named: "Tool.xctest", info: ["CFBundleIdentifier": "com.example.tool"])
        XCTAssertEqual(DeckIdentity.resolve(other), DeckIdentity.neutral)
    }
}

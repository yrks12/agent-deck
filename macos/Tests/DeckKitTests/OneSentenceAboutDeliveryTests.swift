import XCTest
@testable import DeckKit

/// **There is exactly one place in this app that says what a notification does
/// or does not reach him on.**
///
/// `NotificationDelivery` exists to *be* that place. Its whole doc comment is
/// the argument for it: the line under the switch was true the day it was
/// written and became a lie the day per-desk WhatsApp pushes shipped, and then
/// the app was telling him — under a switch called *"Get notified when this Bot
/// finishes or needs input"* — that nothing would reach him, while something
/// did. That is not a stale string. It is the app being wrong about the one
/// question he asked, which is whether he finds out when an agent is stuck.
///
/// Building the single source did not remove the copies.
/// `AgentSettingsModel.notificationsCaveat` still asserted *"nothing delivers
/// notifications yet — there is no push service"*, unread by anything — dead,
/// and exactly the constant a future edit would reach for. `Agent`'s doc
/// comment on the flag said the same thing in its own words.
///
/// So this sweeps `Sources/` for the claim rather than for one constant: any
/// file that states what delivery does or does not happen, other than the one
/// type that owns the fact, is a second copy waiting to go stale on its own.
final class OneSentenceAboutDeliveryTests: XCTestCase {

    /// Phrases that make a claim about delivery, rather than merely mentioning
    /// notifications. A file is only flagged for asserting something.
    private let claims = [
        "nothing delivers",
        "no push service",
        "push service",
        "notifications do not notify",
        "nothing consumes it",
        "delivers on it",
    ]

    func testOnlyOneFileInTheAppSaysWhatANotificationReachesHimOn() throws {
        var claiming: [String: [String]] = [:]
        for file in try Self.sourceFiles() {
            let text = try String(contentsOf: file, encoding: .utf8)
            let found = claims.filter { text.localizedCaseInsensitiveContains($0) }
            if !found.isEmpty { claiming[file.lastPathComponent] = found }
        }

        XCTAssertNotNil(
            claiming["NotificationDelivery.swift"],
            "the sweep matched nothing in the type that owns this fact, so it "
            + "is sweeping for the wrong words and would pass over any copy")

        XCTAssertEqual(
            claiming.keys.sorted(), ["NotificationDelivery.swift"],
            "these files each carry their own claim about what a notification "
            + "reaches him on. Every one of them goes stale on its own, "
            + "silently, and the one under the switch already did:\n  "
            + claiming.sorted { $0.key < $1.key }
                .map { "\($0.key): \($0.value.joined(separator: ", "))" }
                .joined(separator: "\n  "))
    }

    /// And the sentence the app actually shows follows the fact. Pinned here as
    /// well as in `FixtureAndSettingsTests` because this file is the one that
    /// says there may be only one of it — a single source that said the wrong
    /// thing would satisfy the sweep above perfectly.
    func testTheOneSourceSaysWhereItGoesRatherThanThatNothingDoes() {
        let onScreen = NotificationDelivery.current

        XCTAssertTrue(onScreen.deliversAnything)
        XCTAssertTrue(
            onScreen.caveat.contains("phone"),
            "the one place that says this has to name where it goes: \(onScreen.caveat)")
        XCTAssertTrue(
            onScreen.caveat.contains("blocked"),
            "a desk stopped with no sign of it is the moment he complained "
            + "about; the line has to name it: \(onScreen.caveat)")
    }

    private static func sourceFiles() throws -> [URL] {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // DeckKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // package root
            .appendingPathComponent("Sources")
        guard let walk = FileManager.default.enumerator(at: root, includingPropertiesForKeys: nil)
        else { return [] }
        return walk.compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
    }
}

import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **Calls from desks, in the Mac's Settings**: who may ring him and when.
/// An edit sends only what changed and shows what the deck answered; a
/// refusal puts the old value back and says why in one line.
@MainActor
final class MacCallSettingsTests: XCTestCase {

    func testAnEditSendsOnlyTheChangedFieldAndAdoptsTheDecksAnswer() async {
        let deck = RingDeck()
        let model = CallSettingsModel(client: deck)
        await model.load()
        XCTAssertEqual(model.phase, .ready)
        var edited = model.settings
        edited.when = .urgent
        await model.apply(edited)
        XCTAssertEqual(deck.patches, [["when": .text("urgent")]], "only what he changed")
        XCTAssertEqual(model.settings.maxPerDay, 5, "the deck's answer is what is shown")
        XCTAssertNil(model.problem)
    }

    func testARefusedEditSaysWhyAndPutsTheOldValueBack() async {
        let deck = RingDeck()
        deck.updateError = DeckError.transport("max_per_day must be 0..20")
        let model = CallSettingsModel(client: deck)
        await model.load()
        var edited = model.settings
        edited.maxPerDay = 9
        await model.apply(edited)
        XCTAssertEqual(model.settings, .deckDefault, "reverted")
        XCTAssertNotNil(model.problem)
        XCTAssertFalse(model.problem?.contains("\n") ?? true)
    }

    func testADeckThatCannotBeCalledSaysSoInOneLine() {
        let model = CallSettingsModel(client: nil)
        XCTAssertEqual(model.phase, .unavailable)
        let (host, window) = RightPaneFixture.host(Form { CallsFromDesksSection(client: nil) },
                                                   size: CGSize(width: 360, height: 200))
        defer { window.orderOut(nil); window.contentView = nil }
        let words = RightPaneFixture.render(host).map { RightPaneFixture.readText($0).map(\.text) }?
            .joined(separator: " | ") ?? ""
        XCTAssertTrue(words.contains("This deck can't take calls from desks yet."), words)
    }
}

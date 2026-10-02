import XCTest

/// **Listen is mounted where he looks, on both apps, from one shared view
/// file.** A control nobody mounts passes every behaviour test, so this pins
/// the wiring: the bubble's control, the player strip, the phone target
/// compiling the shared file, and the voice features stopping playback.
final class ListenIsMountedOnBothAppsTests: XCTestCase {

    private var repo: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private func source(_ path: String) throws -> String {
        try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheMacBubbleAndThreadMountTheControlAndTheStrip() throws {
        let thread = try source("macos/Sources/DeckUI/ThreadView.swift")
        XCTAssertTrue(thread.contains("ListenOnBubble(player: VoiceDock.shared.listen"), "no Listen on the Mac bubble")
        XCTAssertTrue(thread.contains("ListenStrip(player: voice.listen)"), "no player strip on the Mac")
    }

    func testThePhoneBubbleAndThreadMountTheControlAndTheStrip() throws {
        let thread = try source("ios/AgentDeckPhone/ThreadScreen.swift")
        XCTAssertTrue(thread.contains("ListenButton(player: voice.listen"), "no Listen on the phone bubble")
        XCTAssertTrue(thread.contains("ListenMenuItem(player: voice.listen"), "no long-press Listen")
        XCTAssertTrue(thread.contains("ListenStrip(player: voice.listen)"), "no player strip on the phone")
        XCTAssertTrue(try source("ios/project.yml").contains("../macos/Sources/DeckUI/ListenViews.swift"),
                      "the phone does not compile the shared Listen views")
    }

    func testACallOrAVoiceMessageStopsListeningOnBothApps() throws {
        let mac = try source("macos/Sources/DeckUI/CallBarView.swift")
        XCTAssertTrue(mac.contains("listen.voiceBusy("), "the Mac never tells Listen a call started")
        let phone = try source("ios/AgentDeckPhone/PhoneVoice.swift")
        XCTAssertTrue(phone.contains("listen.voiceBusy("), "the phone never tells Listen a call started")
        XCTAssertTrue(try source("ios/AgentDeckPhone/AgentDeckPhoneApp.swift").contains("voice.bindBusy(to: calls.session)"))
    }

    func testListenUsesTheDeckVoiceAndNeverTheSpokenRepliesSwitch() throws {
        let player = try source("macos/Sources/DeckKit/Voice/ListenPlayer.swift")
        XCTAssertFalse(player.contains("SpokenReplies"), "Listen must not read or write the replies switch")
        XCTAssertTrue(player.contains("client.speech("), "Listen must ask the deck for the voice")
    }

    func testTheSharedViewsUseNoStockAccentOrBlue() throws {
        let views = try source("macos/Sources/DeckUI/ListenViews.swift")
        for banned in EveryControlWearsTheSharedThemeTests.banned {
            XCTAssertFalse(views.contains(banned), banned)
        }
        XCTAssertFalse(views.contains("Slider("), "a stock Slider paints the accent")
    }
}

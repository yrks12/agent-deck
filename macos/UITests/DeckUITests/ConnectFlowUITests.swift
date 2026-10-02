import XCTest

/// Click-path of the easy-setup plan (section 7, steps 2-7), driven against a live box.
/// Screen-gated like every UI test here (`YOS_SCREEN_IS_FREE=1`) and additionally needs the
/// live box's pairing code:
///
/// ```console
/// $ YOS_SCREEN_IS_FREE=1 DECK_PAIR_CODE='ADK1.…' DECK_STATE_SUITE=livepair \
///   xcodebuild -project UITests/DeckUITests.xcodeproj -scheme DeckUITests \
///   -destination 'platform=macOS,arch=arm64' -only-testing:DeckUITests/ConnectFlowUITests test
/// ```
///
/// Written before the Connect screen exists (slice F2): it FAILS today at step 2 by design.
/// Accessibility identifiers used here are the contract F2 must provide:
/// `connect.codeField`, `connect.pasteButton`, `connect.connectedLabel`, `connect.error`,
/// `settings.checkConnection`, `settings.checkLine`, `connect.sheet`.
final class ConnectFlowUITests: XCTestCase {

    override func setUpWithError() throws {
        // Default deny: this launches the real app and takes the screen.
        try requireTheScreenIsFree()
        try XCTSkipUnless(
            !(ProcessInfo.processInfo.environment["DECK_PAIR_CODE"] ?? "").isEmpty,
            "DECK_PAIR_CODE not set (mint one with `deckctl pair` on the live box)")
        continueAfterFailure = false
    }

    private func launch() -> XCUIApplication {
        let app = XCUIApplication()
        app.launchEnvironment["DECK_STATE_SUITE"] = "livepair"
        app.launch()
        return app
    }

    /// Steps 2-3 and 5: nothing paired -> Connect screen; paste -> connected within 5 s, no
    /// Keychain dialog; Settings -> Check connection shows seven passing lines.
    func testPairingByPastingTheCodeConnectsAndTheCheckIsGreen() {
        let app = launch()
        XCTAssertTrue(app.staticTexts["Connect to your server"].waitForExistence(timeout: 30),
                      "a fresh state suite must open on the Connect screen")

        let field = app.textFields["connect.codeField"]
        XCTAssertTrue(field.waitForExistence(timeout: 5), "no code paste field")
        field.click()
        field.typeText(ProcessInfo.processInfo.environment["DECK_PAIR_CODE"] ?? "")
        app.buttons["connect.pasteButton"].click()

        XCTAssertTrue(app.staticTexts["connect.connectedLabel"].waitForExistence(timeout: 5),
                      "not connected within 5 s of pasting the code")
        // A Keychain prompt is a separate system process' window; it must not appear.
        let securityAgent = XCUIApplication(bundleIdentifier: "com.apple.SecurityAgent")
        XCTAssertFalse(securityAgent.windows.firstMatch.exists, "a Keychain dialog appeared")

        app.buttons["settings.checkConnection"].click()
        let ticks = app.staticTexts.matching(identifier: "settings.checkLine")
        XCTAssertTrue(ticks.firstMatch.waitForExistence(timeout: 20))
        XCTAssertEqual(ticks.count, 7, "Check connection must list seven lines")
        for i in 0..<ticks.count {
            XCTAssertTrue(ticks.element(boundBy: i).label.hasPrefix("\u{2713}"),
                          "check line \(i) is not a pass: \(ticks.element(boundBy: i).label)")
        }
    }

    /// Step 6: a relaunch (same suite) is still connected and raises no Keychain dialog.
    func testARelaunchStaysConnectedWithoutAKeychainDialog() {
        var app = launch()
        _ = app.staticTexts["connect.connectedLabel"].waitForExistence(timeout: 30)
        app.terminate()
        app = launch()
        XCTAssertTrue(app.staticTexts["connect.connectedLabel"].waitForExistence(timeout: 10),
                      "the pairing did not survive a relaunch")
        XCTAssertFalse(XCUIApplication(bundleIdentifier: "com.apple.SecurityAgent").windows.firstMatch.exists)
    }

    /// Step 7: after `deckctl revoke <id>` on the box the app says so and reopens Connect.
    /// Set DECK_REVOKED=1 once the operator has revoked this device.
    func testARevokedKeyShowsTheSentenceAndTheConnectSheet() throws {
        try XCTSkipUnless(ProcessInfo.processInfo.environment["DECK_REVOKED"] == "1",
                          "set DECK_REVOKED=1 after `deckctl revoke`")
        let app = launch()
        XCTAssertTrue(app.staticTexts["connect.error"].waitForExistence(timeout: 30))
        XCTAssertTrue(app.staticTexts["connect.error"].label.localizedCaseInsensitiveContains("no longer accepted"))
        XCTAssertTrue(app.otherElements["connect.sheet"].exists)
    }
}

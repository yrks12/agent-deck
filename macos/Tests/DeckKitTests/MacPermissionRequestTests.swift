import XCTest
@testable import DeckKit

/// **The permission buttons put Agent Deck in the list he has to tick.**
///
/// MEASURED 2026-10-01: his Mac reported Accessibility and Screen Recording
/// both off, and "Open System Settings…" only opened the pane. macOS lists an
/// app there only after the app has asked once, so he would face a list
/// without Agent Deck in it and a "+" to hunt with. Each button now asks
/// macOS first (which adds the row and shows its own prompt), then opens the
/// pane.
final class MacPermissionRequestTests: XCTestCase {
    private func rig() -> (MacPermissionRequest, () -> [String]) {
        var log: [String] = []
        let r = MacPermissionRequest(askAccessibility: { log.append("ask-ax") },
                                     askScreenRecording: { log.append("ask-sr") },
                                     open: { log.append($0.absoluteString) })
        return (r, { log })
    }

    func testAccessibilityAsksMacOSThenOpensItsPane() {
        let (r, log) = rig()
        r.request(MacPermissionRequest.accessibilityPane)
        XCTAssertEqual(log(), ["ask-ax",
                               "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"])
    }

    func testScreenRecordingAsksMacOSThenOpensItsPane() {
        let (r, log) = rig()
        r.request(MacPermissionRequest.screenRecordingPane)
        XCTAssertEqual(log(), ["ask-sr",
                               "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture"])
    }

    func testAnyOtherPaneOnlyOpens() {
        let (r, log) = rig()
        r.request("Privacy_AllFiles")
        XCTAssertEqual(log(), ["x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])
    }
}

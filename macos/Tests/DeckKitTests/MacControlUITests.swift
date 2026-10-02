import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **While agents may control his Mac, he always sees it, and can stop it.**
///
/// What goes wrong without these: control is on and nothing on screen says
/// so; the menu bar reads "connected" while an agent types; the banner's
/// Allow does nothing because the tap is treated as "open a thread"; the
/// stop shortcut is a symbol nobody can type.
@MainActor
final class MacControlUITests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_788_222_702)

    func testTheMenuBarSaysControlIsOnAndOffersStop() {
        var s = MacBridgeState(mode: .ask, connection: .online)
        XCTAssertNil(MacMenuBarModel(state: s, now: t0).controlStatus)
        s.control = .start(scope: "atlas", now: t0.timeIntervalSince1970)
        let m = MacMenuBarModel(state: s, now: t0.addingTimeInterval(120))
        XCTAssertEqual(m.controlStatus, "Atlas is controlling your Mac · 28 min left")
        XCTAssertEqual(m.symbol, "cursorarrow.motionlines")
        XCTAssertNotNil(NSImage(systemSymbolName: m.symbol, accessibilityDescription: nil))
        XCTAssertTrue(m.stopControlTitle.contains("⌃⌥⌘."))
        let ended = MacMenuBarModel(state: s, now: t0.addingTimeInterval(MacControlGrant.duration + 1))
        XCTAssertNil(ended.controlStatus, "an expired grant is not shown as live")
    }

    func testTheBannerNamesTheDeskOrSaysAgents() {
        let now = t0.timeIntervalSince1970
        XCTAssertEqual(MacControlCopy.banner(scope: nil, now: now, until: now + 1800),
                       "Agents are controlling your Mac · 30 min left")
        XCTAssertEqual(MacControlCopy.banner(scope: "nova", now: now, until: now + 30),
                       "Nova is controlling your Mac · 1 min left")
    }

    func testAllowOnTheBannerIsAnAnswerNotAThreadTap() {
        var got: (String, Bool)?
        MacControlNotifications.onAnswer = { got = ($0, $1) }
        defer { MacControlNotifications.onAnswer = nil }
        let info: [AnyHashable: Any] = ["mac_control_desk": "atlas"]
        XCTAssertTrue(MacControlNotifications.handle(actionIdentifier: MacControlNotifications.allowAction,
                                                     userInfo: info))
        XCTAssertEqual(got?.0, "atlas")
        XCTAssertEqual(got?.1, true)
        XCTAssertFalse(MacControlNotifications.handle(actionIdentifier: "com.apple.UNNotificationDefaultActionIdentifier",
                                                      userInfo: info))
        XCTAssertEqual(MacControlNotifications.content(desk: "atlas").title, "Atlas wants to control this Mac")
    }

    func testTheStopHotkeyIsControlOptionCommandPeriod() {
        XCTAssertEqual(MacControlHotkey.keyCode, 47)   // kVK_ANSI_Period
        XCTAssertEqual(MacControlCopy.hotkey, "⌃⌥⌘.")
    }

    func testTheLiveViewIsTheDisplayInPointsCapped() {
        XCTAssertEqual(MacScreenStreamer.frameSize(display: MacDisplayFrame(x: 0, y: 0, width: 1512, height: 982)),
                       MacSpace(width: 1512, height: 982))
        XCTAssertEqual(MacScreenStreamer.frameSize(display: MacDisplayFrame(x: 0, y: 0, width: 3200, height: 1800)),
                       MacSpace(width: 1600, height: 900))
    }

    func testOurOwnEventsDoNotCountAsHisHands() {
        let activity = MacOwnerActivity()
        XCTAssertFalse(activity.isActive(now: t0))
        activity.touched(at: t0)
        XCTAssertTrue(activity.isActive(now: t0.addingTimeInterval(1)))
        XCTAssertFalse(activity.isActive(now: t0.addingTimeInterval(MacOwnerActivity.pause + 0.1)))
        XCTAssertEqual(MacInputInjector.marker, 0x4445_434B)
    }

    func testTheBannerViewDraws() {
        let host = NSHostingView(rootView: MacControlBannerView(text: "Atlas is controlling your Mac · 5 min left",
                                                                onStop: {}))
        host.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(host.fittingSize.width, 200)
    }
}

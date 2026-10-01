import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The words and the rules on Settings → Mac**, decided without a window.
final class MacUISettingsCopyTests: XCTestCase {
    private let t0 = Date(timeIntervalSince1970: 1_788_222_000)

    func testADesksAccessReadsLikeThePlan() {
        let now = t0
        let hour = MacGrant(state: .hour, until: now.timeIntervalSince1970 + 32 * 60)
        XCTAssertEqual(MacAccessCopy.grantLine(hour, now: now), "1 hour (32 min left)")
        let almost = MacGrant(state: .hour, until: now.timeIntervalSince1970 + 20)
        XCTAssertEqual(MacAccessCopy.grantLine(almost, now: now), "1 hour (under a minute left)")
        XCTAssertEqual(MacAccessCopy.grantLine(MacGrant(state: .always, until: nil), now: now), "Always")

        var utc = Calendar(identifier: .gregorian); utc.timeZone = TimeZone(identifier: "UTC")!
        let at1400 = utc.date(from: DateComponents(year: 2026, month: 9, day: 30, hour: 14))!
        let denied = MacGrant(state: .denied, until: at1400.timeIntervalSince1970)
        XCTAssertEqual(MacAccessCopy.grantLine(denied, now: at1400.addingTimeInterval(-3600),
                                               locale: Locale(identifier: "en_GB"), timeZone: utc.timeZone),
                       "Denied until 14:00")
    }

    func testOnlyLiveGrantsAreListedAndAlphabetical() {
        var config = MacPolicyConfig()
        config.grants = [
            "nova": MacGrant(state: .always, until: nil),
            "atlas": MacGrant(state: .hour, until: t0.timeIntervalSince1970 + 600),
            "old": MacGrant(state: .hour, until: t0.timeIntervalSince1970 - 5),
            "gone": MacGrant(state: .denied, until: t0.timeIntervalSince1970 - 5),
        ]
        XCTAssertEqual(MacAccessCopy.liveGrants(config, now: t0).map(\.desk), ["atlas", "nova"])
    }

    func testFullAccessAsksBeforeItTurnsOnAndNothingElseDoes() {
        XCTAssertEqual(MacAccessCopy.modeChange(from: .ask, to: .full), .confirmFull)
        XCTAssertEqual(MacAccessCopy.modeChange(from: .off, to: .full), .confirmFull)
        XCTAssertEqual(MacAccessCopy.modeChange(from: .paused, to: .full), .confirmFull)
        XCTAssertEqual(MacAccessCopy.modeChange(from: .full, to: .full), .apply(.full), "already on: nothing to confirm")
        XCTAssertEqual(MacAccessCopy.modeChange(from: .full, to: .ask), .apply(.ask))
        XCTAssertEqual(MacAccessCopy.modeChange(from: .ask, to: .off), .apply(.off))
    }

    func testTheConfirmationSheetSaysWhatFullAccessMeans() {
        let body = MacAccessCopy.fullAccessBody(host: "deck.initech.example")
        XCTAssertEqual(body,
            "Any desk on deck.initech.example will be able to run anything on this Mac as you — read your files, keys and anything your account can reach — without asking. Only for a server you control.")
        XCTAssertTrue(MacAccessCopy.fullAccessBody(host: nil).hasPrefix("Any desk on your deck will"))
        XCTAssertEqual(MacAccessCopy.fullAccessConfirm, "Turn on full access")
    }

    func testTheTransportSentenceIsTheGuardsOwn() {
        XCTAssertEqual(MacAccessCopy.transportSentence(for: .refused(MacTransport.refusal)), MacTransport.refusal)
        XCTAssertNil(MacAccessCopy.transportSentence(for: .online))
        XCTAssertNil(MacAccessCopy.transportSentence(for: .off))
    }

    func testPathsShowWithATildeAndFolderNamesWithoutOne() {
        XCTAssertEqual(MacAccessCopy.shortPath("/Users/y/Projects/acme", home: "/Users/y"), "~/Projects/acme")
        XCTAssertEqual(MacAccessCopy.shortPath("/opt/x", home: "/Users/y"), "/opt/x")
        XCTAssertEqual(MacAccessCopy.shortPath("/Users/sam/x", home: "/Users/y"), "/Users/sam/x",
                       "a sibling home directory is not inside this one")
    }

    func testResetRestoresTheShippedList() {
        var config = MacPolicyConfig()
        config.neverTouch = ["~/only-this"]
        MacAccessCopy.resetNeverTouch(&config)
        XCTAssertEqual(config.neverTouch, MacPolicyConfig.defaultNeverTouch)
    }

    func testAddingAListEntryTrimsDedupesAndIgnoresBlank() {
        var list = ["~/.ssh/**"]
        MacAccessCopy.add("  ~/secrets/** ", to: &list)
        MacAccessCopy.add("~/secrets/**", to: &list)
        MacAccessCopy.add("   ", to: &list)
        XCTAssertEqual(list, ["~/.ssh/**", "~/secrets/**"])
    }

    func testEveryModeHasAPlainSentenceAndNoneUsesJargon() {
        for mode in [MacMode.off, .ask, .full, .paused] {
            let s = MacAccessCopy.modeBlurb(mode)
            XCTAssertFalse(s.isEmpty)
            for word in ["sandbox", "seatbelt", "POSIX", "TCC"] {
                XCTAssertFalse(s.localizedCaseInsensitiveContains(word), "\(mode): '\(word)' is jargon")
            }
        }
    }
}

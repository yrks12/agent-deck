import XCTest
@testable import DeckKit

/// MEASURED (PR #164): Google, YouTube and Gmail sessions in his everyday Chrome
/// are Device Bound, so "Use my Chrome login" copies cookies Google rejects on a
/// desk. For those hosts the fresh passkey sign-in is the primary action.
final class GoogleSignInSteerTests: XCTestCase {
    func testGoogleFamilyLeadsWithFreshPasskey() {
        for host in ["accounts.google.com", "google.com", "studio.youtube.com", "www.youtube.com",
                     "mail.google.com", "gmail.com", "search.google.com"] {
            XCTAssertEqual(MacSignIn.primary(host: host, hasBrowser: true), .freshPasskey, host)
        }
    }

    func testOtherSitesKeepChromeLoginFirst() {
        for host in ["github.com", "example.com", "notgoogle.com", "google.com.evil.example"] {
            XCTAssertEqual(MacSignIn.primary(host: host, hasBrowser: true), .chromeLogin, host)
        }
    }

    func testNoBrowserMeansPasskeyEvenElsewhere() {
        XCTAssertEqual(MacSignIn.primary(host: "github.com", hasBrowser: false), .freshPasskey)
    }

    func testChromeLoginIsDemotedWithAReasonOnGoogle() {
        XCTAssertNotNil(MacSignIn.chromeLoginDemotion(host: "studio.youtube.com"))
        XCTAssertTrue(MacSignIn.chromeLoginDemotion(host: "studio.youtube.com")!
            .contains("Google can't be copied from your Chrome"))
        XCTAssertNil(MacSignIn.chromeLoginDemotion(host: "github.com"))
    }
}

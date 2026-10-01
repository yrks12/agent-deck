import XCTest
@testable import DeckKit

/// **A-6: no bridge over plain HTTP to a public host.** Shell commands and
/// file contents would cross the internet readable and forgeable. The owner's
/// WireGuard `http://10.99.0.1` and Tailscale stay allowed.
final class MacTransportTests: XCTestCase {

    private func ok(_ s: String) -> Bool { MacTransport.allowed(URL(string: s)!) }

    func testPublicPlainHTTPIsRefused() {
        XCTAssertFalse(ok("http://1.2.3.4:7789"))
        XCTAssertFalse(ok("http://deck.example.com"))
        XCTAssertFalse(ok("http://172.32.0.1"))
        XCTAssertFalse(ok("http://100.128.0.1"))
        XCTAssertFalse(ok("http://[2001:db8::1]"))
        XCTAssertFalse(ok("http://10.0.0.1.evil.com"))
        XCTAssertFalse(ok("http://ts.net.evil.com"))
        XCTAssertFalse(ok("ftp://10.0.0.1"))
    }

    func testPrivateNetworksAndHTTPSAreAllowed() {
        XCTAssertTrue(ok("http://10.99.0.1:7789"))
        XCTAssertTrue(ok("http://100.101.1.2"))
        XCTAssertTrue(ok("http://127.0.0.1:7789"))
        XCTAssertTrue(ok("http://localhost:7789"))
        XCTAssertTrue(ok("http://192.168.1.20"))
        XCTAssertTrue(ok("http://172.16.4.4"))
        XCTAssertTrue(ok("http://box.tail1234.ts.net"))
        XCTAssertTrue(ok("http://[::1]:7789"))
        XCTAssertTrue(ok("http://[fd7a:115c::1]"))
        XCTAssertTrue(ok("https://deck.example.com"))
    }

    func testTheSentenceIsTheOneInThePlan() {
        XCTAssertTrue(MacTransport.refusal.contains("needs HTTPS or a private network"))
    }
}

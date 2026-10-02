import XCTest
@testable import DeckKit

/// K9: pin mode trusts SHA-256 of the leaf SPKI and nothing else.
final class PinnedTrustTests: XCTestCase {

    private var server: LocalTLSServer!

    override func setUp() async throws {
        server = try LocalTLSServer()
        try await server.start()
    }

    override func tearDown() { server.stop() }

    private func get(pin: String?) async throws -> (Data, URLResponse, PinnedTrust?) {
        let made = PinnedTrust.session(pin: pin, configuration: .ephemeral)
        defer { made.session.invalidateAndCancel() }
        let (d, r) = try await made.session.data(from: server.url.appendingPathComponent("healthz"))
        return (d, r, made.trust)
    }

    func testThePinIsTheSHA256OfTheLeafSPKI() {
        XCTAssertEqual(PinnedTrust.pin(ofCertificateDER: LocalTLSServer.certificateDER), LocalTLSServer.pin)
    }

    func testASelfSignedServerIsTrustedWhenTheKeyMatchesThePin() async throws {
        let (data, response, trust) = try await get(pin: LocalTLSServer.pin)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertEqual(String(data: data, encoding: .utf8), #"{"ok":true}"#)
        XCTAssertEqual(trust?.didRejectKey, false)
    }

    func testAnyOtherKeyIsRefusedAndSaysSo() async throws {
        let wrong = "sha256/" + Data(repeating: 7, count: 32).base64EncodedString()
        let made = PinnedTrust.session(pin: wrong, configuration: .ephemeral)
        defer { made.session.invalidateAndCancel() }
        do {
            _ = try await made.session.data(from: server.url)
            XCTFail("a key that is not the pin must not be trusted")
        } catch {
            XCTAssertEqual(made.trust?.didRejectKey, true)
        }
        XCTAssertTrue(server.requests.isEmpty, "no request may reach a server whose key does not match")
    }

    /// Without a pin the same server is just an untrusted self-signed cert,
    /// which is the default and must stay that way for tls="ca".
    func testCAModeDoesNotTrustASelfSignedServer() async {
        do {
            _ = try await get(pin: nil)
            XCTFail("system trust must reject a self-signed certificate")
        } catch {}
        do {
            _ = try await get(pin: "")
            XCTFail("an empty pin means CA mode")
        } catch {}
    }

    func testAMalformedPinNeverTrustsAnything() async {
        for bad in ["sha256/", "sha256/AAAA", "md5/abc", "garbage"] {
            do {
                _ = try await get(pin: bad)
                XCTFail("\(bad) must not match")
            } catch {}
        }
    }

    func testBothTransportsCanRideThePinnedSession() async throws {
        let made = PinnedTrust.session(pin: LocalTLSServer.pin, configuration: .ephemeral)
        defer { made.session.invalidateAndCancel() }
        let performer = URLSessionPerformer(session: made.session)
        let (_, http) = try await performer.perform(URLRequest(url: server.url.appendingPathComponent("v1/version")))
        XCTAssertEqual(http.statusCode, 200)
        _ = URLSessionSSETransport(session: made.session)
    }

    func testPinSyntaxCheck() {
        XCTAssertTrue(PinnedTrust.isWellFormed(pin: LocalTLSServer.pin))
        XCTAssertFalse(PinnedTrust.isWellFormed(pin: "sha256/short"))
        XCTAssertFalse(PinnedTrust.isWellFormed(pin: ""))
    }
}

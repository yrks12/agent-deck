import XCTest
@testable import DeckKit

/// **Connect: the browser comes back to this Mac.** The provider redirects to
/// `http://127.0.0.1:47689/callback?code&state`; the app listens there for
/// exactly one such request, answers it with a small page, and hands the
/// whole URL to `connect/complete`. The parsing is pure and pinned here
/// without a socket; the listener is pinned with a real loopback connection.
final class OAuthLoopbackTests: XCTestCase {

    // MARK: the request line -> the callback URL

    func testACallbackRequestBecomesTheFullLoopbackURL() {
        let head = "GET /callback?code=abc123&state=xyz789 HTTP/1.1\r\nHost: 127.0.0.1:47689\r\nAccept: */*\r\n\r\n"

        XCTAssertEqual(
            OAuthLoopback.parse(requestHead: head, port: 47689),
            .callback(URL(string: "http://127.0.0.1:47689/callback?code=abc123&state=xyz789")!))
    }

    func testAProviderRefusalIsStillTheCallback() {
        let head = "GET /callback?error=access_denied&state=xyz HTTP/1.1\r\n\r\n"

        XCTAssertEqual(
            OAuthLoopback.parse(requestHead: head, port: 47689),
            .callback(URL(string: "http://127.0.0.1:47689/callback?error=access_denied&state=xyz")!),
            "the deck has to hear a refusal too, or the sign-in sits pending until it expires")
    }

    /// A browser asks for `/favicon.ico` alongside; a stray POST or a callback
    /// with no state is not his sign-in. None of them may end the wait.
    func testAnythingElseIsIgnoredAndDoesNotEndTheWait() {
        let ignored: [String] = [
            "GET /favicon.ico HTTP/1.1\r\n\r\n",
            "POST /callback?code=a&state=b HTTP/1.1\r\n\r\n",
            "GET /callback?code=a HTTP/1.1\r\n\r\n",
            "GET /callback?state=b HTTP/1.1\r\n\r\n",
            "GET /callbackish?code=a&state=b HTTP/1.1\r\n\r\n",
            "garbage",
            "",
        ]
        for head in ignored {
            guard case .ignored = OAuthLoopback.parse(requestHead: head, port: 47689) else {
                return XCTFail("treated as the callback: \(head.debugDescription)")
            }
        }
    }

    func testThePageSaysConnectedAndNeverEchoesTheCodeOrState() throws {
        let url = URL(string: "http://127.0.0.1:47689/callback?code=SECRETCODE&state=SECRETSTATE")!
        let page = String(decoding: OAuthLoopback.response(for: .callback(url)), as: UTF8.self)

        XCTAssertTrue(page.hasPrefix("HTTP/1.1 200 OK\r\n"), page)
        XCTAssertTrue(page.contains("Content-Type: text/html; charset=utf-8"), page)
        XCTAssertTrue(page.contains("Connection: close"), page)
        XCTAssertTrue(page.contains("Connected — you can close this tab"), page)
        XCTAssertFalse(page.contains("SECRETCODE"))
        XCTAssertFalse(page.contains("SECRETSTATE"))
    }

    func testARefusalPageShowsTheErrorEscaped() {
        let url = URL(string: "http://127.0.0.1:47689/callback?error=%3Cscript%3Ex&state=s")!
        let page = String(decoding: OAuthLoopback.response(for: .callback(url)), as: UTF8.self)

        XCTAssertFalse(page.contains("Connected — you can close this tab"))
        XCTAssertTrue(page.contains("&lt;script&gt;x"), page)
        XCTAssertFalse(page.contains("<script>"), page)
    }

    func testAnIgnoredRequestGetsA404() {
        let page = String(decoding: OAuthLoopback.response(for: .ignored(status: 404)), as: UTF8.self)
        XCTAssertTrue(page.hasPrefix("HTTP/1.1 404"), page)
    }

    func testThePortComesFromTheDecksRedirectURI() {
        XCTAssertEqual(OAuthLoopback.port(fromRedirectURI: "http://127.0.0.1:47689/callback"), 47689)
        XCTAssertEqual(OAuthLoopback.port(fromRedirectURI: "http://127.0.0.1:50000/callback"), 50000)
        XCTAssertNil(OAuthLoopback.port(fromRedirectURI: "https://deck.example/callback"),
                     "only a loopback redirect is one this Mac can catch")
        XCTAssertNil(OAuthLoopback.port(fromRedirectURI: "http://localhost:47689/callback"),
                     "IPv4 loopback only, the address the provider was registered with")
    }

    // MARK: the paste fallback

    func testAPastedAddressIsAcceptedTrimmedAndAnythingElseIsNot() {
        XCTAssertEqual(
            OAuthLoopback.pastedCallbackURL("  http://127.0.0.1:47689/callback?code=a&state=b \n"),
            "http://127.0.0.1:47689/callback?code=a&state=b")
        XCTAssertEqual(
            OAuthLoopback.pastedCallbackURL("http://127.0.0.1:47689/callback?error=access_denied&state=b"),
            "http://127.0.0.1:47689/callback?error=access_denied&state=b")
        XCTAssertNil(OAuthLoopback.pastedCallbackURL("hello"))
        XCTAssertNil(OAuthLoopback.pastedCallbackURL("http://127.0.0.1:47689/callback?code=a"),
                     "no state, so the deck could not match it")
        XCTAssertNil(OAuthLoopback.pastedCallbackURL("https://www.notion.so/login"),
                     "the consent page itself is not where the browser ended")
    }

    // MARK: status while waiting

    func testStatusDecodesAndMapsToWhatTheWaitShows() throws {
        func status(_ json: String) throws -> StoreConnectStatus {
            try DeckCoding.decoder.decode(StoreConnectStatus.self, from: Data(json.utf8))
        }
        XCTAssertEqual(try status(#"{"state":"pending","id":"mcp:com.notion/mcp","detail":""}"#).phase, .waiting)
        XCTAssertEqual(try status(#"{"state":"done","id":"mcp:com.notion/mcp","detail":""}"#).phase, .done)
        XCTAssertEqual(try status(#"{"state":"failed","id":"x","detail":"declined at the provider"}"#).phase,
                       .failed("declined at the provider"))
        XCTAssertEqual(try status(#"{"state":"expired","id":"x","detail":""}"#).phase, .expired)
        XCTAssertEqual(try status(#"{"state":"something_new","id":"x","detail":""}"#).phase, .waiting,
                       "an unknown state keeps waiting until expiry rather than inventing an outcome")
    }

    // MARK: the listener, on a real loopback socket

    private func freePort() -> UInt16 { UInt16.random(in: 49_200...60_000) }

    func testTheListenerAnswersOneCallbackAndHandsBackItsURL() async throws {
        let port = freePort()
        let listener = OAuthLoopbackListener(port: port)
        try await listener.start()

        async let caught = listener.callback(until: Date().addingTimeInterval(10))

        // A favicon first, like a browser: answered 404, the wait goes on.
        let (_, favicon) = try await URLSession.shared.data(
            from: URL(string: "http://127.0.0.1:\(port)/favicon.ico")!)
        XCTAssertEqual((favicon as? HTTPURLResponse)?.statusCode, 404)

        let (body, response) = try await URLSession.shared.data(
            from: URL(string: "http://127.0.0.1:\(port)/callback?code=c0de&state=st4te")!)
        XCTAssertEqual((response as? HTTPURLResponse)?.statusCode, 200)
        XCTAssertTrue(String(decoding: body, as: UTF8.self).contains("Connected — you can close this tab"))

        let url = try await caught
        XCTAssertEqual(url.absoluteString, "http://127.0.0.1:\(port)/callback?code=c0de&state=st4te")

        // Exactly one: it has stopped listening.
        do {
            _ = try await URLSession.shared.data(
                from: URL(string: "http://127.0.0.1:\(port)/callback?code=again&state=x")!)
            XCTFail("the listener answered a second callback")
        } catch {}
    }

    func testTheListenerGivesUpAtTheDeadline() async throws {
        let listener = OAuthLoopbackListener(port: freePort())
        try await listener.start()

        do {
            _ = try await listener.callback(until: Date().addingTimeInterval(0.3))
            XCTFail("waited past the deadline")
        } catch let error as OAuthLoopbackError {
            XCTAssertEqual(error, .timedOut)
        }
    }

    func testABusyPortIsReportedSoThePasteFallbackCanTakeOver() async throws {
        let port = freePort()
        let first = OAuthLoopbackListener(port: port)
        try await first.start()
        defer { first.cancel() }

        do {
            try await OAuthLoopbackListener(port: port).start()
            XCTFail("two listeners on one port")
        } catch let error as OAuthLoopbackError {
            guard case .portUnavailable = error else { return XCTFail("\(error)") }
        }
    }
}

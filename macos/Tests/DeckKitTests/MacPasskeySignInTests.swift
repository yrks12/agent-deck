import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"i need to be able to login with the passkeys from my mac on their
/// computers."**
///
/// His passkeys live in iCloud Keychain — on this Mac and his iPhone, never in
/// a desk's Linux container — so a passkey-only sign-in cannot finish on a
/// desk's screen. The app offers "Sign in on this Mac" on a browser sign-in
/// card: a throwaway Chrome profile opens here (Chrome on macOS offers iCloud
/// passkeys to any profile — measured), he signs in with Touch ID, the app
/// reads that profile's cookies over CDP and hands them to the deck on
/// `POST /v1/logins`, which puts them in every desk's browser. Then Chrome is
/// closed and the profile deleted, whatever happened.
///
/// Fakes only. **No test here launches Chrome or opens a socket**; the real
/// browser is covered by its pure parts (argv, port file, CDP reply parsing,
/// the private profile directory).
final class MacPasskeySignInTests: XCTestCase {

    // MARK: fakes

    final class FakeWindow: SignInWindow, @unchecked Sendable {
        var cookies: Data
        var readError: Error?
        private(set) var closed = 0
        init(cookies: Data) { self.cookies = cookies }
        func cookieJSON() async throws -> Data {
            if let readError { throw readError }
            return cookies
        }
        func close() async { closed += 1 }
    }

    final class FakeBrowser: SignInBrowser, @unchecked Sendable {
        var opened: [URL] = []
        var window: FakeWindow
        var openError: Error?
        init(cookies: String = #"[{"name":"SID","value":"v","domain":".google.com"}]"#) {
            window = FakeWindow(cookies: Data(cookies.utf8))
        }
        func open(_ url: URL) async throws -> SignInWindow {
            if let openError { throw openError }
            opened.append(url)
            return window
        }
    }

    final class FakeSharing: LoginSharingClient, @unchecked Sendable {
        var sent: [Data] = []
        var error: Error?
        var reply = LoginShare(cookies: 1, desks: 3, failed: [])
        func shareLogins(cookieJSON: Data) async throws -> LoginShare {
            if let error { throw error }
            sent.append(cookieJSON)
            return reply
        }
    }

    private func handoff(kind: String = "login",
                         place: String = "https://accounts.google.com/v3/signin/challenge/pk?TL=abc")
        throws -> AttentionItem {
        let page = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1756000100,"agent":"acme","asked_by":"acme",
          "desk_known":true,"kind":"\(kind)",
          "needs":"This site wants a passkey. Passkeys are on your own phone/Mac.",
          "state":"stopped","where":"\(place)",
          "evidence":"surface=browser desk=acme","status":"waiting","options":[
            {"reply":"done","available":true,"summary":"I'm done, continue - it checks"},
            {"reply":"skipped","available":true,"summary":"Skip this step - it gives up"}]}]}
        """.utf8))
        return AttentionItem.make(handoff: try XCTUnwrap(page.handoffs.first), agents: [:])
    }

    // MARK: 1 — which cards offer it, and where it goes

    func testABrowserSignInCardOffersTheMacAtTheSitesFrontDoor() throws {
        let item = try handoff()
        // Not the passkey challenge URL: that is one desk's half-finished
        // attempt and means nothing in a fresh browser.
        XCTAssertEqual(item.signInOnMacURL?.absoluteString, "https://accounts.google.com/")
    }

    func testOnlyASignInOverHTTPSOffersIt() throws {
        XCTAssertNil(try handoff(kind: "2fa").signInOnMacURL, "a code is not a sign-in")
        XCTAssertNil(try handoff(kind: "captcha").signInOnMacURL)
        XCTAssertNil(try handoff(place: "admin.microsoft.com").signInOnMacURL,
                     "a bare host is not somewhere to open")
        XCTAssertNil(try handoff(place: "http://intranet.local/login").signInOnMacURL,
                     "no passkey is offered to plain http")
        XCTAssertNil(try handoff(place: "").signInOnMacURL)
    }

    // MARK: 2 — the flow

    func testSignInOpensChromeThenSharesExactlyWhatItReadAndClosesIt() async throws {
        let browser = FakeBrowser(), sharing = FakeSharing()
        let flow = MacSignInFlow(browser: browser, sharing: sharing)
        let url = URL(string: "https://accounts.google.com/")!

        try await flow.start(id: "h1", url: url)
        XCTAssertEqual(browser.opened, [url])
        let isOpen = await flow.isOpen(id: "h1")
        XCTAssertTrue(isOpen)

        let share = try await flow.finish(id: "h1")
        XCTAssertEqual(share.desks, 3)
        XCTAssertEqual(sharing.sent, [browser.window.cookies],
                       "the cookies travel as read, untouched and unparsed")
        XCTAssertEqual(browser.window.closed, 1, "Chrome is closed and its profile deleted")
        let stillOpen = await flow.isOpen(id: "h1")
        XCTAssertFalse(stillOpen)
    }

    func testStartingTwiceOpensOneWindow() async throws {
        let browser = FakeBrowser()
        let flow = MacSignInFlow(browser: browser, sharing: FakeSharing())
        let url = URL(string: "https://github.com/")!
        try await flow.start(id: "h1", url: url)
        try await flow.start(id: "h1", url: url)
        XCTAssertEqual(browser.opened.count, 1)
    }

    func testARefusedUploadStillClosesChrome() async throws {
        let browser = FakeBrowser(), sharing = FakeSharing()
        sharing.error = DeckError.http(409, reason: "isolated")
        let flow = MacSignInFlow(browser: browser, sharing: sharing)
        try await flow.start(id: "h1", url: URL(string: "https://github.com/")!)
        do {
            _ = try await flow.finish(id: "h1")
            XCTFail("a refusal was swallowed")
        } catch {}
        XCTAssertEqual(browser.window.closed, 1)
    }

    func testNothingSignedInIsNotUploaded() async throws {
        let browser = FakeBrowser(cookies: "[]"), sharing = FakeSharing()
        let flow = MacSignInFlow(browser: browser, sharing: sharing)
        try await flow.start(id: "h1", url: URL(string: "https://github.com/")!)
        do {
            _ = try await flow.finish(id: "h1")
            XCTFail("an empty jar was shared")
        } catch let error as MacSignInError {
            XCTAssertEqual(error, .nothingSignedIn)
        }
        XCTAssertTrue(sharing.sent.isEmpty)
        XCTAssertEqual(browser.window.closed, 1)
    }

    func testChromeClosedBeforeSharingSaysSo() async throws {
        let browser = FakeBrowser(), sharing = FakeSharing()
        browser.window.readError = MacSignInError.chromeClosed
        let flow = MacSignInFlow(browser: browser, sharing: sharing)
        try await flow.start(id: "h1", url: URL(string: "https://github.com/")!)
        do {
            _ = try await flow.finish(id: "h1")
            XCTFail("a closed Chrome was treated as signed in")
        } catch let error as MacSignInError {
            XCTAssertEqual(error, .chromeClosed)
        }
        XCTAssertTrue(sharing.sent.isEmpty)
    }

    func testFinishingWithoutStartingIsRefused() async throws {
        let flow = MacSignInFlow(browser: FakeBrowser(), sharing: FakeSharing())
        do {
            _ = try await flow.finish(id: "nope")
            XCTFail()
        } catch let error as MacSignInError {
            XCTAssertEqual(error, .notStarted)
        }
    }

    func testCancelClosesChrome() async throws {
        let browser = FakeBrowser()
        let flow = MacSignInFlow(browser: browser, sharing: FakeSharing())
        try await flow.start(id: "h1", url: URL(string: "https://github.com/")!)
        await flow.cancel(id: "h1")
        XCTAssertEqual(browser.window.closed, 1)
    }

    func testEveryFailureHasASentenceForHim() {
        XCTAssertTrue(MacSignIn.sentence(for: MacSignInError.chromeMissing).contains("Google Chrome"))
        XCTAssertTrue(MacSignIn.sentence(for: DeckError.http(409, reason: "isolated"))
            .contains("shared_logins"))
        XCTAssertTrue(MacSignIn.sentence(for: MacSignInError.nothingSignedIn).contains("signed in"))
    }

    // MARK: 3 — the store: one tap to open, one tap to share, the card closes

    @MainActor
    func testTheStoreSharesThenAnswersTheCardDone() async throws {
        let client = ScriptedDeckClient()
        client.handoffsPage = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1756000100,"agent":"acme","asked_by":"acme",
          "desk_known":true,"kind":"login","needs":"Sign in with your passkey.",
          "state":"stopped","where":"https://accounts.google.com/v3/signin/challenge/pk",
          "evidence":"surface=browser","status":"waiting","options":[
            {"reply":"done","available":true,"summary":"I'm done, continue - it checks"},
            {"reply":"skipped","available":true,"summary":"Skip this step - it gives up"}]}]}
        """.utf8))
        let browser = FakeBrowser(), sharing = FakeSharing()
        let deck = DeckStore(client: client, approvalPollInterval: 600,
                             signIn: MacSignInFlow(browser: browser, sharing: sharing))
        await deck.loadApprovals()
        let item = try XCTUnwrap(deck.attention.first)

        await deck.startMacSignIn(item)
        XCTAssertEqual(deck.macSignIns["h1"], .waitingForYou)
        XCTAssertEqual(browser.opened.map(\.absoluteString), ["https://accounts.google.com/"])

        await deck.finishMacSignIn(item)
        XCTAssertEqual(deck.macSignIns["h1"], .shared(desks: 3))
        XCTAssertEqual(client.resolvedHandoffs.map(\.0), ["h1"])
        XCTAssertEqual(client.resolvedHandoffs.map(\.1), ["done"],
                       "signed in everywhere: the desk goes back and checks")
    }

    @MainActor
    func testARefusedShareLeavesTheCardUpAndSaysWhy() async throws {
        let client = ScriptedDeckClient()
        client.handoffsPage = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1,"agent":"acme","asked_by":"acme","desk_known":true,
          "kind":"login","needs":"Sign in.","state":"stopped","where":"https://github.com/login",
          "status":"waiting","options":[
            {"reply":"done","available":true,"summary":"I'm done, continue - it checks"}]}]}
        """.utf8))
        let sharing = FakeSharing()
        sharing.error = DeckError.http(409, reason: "isolated")
        let deck = DeckStore(client: client, approvalPollInterval: 600,
                             signIn: MacSignInFlow(browser: FakeBrowser(), sharing: sharing))
        await deck.loadApprovals()
        let item = try XCTUnwrap(deck.attention.first)
        await deck.startMacSignIn(item)
        await deck.finishMacSignIn(item)
        guard case .failed(let why) = deck.macSignIns["h1"] else {
            return XCTFail("\(String(describing: deck.macSignIns["h1"]))")
        }
        XCTAssertTrue(why.contains("shared_logins"))
        XCTAssertTrue(client.resolvedHandoffs.isEmpty, "nothing was shared, so nothing is done")
    }

    // MARK: 4 — the wire

    func testTheCookiesArePostedToTheWriteOnlyRouteAsRead() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(#"{"ok":true,"shared":true,"cookies":2,"desks":4,"failed":["ops"]}"#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)
        let jar = Data(#"[{"name":"SID","value":"v","domain":".google.com"}]"#.utf8)

        let share = try await client.shareLogins(cookieJSON: jar)

        XCTAssertEqual(performer.paths, ["/v1/logins"])
        XCTAssertEqual(performer.requests.first?.httpMethod, "POST")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(
            with: XCTUnwrap(performer.requests.first?.httpBody)) as? [String: Any])
        let rows = try XCTUnwrap(body["cookies"] as? [[String: Any]])
        XCTAssertEqual(rows.first?["name"] as? String, "SID")
        XCTAssertEqual(share, LoginShare(cookies: 2, desks: 4, failed: ["ops"]))
    }

    // MARK: 5 — the real Chrome, by its pure parts

    func testChromeGetsAThrowawayProfileAndALoopbackDebuggingPort() {
        let profile = URL(fileURLWithPath: "/tmp/agent-deck-signin-X")
        let argv = ChromeSignInBrowser.arguments(
            profile: profile, url: URL(string: "https://accounts.google.com/")!)
        XCTAssertTrue(argv.contains("--user-data-dir=/tmp/agent-deck-signin-X"),
                      "never his own profile: Chrome refuses a debugging port on it, and it is his")
        XCTAssertTrue(argv.contains("--remote-debugging-port=0"), "a free port, not a fixed one")
        XCTAssertTrue(argv.contains("--remote-debugging-address=127.0.0.1"))
        XCTAssertTrue(argv.contains("--no-first-run"))
        XCTAssertFalse(argv.contains { $0.hasPrefix("--headless") },
                       "he has to see it to touch the passkey prompt")
        XCTAssertEqual(argv.last, "https://accounts.google.com/")
    }

    func testThePortFileIsReadAndGarbageIsNot() {
        let port = ChromeSignInBrowser.activePort("53203\n/devtools/browser/4f2a-9c\n")
        XCTAssertEqual(port?.port, 53203)
        XCTAssertEqual(port?.path, "/devtools/browser/4f2a-9c")
        XCTAssertNil(ChromeSignInBrowser.activePort(""))
        XCTAssertNil(ChromeSignInBrowser.activePort("abc\n/devtools/browser/x"))
        XCTAssertNil(ChromeSignInBrowser.activePort("99\nhttp://evil/x"))
    }

    func testTheProfileDirectoryIsPrivate() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("signin-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: root) }
        let profile = try ChromeSignInBrowser.makeProfile(in: root)
        let mode = try FileManager.default.attributesOfItem(atPath: profile.path)[.posixPermissions] as? Int
        XCTAssertEqual(mode, 0o700)
    }

    func testTheCookieReplyIsUnwrappedAndAnErrorIsNot() throws {
        let reply = Data(#"{"id":1,"result":{"cookies":[{"name":"SID","value":"v"}]}}"#.utf8)
        let jar = try ChromeSignInBrowser.cookies(fromReply: reply)
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(with: jar) as? [[String: Any]])
        XCTAssertEqual(rows.first?["name"] as? String, "SID")
        XCTAssertThrowsError(try ChromeSignInBrowser.cookies(
            fromReply: Data(#"{"id":1,"error":{"message":"nope"}}"#.utf8)))
    }
}

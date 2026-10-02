import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"Don't have the login on the screen, only on the side? Also on iPhone I
/// don't have it at all."**
///
/// A desk's sign-in card (harbor-social needs TikTok) drew "Use my Chrome
/// login" and "Sign in fresh with passkey" only in the right-hand attention
/// pane; the card in the desk's own thread had none of it, and the phone had
/// no sign-in at all. This file pins:
///
/// 1. one ordered action list, decided in DeckKit, for the Mac and the phone;
/// 2. the desk's thread carries its own sign-in card, drawn by the SAME view
///    the side pane draws, so the two cannot drift;
/// 3. the phone's "on my Mac" buttons become `POST /v1/logins/requests` calls
///    and its card follows the request to done / failed / Mac offline;
/// 4. the Mac's handler runs the silent Chrome extraction for exactly the
///    requested site, shares it, resolves the card and reports a count only.
///
/// No Chrome is launched and no sound is made: every browser and every
/// notification here is a fake.
@MainActor
final class SignInEverywhereTests: XCTestCase {

    // MARK: fixtures

    /// `GET /v1/handoffs` for a browser sign-in card, as `_handoff_row` builds it.
    static func tiktokPage(host: String = "www.linkedin.com", desk: String = "harbor-social") throws -> HandoffsPage {
        try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1756000100,"agent":"\(desk)",
          "asked_by":"\(desk)","desk_known":true,"kind":"login",
          "needs":"Sign in to TikTok so I can post the clip",
          "state":"The clip is rendered; nothing is posted.",
          "where":"https://\(host)/login?redirect=x","evidence":"","status":"waiting",
          "options":[
            {"reply":"done","available":true,"summary":"Allow — \(desk) carries on by itself on https://\(host); no take-over needed"},
            {"reply":"skipped","available":true,"summary":"No — the desk abandons this step and reports what it can no longer finish"}],
          "always_option":{"reply":"always","available":true,
            "summary":"Allow always — \(desk) may do this (login) on https://\(host) from now on without asking"}}]}
        """.utf8))
    }

    static func item(host: String = "www.linkedin.com") throws -> AttentionItem {
        let handoff = try XCTUnwrap(tiktokPage(host: host).handoffs.first)
        return AttentionItem.make(handoff: handoff, agents: [:])
    }

    private func labels(_ actions: [SignInCardAction], _ item: AttentionItem,
                        _ surface: SignInCard.Surface) -> [String] {
        actions.map { SignInCard.label($0, item: item, surface: surface) }
    }

    // MARK: 1 — one ordered list, both devices

    func testTheMacCardOffersEverySignInActionInTheOwnersOrder() throws {
        let item = try Self.item()
        let surface = SignInCard.Surface.mac(browser: "Chrome", canSignInHere: true)
        let actions = SignInCard.actions(for: item, surface: surface, canTakeOver: true)
        XCTAssertEqual(labels(actions, item, surface), [
            "Use my Chrome login", "Sign in fresh with passkey", "Allow",
            "Allow always for this site", "Take over", "Skip this step"])
    }

    func testThePhoneOffersTheSameActionsAsRequestsToTheMac() throws {
        let item = try Self.item()
        let surface = SignInCard.Surface.phone(canAskMac: true)
        let actions = SignInCard.actions(for: item, surface: surface, canTakeOver: true)
        XCTAssertEqual(labels(actions, item, surface), [
            "Use my Mac's Chrome login", "Sign in fresh with passkey on my Mac", "Allow",
            "Allow always for this site", "Take over", "Skip this step"])
        XCTAssertEqual(actions.first, .chromeLogin)
        XCTAssertEqual(SignInCardAction.chromeLogin.method, .chrome)
        XCTAssertEqual(SignInCardAction.freshPasskey.method, .passkey)
    }

    func testGoogleLeadsWithThePasskeyOnBothDevices() throws {
        let item = try Self.item(host: "accounts.google.com")
        for surface in [SignInCard.Surface.mac(browser: "Chrome", canSignInHere: true),
                        .phone(canAskMac: true)] {
            let actions = SignInCard.actions(for: item, surface: surface, canTakeOver: false)
            // His everyday Google session never leaves the Mac (SignOutGuardTests).
            XCTAssertEqual(actions.first, .freshPasskey)
            XCTAssertFalse(actions.contains(.chromeLogin))
        }
    }

    func testANonSignInHandoffOffersNoBrowserButtons() throws {
        let page = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h2","agent":"atlas","desk_known":true,"kind":"2fa",
          "needs":"Read me the SMS code","where":"https://bank.example","status":"waiting",
          "options":[{"reply":"done","summary":"I'm done, continue — re-check"},
                     {"reply":"skipped","summary":"Skip this step — abandon"}]}]}
        """.utf8))
        let item = AttentionItem.make(handoff: page.handoffs[0], agents: [:])
        let actions = SignInCard.actions(for: item, surface: .mac(browser: "Chrome", canSignInHere: true),
                                         canTakeOver: false)
        XCTAssertFalse(actions.contains(.chromeLogin))
        XCTAssertFalse(actions.contains(.freshPasskey))
        XCTAssertEqual(SignInCard.label(actions[0], item: item, surface: .phone(canAskMac: true)),
                       "I'm done, continue", "only a browser card's done reads Allow")
    }

    // MARK: 2 — the desk's thread carries the card, drawn by the one view

    func testTheDesksThreadCarriesItsSignInCard() async throws {
        let deck = SecureHandoffTests.BlockedDeck(agents: [makeAgent("harbor-social"), makeAgent("atlas")])
        deck.handoffsPage = try Self.tiktokPage()
        let store = DeckStore(client: deck, approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "harbor-social", threadID: "direct:harbor-social")
        await store.loadApprovals()
        XCTAssertEqual(store.threadSignIns.map(\.askID), ["h1"])

        store.select(agent: "atlas", threadID: "direct:atlas")
        XCTAssertEqual(store.threadSignIns, [], "another desk's card is not drawn in this thread")
    }

    func testTheInlineCardDrawsTheSignInButtons() throws {
        let item = try Self.item()
        let card = SignInCardView(item: item, signIn: nil, canSignInOnMac: true,
                                  macBrowserLabel: "Chrome", canTakeOver: true)
        XCTAssertEqual(card.drawnLabels, [
            "Use my Chrome login", "Sign in fresh with passkey", "Allow",
            "Allow always for this site", "Take over", "Skip this step"])
        let probe = NSHostingView(rootView: card.frame(width: 320))
        probe.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(probe.fittingSize.height, 120, "the card drew its buttons")
    }

    func testTheThreadAndTheSidePaneDrawTheSameCardView() throws {
        let ui = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI")
        let thread = try String(contentsOf: ui.appendingPathComponent("ThreadView.swift"), encoding: .utf8)
        let tray = try String(contentsOf: ui.appendingPathComponent("AttentionTrayView.swift"), encoding: .utf8)
        XCTAssertTrue(thread.contains("SignInCardView("), "the thread draws the shared card")
        XCTAssertTrue(tray.contains("SignInCardView("), "the side pane draws the shared card")
        XCTAssertFalse(tray.contains("Use my \\("), "no second copy of the sign-in buttons in the tray")
    }

    // MARK: 3 — the phone asks the Mac

    func testPhoneChromeTapFilesARequestAndEndsSignedInFromTheMac() async throws {
        let item = try Self.item()
        let deck = FakeRequests()
        deck.script = [.claimed, .done(outcome: "signed_in", desks: 3)]
        var seen: [RemoteSignInPhase] = []
        let final = await PhoneSignIn(client: deck, pause: { }).run(
            item: item, method: .chrome) { seen.append($0) }

        XCTAssertEqual(deck.filed.count, 1)
        XCTAssertEqual(deck.filed.first?.desk, "harbor-social")
        XCTAssertEqual(deck.filed.first?.origin, "https://www.linkedin.com/")
        XCTAssertEqual(deck.filed.first?.method, .chrome)
        XCTAssertEqual(deck.filed.first?.handoff, "h1")
        XCTAssertEqual(final, .signedIn(node: "Studio Mac", desks: 3))
        XCTAssertEqual(final.line, "Signed in from your Mac ✓ (Studio Mac)")
        XCTAssertEqual(seen.first, .waitingForMac)
    }

    func testPhonePasskeyTapAsksTheMacToOpenTheWindow() async throws {
        let deck = FakeRequests()
        deck.script = [.done(outcome: "opened", desks: 0)]
        let final = await PhoneSignIn(client: deck, pause: { }).run(
            item: try Self.item(), method: .passkey) { _ in }
        XCTAssertEqual(deck.filed.first?.method, .passkey)
        XCTAssertEqual(final, .finishOnMac(node: "Studio Mac", host: "www.linkedin.com"))
    }

    func testAnUnclaimedExpiryIsMacOfflineAndAClaimedOneIsNot() {
        XCTAssertEqual(RemoteSignInPhase(LoginRequest.fixture(status: "expired", node: nil)), .macOffline)
        XCTAssertEqual(RemoteSignInPhase.macOffline.line, "Your Mac is offline")
        XCTAssertTrue(RemoteSignInPhase.macOffline.canRetry)
        guard case .failed = RemoteSignInPhase(LoginRequest.fixture(status: "expired", node: "Studio Mac")) else {
            return XCTFail("a Mac that claimed it and went quiet is not 'offline'")
        }
        XCTAssertEqual(RemoteSignInPhase(LoginRequest.fixture(status: "failed", node: "Studio Mac",
                                                              detail: "Not signed in here.")),
                       .failed("Not signed in here."))
    }

    func testTheHTTPClientSendsTheRequestShapeTheDeckReads() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/logins/requests"] = Data(#"""
        {"ok":true,"request":{"id":"r1","desk":"probe","origin":"https://github.com","host":"github.com",
         "method":"chrome","handoff":null,"status":"queued","outcome":null,"node":null,"desks":null,
         "detail":null,"created_at":1,"claimed_at":null,"finished_at":null,"expires_at":121}}
        """#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens, performer: performer)
        let filed = try await client.fileLoginRequest(desk: "probe", origin: "https://github.com/",
                                                      method: .chrome, handoff: nil)
        XCTAssertEqual(filed.id, "r1")
        let sent = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(sent.httpMethod, "POST")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: sent.httpBody ?? Data()) as? [String: String])
        XCTAssertEqual(body, ["desk": "probe", "origin": "https://github.com/", "method": "chrome"])

        performer.bodies["/v1/logins/requests/next"] = Data(#"{"request":null}"#.utf8)
        let none = try await client.nextLoginRequest(node: "Studio Mac", wait: 25)
        XCTAssertNil(none)
        let poll = try XCTUnwrap(performer.requests.last?.url)
        XCTAssertEqual(poll.path, "/v1/logins/requests/next")
        XCTAssertTrue(poll.query?.contains("node=Studio%20Mac") == true, poll.absoluteString)
    }

    // MARK: 4 — the Mac runs it

    func testTheMacExtractsExactlyTheRequestedSiteSharesAndResolves() async throws {
        let requests = FakeRequests()
        let extractor = SpyExtractor()
        let sharing = SpySharing()
        let resolved = SignInBox<[String]>([])
        let handler = MacLoginRequestHandler(
            requests: requests,
            importer: MacLoginImport(extractor: extractor, sharing: sharing),
            resolveHandoff: { id in resolved.value.append(id) },
            openPasskey: { _ in XCTFail("chrome must not open a window") },
            notify: { _ in XCTFail("chrome needs no UI on the Mac") })
        await handler.handle(LoginRequest.fixture(status: "claimed", node: "Studio Mac",
                                                  host: "github.com", handoff: "h1"))

        XCTAssertEqual(extractor.sites, ["github.com"])
        XCTAssertEqual(sharing.calls, 1)
        XCTAssertEqual(resolved.value, ["h1"])
        XCTAssertEqual(requests.reports.map(\.status), ["done"])
        XCTAssertEqual(requests.reports.first?.outcome, "signed_in")
        XCTAssertEqual(requests.reports.first?.desks, 3)
        XCTAssertFalse(requests.reports.first?.detail?.contains("SECRET") ?? false)
    }

    func testANotSignedInMacReportsFailedAndResolvesNothing() async throws {
        let requests = FakeRequests()
        let extractor = SpyExtractor()
        extractor.jar = Data("[]".utf8)
        let resolved = SignInBox<[String]>([])
        let handler = MacLoginRequestHandler(
            requests: requests,
            importer: MacLoginImport(extractor: extractor, sharing: SpySharing()),
            resolveHandoff: { resolved.value.append($0) },
            openPasskey: { _ in }, notify: { _ in })
        await handler.handle(LoginRequest.fixture(status: "claimed", node: "Studio Mac",
                                                  host: "github.com", handoff: "h1"))
        XCTAssertEqual(requests.reports.map(\.status), ["failed"])
        XCTAssertEqual(resolved.value, [])
    }

    func testAPasskeyRequestOpensTheWindowAndSaysSoOnTheMac() async throws {
        let requests = FakeRequests()
        let extractor = SpyExtractor()
        let opened = SignInBox<[String]>([])
        let said = SignInBox<[String]>([])
        let handler = MacLoginRequestHandler(
            requests: requests,
            importer: MacLoginImport(extractor: extractor, sharing: SpySharing()),
            resolveHandoff: { _ in },
            openPasskey: { opened.value.append($0.host) },
            notify: { said.value.append($0) })
        await handler.handle(LoginRequest.fixture(status: "claimed", node: "Studio Mac",
                                                  host: "github.com", method: .passkey, handoff: "h1"))
        XCTAssertEqual(opened.value, ["github.com"])
        XCTAssertEqual(said.value, ["Finish signing in to github.com on your Mac"])
        XCTAssertEqual(extractor.sites, [], "passkey reads no Chrome login")
        XCTAssertEqual(requests.reports.first?.outcome, "opened")
    }
}

// MARK: - doubles

final class SignInBox<T>: @unchecked Sendable {
    var value: T
    init(_ value: T) { self.value = value }
}

extension LoginRequest {
    static func fixture(status: String, node: String?, host: String = "www.tiktok.com",
                        method: LoginMethod = .chrome, handoff: String? = "h1",
                        detail: String? = nil, outcome: String? = nil, desks: Int? = nil) -> LoginRequest {
        LoginRequest(id: "r1", desk: "harbor-social", origin: "https://\(host)", host: host,
                     method: method, handoff: handoff, status: status, outcome: outcome,
                     node: node, desks: desks, detail: detail)
    }
}

/// Serves `/v1/logins/requests*` from a script, and records what was asked.
final class FakeRequests: LoginRequestClient, @unchecked Sendable {
    enum Step { case claimed, done(outcome: String, desks: Int), expired(node: String?) }
    struct Filed { let desk: String; let origin: String; let method: LoginMethod; let handoff: String? }
    struct Report { let status: String; let outcome: String?; let desks: Int; let detail: String? }
    private let lock = NSLock()
    var script: [Step] = []
    private(set) var filed: [Filed] = []
    private(set) var reports: [Report] = []
    private var last: Filed?

    private func row(_ step: Step?) -> LoginRequest {
        let f = last
        let host = URL(string: f?.origin ?? "https://x")?.host ?? "x"
        let base = (id: "r1", desk: f?.desk ?? "", origin: f?.origin ?? "", method: f?.method ?? .chrome)
        switch step {
        case nil:
            return LoginRequest(id: base.id, desk: base.desk, origin: base.origin, host: host, method: base.method,
                                handoff: f?.handoff, status: "queued", outcome: nil, node: nil, desks: nil, detail: nil)
        case .claimed?:
            return LoginRequest(id: base.id, desk: base.desk, origin: base.origin, host: host, method: base.method,
                                handoff: f?.handoff, status: "claimed", outcome: nil, node: "Studio Mac",
                                desks: nil, detail: nil)
        case .done(let outcome, let desks)?:
            return LoginRequest(id: base.id, desk: base.desk, origin: base.origin, host: host, method: base.method,
                                handoff: f?.handoff, status: "done", outcome: outcome, node: "Studio Mac",
                                desks: desks, detail: nil)
        case .expired(let node)?:
            return LoginRequest(id: base.id, desk: base.desk, origin: base.origin, host: host, method: base.method,
                                handoff: f?.handoff, status: "expired", outcome: nil, node: node,
                                desks: nil, detail: nil)
        }
    }

    func fileLoginRequest(desk: String, origin: String, method: LoginMethod,
                          handoff: String?) async throws -> LoginRequest {
        lock.withLock {
            let f = Filed(desk: desk, origin: origin, method: method, handoff: handoff)
            filed.append(f); last = f
        }
        return row(nil)
    }
    func loginRequest(id: String) async throws -> LoginRequest {
        let step: Step? = lock.withLock { script.isEmpty ? nil : script.removeFirst() }
        return row(step ?? .expired(node: nil))
    }
    func nextLoginRequest(node: String, wait: TimeInterval) async throws -> LoginRequest? { nil }
    func reportLoginRequest(id: String, status: String, outcome: String?, desks: Int,
                            detail: String?) async throws {
        lock.withLock { reports.append(Report(status: status, outcome: outcome, desks: desks, detail: detail)) }
    }
}

final class SpyExtractor: ProfileCookieExtracting, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var sites: [String] = []
    var jar = Data(#"[{"name":"user_session","value":"SECRET","domain":".github.com"}]"#.utf8)
    func extract(site host: String) async throws -> Data {
        lock.withLock { sites.append(host) }
        return jar
    }
}

final class SpySharing: LoginSharingClient, @unchecked Sendable {
    private let lock = NSLock()
    private(set) var calls = 0
    func shareLogins(cookieJSON: Data) async throws -> LoginShare {
        lock.withLock { calls += 1 }
        return LoginShare(cookies: 1, desks: 3, failed: [])
    }
}

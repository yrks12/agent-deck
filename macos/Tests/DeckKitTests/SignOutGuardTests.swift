import XCTest
@testable import DeckKit

/// "Why are things signing out, both on my Mac and on their computers?"
///
/// MEASURED 2026-10-01 (UTC): "Use my Chrome login" copied his everyday TikTok
/// session to every desk at 17:36:14; by 18:19:27 his own Mac was back on
/// tiktok.com/login. His Instagram session, copied at 23:39:25, was logged out
/// on a desk by 00:27. Google's everyday session is device-bound and never
/// worked on a desk anyway. Copying his live session for these sites signs his
/// Mac out, so the Mac refuses to, and the card does not offer it.
final class SignOutGuardTests: XCTestCase {
    final class Spy: LoginSharingClient, @unchecked Sendable {
        var sent = 0
        func shareLogins(cookieJSON: Data) async throws -> LoginShare {
            sent += 1; return LoginShare(cookies: 1, desks: 3, failed: [])
        }
    }
    struct Jar: ProfileCookieExtracting {
        func extract(site host: String) async throws -> Data {
            Data(#"[{"name":"sessionid","domain":".\#(host)"}]"#.utf8)
        }
    }

    func testHisChromeLoginIsNeverCopiedForASiteThatSignsHimOut() async {
        for host in ["www.tiktok.com", "instagram.com", "dashboard.stripe.com",
                     "accounts.google.com", "studio.youtube.com"] {
            let spy = Spy()
            do {
                _ = try await MacLoginImport(extractor: Jar(), sharing: spy).run(site: host)
                XCTFail("\(host): his live session must not leave the Mac")
            } catch {
                XCTAssertFalse(MacLoginImport.sentence(for: error).isEmpty, host)
            }
            XCTAssertEqual(spy.sent, 0, host)
        }
    }

    func testOtherSitesStillShareHisChromeLogin() async throws {
        let spy = Spy()
        _ = try await MacLoginImport(extractor: Jar(), sharing: spy).run(site: "github.com")
        XCTAssertEqual(spy.sent, 1)
    }

    func testTheRefusalSaysWhy() {
        let why = MacSignIn.chromeLoginRefusal(host: "www.tiktok.com") ?? ""
        XCTAssertTrue(why.lowercased().contains("sign"), why)
        XCTAssertNil(MacSignIn.chromeLoginRefusal(host: "github.com"))
    }

    func testTheCardDoesNotOfferHisChromeLoginForThoseSites() throws {
        let page = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"t1","agent":"atlas","desk_known":true,"kind":"login",
          "needs":"Sign in.","where":"https://www.tiktok.com/login","status":"waiting",
          "options":[{"reply":"done","summary":"I'm done, continue - it checks"}]}]}
        """.utf8))
        let item = AttentionItem.make(handoff: page.handoffs[0], agents: [:])
        for surface in [SignInCard.Surface.mac(browser: "Chrome", canSignInHere: true),
                        .phone(canAskMac: true)] {
            let actions = SignInCard.actions(for: item, surface: surface, canTakeOver: true)
            XCTAssertFalse(actions.contains(.chromeLogin), "\(surface)")
            XCTAssertFalse(actions.contains(.freshPasskey), "\(surface): the deck keeps TikTok per desk")
        }
    }

    final class ViaSpy: LoginSharingClient, @unchecked Sendable {
        var vias: [String] = []
        func shareLogins(cookieJSON: Data) async throws -> LoginShare {
            vias.append("none"); return LoginShare(cookies: 1, desks: 1, failed: [])
        }
        func shareLogins(cookieJSON: Data, via: String) async throws -> LoginShare {
            vias.append(via); return LoginShare(cookies: 1, desks: 1, failed: [])
        }
    }

    /// The vault records where each login came from; his everyday Chrome says so.
    func testHisChromeLoginIsLabelledAsComingFromHisChrome() async throws {
        let spy = ViaSpy()
        _ = try await MacLoginImport(extractor: Jar(), sharing: spy).run(site: "github.com")
        XCTAssertEqual(spy.vias, ["chrome"])
    }

    func testTheUploadBodyCarriesWhereTheLoginCameFrom() {
        let body = HTTPDeckClient.loginsBody(cookieJSON: Data("[]".utf8), via: "fresh")
        let obj = (try? JSONSerialization.jsonObject(with: body)) as? [String: Any]
        XCTAssertEqual(obj?["via"] as? String, "fresh")
        XCTAssertEqual(obj?["source"] as? String, "mac")
    }

    func testTheDecksPerDeskRefusalReadsAsASentence() {
        let s = MacSignIn.sentence(for: DeckError.http(409, reason: "per_desk_site"))
        XCTAssertTrue(s.contains("own screen"), s)
    }
}

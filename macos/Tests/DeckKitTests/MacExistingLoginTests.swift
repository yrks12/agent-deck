import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"sign in with passkey takes me to browser where I'm not logged in."**
///
/// He expects the login he already has on this Mac to carry to the desks. This
/// pins the pure core of that: which browser he uses, which of its cookies may
/// leave the Mac for a given site (only that site's, plus its sign-in domains),
/// the Safari cookie-file parser and its Full-Disk-Access wall, and the rule
/// that the throwaway profile copy is deleted no matter what.
///
/// No browser is launched here; the one part that needs Chrome is faked.
final class MacExistingLoginTests: XCTestCase {

    // MARK: which browser he uses

    func testChromeFamilyAndSafariAreRecognised() {
        XCTAssertEqual(MacBrowsers.family(forBundleID: "com.google.chrome"), .chrome)
        XCTAssertEqual(MacBrowsers.family(forBundleID: "com.brave.Browser"), .chrome)
        XCTAssertEqual(MacBrowsers.family(forBundleID: "company.thebrowser.Browser"), .chrome)
        XCTAssertEqual(MacBrowsers.family(forBundleID: "com.apple.Safari"), .safari)
        XCTAssertEqual(MacBrowsers.family(forBundleID: "org.mozilla.firefox"), .firefox)
        XCTAssertEqual(MacBrowsers.family(forBundleID: nil), .other)
        XCTAssertEqual(MacBrowsers.label(forBundleID: "com.google.chrome"), "Chrome")
        XCTAssertEqual(MacBrowsers.label(forBundleID: "company.thebrowser.browser"), "Arc")
    }

    func testChromeProfilesComeOutOfLocalStateNewestFieldsIntact() {
        let data = Data("""
        {"profile":{"info_cache":{
          "Profile 10":{"name":"Your Chrome","user_name":"sam.carter@example.com"},
          "Default":{"name":"Person 1","user_name":""},
          "Broken":{"user_name":"x@example.com"}}}}
        """.utf8)
        let profiles = ChromeProfile.all(fromLocalState: data)
        XCTAssertEqual(profiles.map(\.dir), ["Default", "Profile 10"], "sorted, unnamed dropped")
        let ten = try? XCTUnwrap(profiles.first { $0.dir == "Profile 10" })
        XCTAssertEqual(ten?.name, "Your Chrome")
        XCTAssertEqual(ten?.email, "sam.carter@example.com")
        XCTAssertNil(profiles.first { $0.dir == "Default" }?.email, "an empty email is nil")
    }

    // MARK: which cookies may leave the Mac

    func testRegistrableDomainFoldsSubdomainsAndKnowsTwoLabelTLDs() {
        XCTAssertEqual(SiteCookies.registrableDomain("accounts.google.com"), "google.com")
        XCTAssertEqual(SiteCookies.registrableDomain(".www.google.com"), "google.com")
        XCTAssertEqual(SiteCookies.registrableDomain("foo.bar.bbc.co.uk"), "bbc.co.uk")
    }

    func testGoogleAuthCookiesTravelAndUnrelatedOnesDoNot() {
        // The case the owner hit: signing into Google keeps the session on
        // accounts.google.com — which must travel — while youtube must not.
        XCTAssertTrue(SiteCookies.belongs(cookieDomain: ".google.com", toSite: "accounts.google.com"))
        XCTAssertTrue(SiteCookies.belongs(cookieDomain: "accounts.google.com", toSite: "www.google.com"))
        XCTAssertTrue(SiteCookies.belongs(cookieDomain: ".mail.google.com", toSite: "google.com"))
        XCTAssertFalse(SiteCookies.belongs(cookieDomain: ".youtube.com", toSite: "google.com"))
        XCTAssertFalse(SiteCookies.belongs(cookieDomain: ".notgoogle.com", toSite: "google.com"),
                       "a suffix that is not a subdomain must not match")
        XCTAssertTrue(SiteCookies.belongs(cookieDomain: ".instagram.com", toSite: "instagram.com"))
    }

    func testCrossDomainSignInHostsTravelForTheirSite() {
        XCTAssertTrue(SiteCookies.belongs(cookieDomain: "login.microsoftonline.com",
                                          toSite: "admin.microsoft.com"))
        XCTAssertFalse(SiteCookies.belongs(cookieDomain: "login.microsoftonline.com",
                                           toSite: "google.com"))
    }

    func testFilterKeepsOnlyTheRequestedSitesCookies() throws {
        let jar = Data("""
        [{"name":"SID","value":"a","domain":".google.com"},
         {"name":"AUTH","value":"b","domain":"accounts.google.com"},
         {"name":"VISITOR","value":"c","domain":".youtube.com"},
         {"name":"junk","value":"d","domain":"evil.example"}]
        """.utf8)
        let kept = try SiteCookies.filterJSON(jar, toSite: "google.com")
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(with: kept) as? [[String: Any]])
        XCTAssertEqual(Set(rows.compactMap { $0["name"] as? String }), ["SID", "AUTH"])
        XCTAssertEqual(SiteCookies.countJSON(jar, toSite: "google.com"), 2)
    }

    // MARK: Safari's binarycookies, and the Full-Disk-Access wall

    /// Builds one real-format `Cookies.binarycookies` page with the given cookies.
    private func binaryCookies(_ cookies: [(domain: String, name: String, path: String,
                                            value: String, secure: Bool, expiry: Double)]) -> Data {
        func le32(_ v: Int) -> [UInt8] { (0..<4).map { UInt8((v >> (8*$0)) & 0xFF) } }
        func be32(_ v: Int) -> [UInt8] { (0..<4).reversed().map { UInt8((v >> (8*$0)) & 0xFF) } }
        func leDouble(_ d: Double) -> [UInt8] {
            let bits = d.bitPattern; return (0..<8).map { UInt8((bits >> (8*$0)) & 0xFF) }
        }
        func record(_ c: (domain: String, name: String, path: String, value: String,
                          secure: Bool, expiry: Double)) -> [UInt8] {
            let stringsStart = 56
            var strings: [UInt8] = []
            func put(_ s: String) -> Int {
                let off = stringsStart + strings.count
                strings += Array(s.utf8) + [0]; return off
            }
            let dOff = put(c.domain), nOff = put(c.name), pOff = put(c.path), vOff = put(c.value)
            let size = stringsStart + strings.count
            var r = [UInt8](repeating: 0, count: 56)
            r.replaceSubrange(0..<4, with: le32(size))
            r.replaceSubrange(8..<12, with: le32(c.secure ? 0x1 : 0x0))
            r.replaceSubrange(16..<20, with: le32(dOff))
            r.replaceSubrange(20..<24, with: le32(nOff))
            r.replaceSubrange(24..<28, with: le32(pOff))
            r.replaceSubrange(28..<32, with: le32(vOff))
            r.replaceSubrange(40..<48, with: leDouble(c.expiry))
            return r + strings
        }
        var records = cookies.map(record)
        let headerLen = 8 + records.count * 4 + 4       // 0x0100 + count + offsets + footer
        var offset = headerLen
        var offsets: [Int] = []
        for r in records { offsets.append(offset); offset += r.count }
        var page: [UInt8] = [0x00, 0x00, 0x01, 0x00] + le32(records.count)
        for o in offsets { page += le32(o) }
        page += le32(0)                                  // page footer
        for r in records { page += r }

        var out: [UInt8] = Array("cook".utf8) + be32(1) + be32(page.count) + page
        out += be32(0) + be32(0)                         // checksum-ish footer
        return Data(out)
    }

    func testTheBinaryCookiesParserReadsAFixtureBackOut() throws {
        let expiry = 800_000_000.0   // mac absolute time
        let data = binaryCookies([
            (".github.com", "user_session", "/", "abc123", true, expiry),
            (".github.com", "logged_in", "/", "yes", false, 0),   // session cookie
        ])
        let rows = try SafariCookies.parse(data)
        XCTAssertEqual(rows.count, 2)
        let session = try XCTUnwrap(rows.first { $0["name"] as? String == "user_session" })
        XCTAssertEqual(session["value"] as? String, "abc123")
        XCTAssertEqual(session["domain"] as? String, ".github.com")
        XCTAssertEqual(session["secure"] as? Bool, true)
        XCTAssertEqual(try XCTUnwrap(session["expires"] as? Double), expiry + 978_307_200.0, accuracy: 1)
        let li = try XCTUnwrap(rows.first { $0["name"] as? String == "logged_in" })
        XCTAssertEqual(li["session"] as? Bool, true)
        XCTAssertNil(li["expires"], "a session cookie has no expiry")
    }

    func testGarbageIsNotMistakenForCookies() {
        XCTAssertThrowsError(try SafariCookies.parse(Data("not a cookie file".utf8))) {
            XCTAssertEqual($0 as? SafariCookiesError, .notBinaryCookies)
        }
    }

    func testFullDiskAccessMissingHasAMessageAndTheSettingsPane() throws {
        XCTAssertTrue(SafariAccess.missingMessage.lowercased().contains("full disk access"))
        XCTAssertEqual(SafariAccess.fullDiskAccessURL.scheme, "x-apple.systempreferences")
        XCTAssertTrue(SafariAccess.fullDiskAccessURL.absoluteString.contains("Privacy_AllFiles"))
        XCTAssertTrue(MacLoginImport.sentence(for: MacLoginImportError.fullDiskAccessNeeded)
            .lowercased().contains("full disk access"))
    }

    func testSafariExtractorMapsAnUnreadableFileToTheFDAWall() async {
        let extractor = SafariProfileExtractor(
            cookieFile: URL(fileURLWithPath: "/does/not/exist/Cookies.binarycookies"))
        do {
            _ = try await extractor.extract(site: "github.com")
            XCTFail("a missing file should read as the FDA wall")
        } catch {
            XCTAssertEqual(error as? MacLoginImportError, .fullDiskAccessNeeded)
        }
    }

    // MARK: the throwaway profile copy is deleted no matter what

    final class SpyReader: ProfileCookieReader, @unchecked Sendable {
        let cookies: Data
        let throwAfterSeeing: Bool
        private(set) var sawRoot: URL?
        private(set) var sawCookieCopy = false
        init(cookies: Data, throwAfterSeeing: Bool = false) {
            self.cookies = cookies; self.throwAfterSeeing = throwAfterSeeing
        }
        func readCookies(profileRoot: URL) async throws -> Data {
            sawRoot = profileRoot
            sawCookieCopy = FileManager.default.fileExists(
                atPath: profileRoot.appendingPathComponent("Default/Cookies").path)
            if throwAfterSeeing { throw MacSignInError.cdp }
            return cookies
        }
    }

    private func sourceProfile() throws -> (profile: URL, localState: URL, temp: URL) {
        let base = FileManager.default.temporaryDirectory
            .appendingPathComponent("uld-src-\(UUID().uuidString)")
        let profile = base.appendingPathComponent("Profile 10")
        try FileManager.default.createDirectory(at: profile, withIntermediateDirectories: true)
        try Data("encrypted-cookies".utf8).write(to: profile.appendingPathComponent("Cookies"))
        let localState = base.appendingPathComponent("Local State")
        try Data("{}".utf8).write(to: localState)
        return (profile, localState, base.appendingPathComponent("tmp"))
    }

    func testTheCopyIsMadeReadAndDeletedOnSuccess() async throws {
        let (profile, localState, temp) = try sourceProfile()
        defer { try? FileManager.default.removeItem(at: profile.deletingLastPathComponent()) }
        let reader = SpyReader(cookies: Data("""
        [{"name":"SID","value":"x","domain":".github.com"},
         {"name":"other","value":"y","domain":".evil.example"}]
        """.utf8))
        let ex = ChromeProfileExtractor(sourceProfile: profile, localState: localState,
                                        reader: reader, tempParent: temp)
        let out = try await ex.extract(site: "github.com")

        XCTAssertTrue(reader.sawCookieCopy, "Chrome must be pointed at a copy that has the cookies")
        let rows = try XCTUnwrap(JSONSerialization.jsonObject(with: out) as? [[String: Any]])
        XCTAssertEqual(rows.map { $0["name"] as? String }, ["SID"], "only the site's cookies leave")
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: temp.path)
        XCTAssertEqual(leftovers, [], "the copy was left on disk")
    }

    func testTheCopyIsDeletedEvenWhenReadingThrows() async throws {
        let (profile, localState, temp) = try sourceProfile()
        defer { try? FileManager.default.removeItem(at: profile.deletingLastPathComponent()) }
        let reader = SpyReader(cookies: Data("[]".utf8), throwAfterSeeing: true)
        let ex = ChromeProfileExtractor(sourceProfile: profile, localState: localState,
                                        reader: reader, tempParent: temp)
        do {
            _ = try await ex.extract(site: "github.com")
            XCTFail("the reader threw; extract must rethrow")
        } catch {}
        XCTAssertTrue(reader.sawRoot != nil)
        let leftovers = (try? FileManager.default.contentsOfDirectory(atPath: temp.path)) ?? []
        XCTAssertEqual(leftovers, [], "a failed read still deletes the signed-in copy")
    }

    // MARK: extract then share

    final class FakeSharing: LoginSharingClient, @unchecked Sendable {
        var sent: [Data] = []
        func shareLogins(cookieJSON: Data) async throws -> LoginShare {
            sent.append(cookieJSON); return LoginShare(cookies: 1, desks: 4, failed: [])
        }
    }
    struct StubExtractor: ProfileCookieExtracting {
        let data: Data
        func extract(site host: String) async throws -> Data { data }
    }

    func testImportSharesTheFilteredCookiesAndReportsDesks() async throws {
        let sharing = FakeSharing()
        let imp = MacLoginImport(
            extractor: StubExtractor(data: Data(#"[{"name":"SID","domain":".github.com"}]"#.utf8)),
            sharing: sharing)
        let share = try await imp.run(site: "github.com")
        XCTAssertEqual(share.desks, 4)
        XCTAssertEqual(sharing.sent.count, 1)
    }

    func testNotSignedInHereIsAKnownFailureNotAShare() async {
        let sharing = FakeSharing()
        let imp = MacLoginImport(extractor: StubExtractor(data: Data("[]".utf8)), sharing: sharing)
        do {
            _ = try await imp.run(site: "github.com")
            XCTFail("an empty jar must not be shared")
        } catch {
            XCTAssertEqual(error as? MacLoginImportError, .notLoggedInHere)
        }
        XCTAssertTrue(sharing.sent.isEmpty, "nothing left the Mac")
        XCTAssertTrue(MacLoginImport.sentence(for: MacLoginImportError.notLoggedInHere)
            .lowercased().contains("passkey"))
    }

    // MARK: the store — primary action

    private func loginCard() throws -> ScriptedDeckClient {
        let client = ScriptedDeckClient()
        client.handoffsPage = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1,"agent":"acme","asked_by":"acme","desk_known":true,
          "kind":"login","needs":"Sign in.","state":"stopped","where":"https://github.com/login",
          "status":"waiting","options":[
            {"reply":"done","available":true,"summary":"I'm done, continue - it checks"}]}]}
        """.utf8))
        return client
    }

    @MainActor
    func testUsingHisMacLoginSharesItAndAnswersTheCardDone() async throws {
        let client = try loginCard()
        let sharing = FakeSharing()
        let deck = DeckStore(
            client: client, approvalPollInterval: 600,
            macLogin: MacLoginImport(
                extractor: StubExtractor(data: Data(#"[{"name":"user_session","domain":".github.com"}]"#.utf8)),
                sharing: sharing),
            macBrowserLabel: "Chrome")
        await deck.loadApprovals()
        let item = try XCTUnwrap(deck.attention.first)

        await deck.useMacLogin(item)

        XCTAssertEqual(deck.macSignIns["h1"], .shared(desks: 4))
        XCTAssertEqual(client.resolvedHandoffs.map(\.0), ["h1"])
        XCTAssertEqual(client.resolvedHandoffs.map(\.1), ["done"])
        XCTAssertEqual(sharing.sent.count, 1)
    }

    @MainActor
    func testNotSignedInHereOffersThePasskeyFallbackAndDoesNotAnswer() async throws {
        let client = try loginCard()
        let sharing = FakeSharing()
        let deck = DeckStore(
            client: client, approvalPollInterval: 600,
            macLogin: MacLoginImport(extractor: StubExtractor(data: Data("[]".utf8)),
                                     sharing: sharing),
            macBrowserLabel: "Chrome")
        await deck.loadApprovals()
        let item = try XCTUnwrap(deck.attention.first)

        await deck.useMacLogin(item)

        guard case .offerPasskeyInstead(let why) = deck.macSignIns["h1"] else {
            return XCTFail("\(String(describing: deck.macSignIns["h1"]))")
        }
        XCTAssertTrue(why.lowercased().contains("passkey"))
        XCTAssertTrue(client.resolvedHandoffs.isEmpty, "nothing carried over, so nothing is done")
        XCTAssertTrue(sharing.sent.isEmpty)
    }
}

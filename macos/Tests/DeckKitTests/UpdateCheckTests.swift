import XCTest
@testable import DeckKit

/// K8, app side: a quiet "a new version exists" banner, and a blocking one when
/// the server has moved past this app.
final class UpdateCheckTests: XCTestCase {

    private let manifest = #"""
    {"version":"0.9.1","released":"2026-10-05",
     "server":{"url":"https://r/x.tar.gz","sha256":"aa"},
     "app":{"url":"https://r/AgentDeck-0.9.1.dmg","sha256":"bb","min_macos":"14.0"},
     "min_app":"0.9.0","min_server":"0.9.0","notes":"Fixes the sidebar."}
    """#

    func testVersionsCompareNumericallyNotAsText() {
        XCTAssertTrue(AppVersion("0.10.0")! > AppVersion("0.9.9")!)
        XCTAssertTrue(AppVersion("1.0")! == AppVersion("1.0.0")!)
        XCTAssertTrue(AppVersion("0.9.1")! > AppVersion("0.9.0")!)
        XCTAssertNil(AppVersion("banana"))
        XCTAssertNil(AppVersion(""))
    }

    func testANewerManifestOffersTheDMGWithoutBlocking() throws {
        let m = try UpdateCheck.decodeManifest(Data(manifest.utf8))
        let notice = UpdateCheck.notice(manifest: m, appVersion: "0.9.0")
        XCTAssertEqual(notice?.version, "0.9.1")
        XCTAssertEqual(notice?.downloadURL.absoluteString, "https://r/AgentDeck-0.9.1.dmg")
        XCTAssertEqual(notice?.headline, "Agent Deck 0.9.1 is available")
    }

    func testTheSameOrAnOlderManifestSaysNothing() throws {
        let m = try UpdateCheck.decodeManifest(Data(manifest.utf8))
        XCTAssertNil(UpdateCheck.notice(manifest: m, appVersion: "0.9.1"))
        XCTAssertNil(UpdateCheck.notice(manifest: m, appVersion: "1.2.0"))
    }

    func testAnUnreadableAppVersionNeverNags() throws {
        let m = try UpdateCheck.decodeManifest(Data(manifest.utf8))
        XCTAssertNil(UpdateCheck.notice(manifest: m, appVersion: "dev"))
    }

    func testAServerThatMovedPastThisAppBlocks() {
        let v = VersionInfo(version: "0.9.3", minApp: "0.9.2", api: 1)
        XCTAssertTrue(UpdateCheck.appIsTooOld(server: v, appVersion: "0.9.1"))
        XCTAssertFalse(UpdateCheck.appIsTooOld(server: v, appVersion: "0.9.2"))
        XCTAssertFalse(UpdateCheck.appIsTooOld(server: v, appVersion: "dev"))
        XCTAssertEqual(UpdateCheck.tooOldText, "Your server was updated; update this app to keep working.")
    }

    func testVersionResponseDecodes() throws {
        let v = try UpdateCheck.decodeVersion(Data(#"{"version":"0.9.0","min_app":"0.9.0","api":1}"#.utf8))
        XCTAssertEqual(v, VersionInfo(version: "0.9.0", minApp: "0.9.0", api: 1))
    }

    func testTheCheckRunsOnLaunchAndThenOncePerDay() {
        let now = Date(timeIntervalSince1970: 1_000_000)
        XCTAssertTrue(UpdateCheck.isDue(lastChecked: nil, now: now))
        XCTAssertFalse(UpdateCheck.isDue(lastChecked: now.addingTimeInterval(-3600), now: now))
        XCTAssertTrue(UpdateCheck.isDue(lastChecked: now.addingTimeInterval(-86_400), now: now))
    }

    /// No auth, no identifiers: a plain GET of the manifest and nothing else.
    func testFetchSendsAnAnonymousGet() async throws {
        let spy = SpyPerformer { _ in (200, Data(self.manifest.utf8), [:]) }
        let m = try await UpdateCheck.fetchManifest(from: URL(string: "https://r/manifest.json")!, performer: spy)
        XCTAssertEqual(m.version, "0.9.1")
        let req = try XCTUnwrap(spy.requests.first)
        XCTAssertEqual(req.httpMethod, "GET")
        XCTAssertNil(req.value(forHTTPHeaderField: "Authorization"))
        XCTAssertNil(req.httpBody)
        XCTAssertEqual(req.allHTTPHeaderFields?.keys.sorted() ?? [], [])
    }

    func testAFailedFetchThrowsAndIsNotAnUpdate() async {
        let spy = SpyPerformer { _ in (503, Data(), [:]) }
        do {
            _ = try await UpdateCheck.fetchManifest(from: URL(string: "https://r/manifest.json")!, performer: spy)
            XCTFail("a 503 is not a manifest")
        } catch {}
    }
}

/// Records requests and answers from a closure. Shared by the connection tests.
final class SpyPerformer: RequestPerformer, @unchecked Sendable {
    private let lock = NSLock()
    private var _requests: [URLRequest] = []
    private let answer: @Sendable (URLRequest) -> (Int, Data, [String: String])

    init(_ answer: @escaping @Sendable (URLRequest) -> (Int, Data, [String: String])) { self.answer = answer }

    var requests: [URLRequest] { lock.lock(); defer { lock.unlock() }; return _requests }

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        lock.lock(); _requests.append(request); lock.unlock()
        let (status, body, headers) = answer(request)
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: "HTTP/1.1", headerFields: headers)!
        return (body, response)
    }
}

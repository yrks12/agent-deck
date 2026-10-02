import XCTest
@testable import DeckKit
@testable import DeckUI

/// **The Connect wait, end to end on this Mac.** A scripted deck, a real
/// loopback socket, and a "browser" that is a URLSession request — so the
/// order (port held, then browser opened), each way of finishing, and the
/// paste check are pinned without a provider.
@MainActor
final class StoreConnectModelTests: XCTestCase {

    final class ScriptedStore: StoreClient, @unchecked Sendable {
        var port: UInt16 = 0
        var status = "pending"
        var expiresAt = "2099-01-01T00:00:00Z"
        private(set) var completed: [String] = []
        private let lock = NSLock()

        func storeConnect(id: String, desks: StoreDesks) async throws -> StoreConnectStart {
            StoreConnectStart(authorizeURL: URL(string: "https://provider.example/authorize")!,
                              state: "st", redirectURI: "http://127.0.0.1:\(port)/callback",
                              expiresAt: expiresAt)
        }
        func storeConnectComplete(callbackURL: String) async throws -> StoreInstallResult {
            lock.lock(); completed.append(callbackURL); lock.unlock()
            let item = FixtureDeckClient.storeItems[0]
            return StoreInstallResult(item: item, installed: [], reload: [
                StoreReload(desk: "atlas", action: "after_turn", detail: "reloads after its current turn")])
        }
        func storeConnectStatus(state: String) async throws -> StoreConnectStatus {
            StoreConnectStatus(state: status, id: "x", detail: "declined at the provider")
        }
        func storeCatalog(kind: String?, query: String?, trust: StoreTrustScope, limit: Int, offset: Int) async throws -> StoreCatalogPage { StoreCatalogPage(items: [], total: 0) }
        func storeItem(id: String) async throws -> StoreItem { throw DeckError.storeRefused(.unknownItem, detail: "") }
        func storeInstall(_ request: StoreInstallRequest) async throws -> StoreInstallResult { throw DeckError.storeRefused(.needsOAuth, detail: "") }
        func storeUpdate(id: String, desks: StoreDesks) async throws -> StoreInstallResult { throw DeckError.storeRefused(.unknownItem, detail: "") }
        func storeUninstall(id: String, desks: StoreDesks) async throws -> StoreUninstallResult { StoreUninstallResult(removed: [], reload: []) }
        func storeInstalled(desk: String?) async throws -> StoreInstalledPage { StoreInstalledPage(desks: []) }
        func storeRefresh() async throws -> Bool { true }
    }

    private func waitFor(_ what: String, _ condition: () -> Bool) async throws {
        for _ in 0..<200 where !condition() { try await Task.sleep(nanoseconds: 25_000_000) }
        XCTAssertTrue(condition(), "timed out waiting for \(what)")
    }

    override func setUp() async throws {
        StoreConnectModel.pollInterval = 50_000_000
    }

    func testThePortIsHeldBeforeTheBrowserOpensAndTheCallbackInstalls() async throws {
        let deck = ScriptedStore()
        deck.port = UInt16.random(in: 49_200...60_000)
        var opened: [URL] = []
        var portWasHeld = false
        var installed = 0
        let model = StoreConnectModel(client: deck, openURL: { url in
            opened.append(url)
            // The browser may come straight back; the port must already be open.
            let probe = OAuthLoopbackListener(port: deck.port)
            Task { do { try await probe.start(); probe.cancel() } catch { portWasHeld = true } }
        }, onInstalled: { installed += 1 })

        await model.connect(id: "mcp:com.notion/mcp", desks: .all)
        XCTAssertEqual(model.stage, .waiting(listening: true))
        XCTAssertEqual(opened.map(\.host), ["provider.example"])
        try await waitFor("the probe") { portWasHeld }

        _ = try await URLSession.shared.data(
            from: URL(string: "http://127.0.0.1:\(deck.port)/callback?code=c&state=st")!)

        try await waitFor("done") { if case .done = model.stage { return true } else { return false } }
        XCTAssertEqual(deck.completed, ["http://127.0.0.1:\(deck.port)/callback?code=c&state=st"])
        guard case .done(let result?) = model.stage else { return XCTFail("\(model.stage)") }
        XCTAssertEqual(result.reload.map(StoreBrowser.reloadLine), ["atlas — reloads after its current turn"])
        XCTAssertEqual(installed, 1)
    }

    func testAPastedAddressFinishesItAndNonsenseIsRefusedWithoutACall() async throws {
        let deck = ScriptedStore()
        deck.port = UInt16.random(in: 49_200...60_000)
        let model = StoreConnectModel(client: deck, openURL: { _ in }, onInstalled: {})
        await model.connect(id: "x", desks: .desks(["atlas"]))

        model.pasted = "not an address"
        await model.submitPasted()
        XCTAssertNotNil(model.pasteProblem)
        XCTAssertEqual(deck.completed, [])

        model.pasted = " http://127.0.0.1:\(deck.port)/callback?code=p&state=st "
        await model.submitPasted()
        XCTAssertEqual(deck.completed, ["http://127.0.0.1:\(deck.port)/callback?code=p&state=st"])
        XCTAssertEqual(model.pasted, "", "the pasted code is not kept")
        guard case .done = model.stage else { return XCTFail("\(model.stage)") }
    }

    func testTheStatusPollSeesASignInFinishedElsewhere() async throws {
        let deck = ScriptedStore()
        deck.port = UInt16.random(in: 49_200...60_000)
        deck.status = "done"
        let model = StoreConnectModel(client: deck, openURL: { _ in }, onInstalled: {})
        await model.connect(id: "x", desks: .all)

        try await waitFor("done via status") { model.stage == .done(nil) }
        XCTAssertEqual(deck.completed, [])
    }

    func testAFailedStatusIsShownInTheDecksWords() async throws {
        let deck = ScriptedStore()
        deck.port = UInt16.random(in: 49_200...60_000)
        deck.status = "failed"
        let model = StoreConnectModel(client: deck, openURL: { _ in }, onInstalled: {})
        await model.connect(id: "x", desks: .all)

        try await waitFor("failed") { model.stage == .failed("declined at the provider") }
    }

    func testTheWaitEndsAtExpiresAt() async throws {
        let deck = ScriptedStore()
        deck.port = UInt16.random(in: 49_200...60_000)
        let formatter = ISO8601DateFormatter()
        deck.expiresAt = formatter.string(from: Date().addingTimeInterval(1))
        let model = StoreConnectModel(client: deck, openURL: { _ in }, onInstalled: {})
        await model.connect(id: "x", desks: .all)

        try await waitFor("expired") { model.stage == .expired }
    }
}

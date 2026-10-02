import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// The Connectors & Skills panel, drawn from the fixture store and read back
/// off the pixels. Written to `$DECK_STORE_CAPTURE_DIR` (or the temporary
/// directory) — never into the repository.
@MainActor
final class StoreCaptureTests: XCTestCase {

    private let size = CGSize(width: 720, height: 680)

    private func render<V: View>(_ view: V, named name: String, settle: TimeInterval = 1.5)
        async throws -> [String] {
        let (host, window) = RightPaneFixture.host(view.frame(width: size.width, height: size.height),
                                                  size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: UInt64(settle * 1_000_000_000))
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        let dir = URL(fileURLWithPath: ProcessInfo.processInfo.environment["DECK_STORE_CAPTURE_DIR"]
                      ?? NSTemporaryDirectory())
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try RightPaneFixture.png(rep)?.write(to: dir.appendingPathComponent(name))
        return RightPaneFixture.readText(rep).map(\.text)
    }

    private func model() -> StorePanelModel {
        StorePanelModel(client: FixtureDeckClient(), desks: ["chief", "hemingway", "seeker"])
    }

    func testTheConnectorsTabDrawsCardsWithHonestBadges() async throws {
        let text = try await render(StorePanelView(model: model()), named: "store-connectors.png")
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("Connectors & Skills"), text)
        XCTAssertTrue(text.contains("Microsoft Learn"), text)
        XCTAssertTrue(text.contains("Official"), text)
        XCTAssertFalse(text.contains("postgres-tools"), "unverified is hidden by default: \(text)")
        XCTAssertTrue(text.contains("Other sources"), text)
    }

    func testOtherSourcesShowsTheWarningAndTheUnverifiedItem() async throws {
        let model = model()
        model.browser.showsUnverified = true
        let text = try await render(StorePanelView(model: model), named: "store-other-sources.png")
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("postgres-tools"), text)
        XCTAssertTrue(text.contains("Unverified source"), text)
    }

    func testTheDetailOfAKeyedConnectorAsksForTheKey() async throws {
        let github = try XCTUnwrap(FixtureDeckClient.storeItems.first { $0.auth == "api_key" })
        let text = try await render(StorePanelView(model: model(), selected: github),
                                    named: "store-detail-api-key.png").joined(separator: " | ")

        XCTAssertTrue(text.contains("All desks"), text)
        XCTAssertTrue(text.contains("Personal access token"), text)
        XCTAssertTrue(text.contains("Install"), text)
    }

    func testAnUnverifiedDetailWarnsAndShowsTheReloadLinesAfterInstall() async throws {
        let item = try XCTUnwrap(FixtureDeckClient.storeItems.first { $0.trust == "unverified" })
        let result = StoreInstallResult(item: item, installed: [], reload: [
            StoreReload(desk: "chief", action: "after_turn", detail: "reloads after its current turn"),
            StoreReload(desk: "hemingway", action: "next_wake", detail: "picks it up on its next wake"),
        ])
        let view = StoreItemDetailView(item: item, model: model(), onBack: {}, result: result)
        let text = try await render(view, named: "store-detail-unverified.png", settle: 0.6)
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("Unverified source"), text)
        XCTAssertTrue(text.contains("install it anyway"), text)
        XCTAssertTrue(text.contains("chief — reloads after its current turn")
                      || text.contains("chief - reloads after its current turn"), text)
    }

    func testTheInstalledTabGroupsByDesk() async throws {
        let model = model()
        model.browser.tab = .installed
        let text = try await render(StorePanelView(model: model), named: "store-installed.png")
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("chief"), text)
        XCTAssertTrue(text.contains("Update"), text)
        XCTAssertTrue(text.contains("Remove"), text)
        XCTAssertTrue(text.contains("9f2a15f"), text)
    }

    // MARK: plugins and Connect

    func testThePluginsTabShowsOfficialAndVerifiedPlugins() async throws {
        let model = model()
        model.browser.tab = .plugins
        let text = try await render(StorePanelView(model: model), named: "store-plugins.png")
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("Plugins"), text)
        XCTAssertTrue(text.contains("Code review"), text)
        XCTAssertTrue(text.contains("Figma"), text)
        XCTAssertTrue(text.contains("Verified"), text)
        XCTAssertTrue(text.contains("Official"), text)
    }

    func testAPluginsDetailLeadsWithItsTrustNote() async throws {
        let figma = try XCTUnwrap(FixtureDeckClient.storeItems.first { $0.kind == "plugin" && $0.trust == "verified" })
        let text = try await render(StorePanelView(model: model(), selected: figma),
                                    named: "store-detail-plugin.png").joined(separator: " ")

        XCTAssertTrue(text.contains("hooks and MCP servers"), text)
        XCTAssertTrue(text.contains("All plugins"), text)
    }

    func testAnOAuthConnectorOffersConnectWithTheDeskPicker() async throws {
        let notion = try XCTUnwrap(FixtureDeckClient.storeItems.first { $0.auth == "oauth" })
        let view = StoreItemDetailView(item: notion, model: model(), onBack: {}, openURL: { _ in })
        let text = try await render(view, named: "store-detail-oauth.png", settle: 0.6)
            .joined(separator: " | ")

        XCTAssertTrue(text.contains("Connect"), text)
        XCTAssertTrue(text.contains("All desks"), text)
        XCTAssertFalse(text.contains("isn't supported"), text)
        XCTAssertFalse(text.contains("Install |"), "Connect replaces Install: \(text)")
    }
}

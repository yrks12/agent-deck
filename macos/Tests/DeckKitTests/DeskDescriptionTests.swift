import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **Every desk says what it is for** — `description` on the wire.
///
/// The owner asked why none of his agents has a description in the apps
/// (2026-10-01). The rows carried a name, a two-word chip and the last message;
/// nothing said what the desk is FOR. The deck now sends `description`: one or
/// two plain sentences, `""` when nobody wrote one (`client-api.md` §3).
///
/// Here: the row decodes it, the settings panel saves it, the fixture deck the
/// videos are captured from carries it, and the conversation header shows it
/// without changing height from one desk to the next.
final class DeskDescriptionTests: XCTestCase {

    private let said = "Runs the shop's Instagram: posts, replies and the nightly round."

    func testTheRowDecodesTheDescription() throws {
        let row = try DeckCoding.decoder.decode(Agent.self, from: Data("""
        {"name":"shop-social","label":"Shop Social","description":"\(said)",
         "state":"IDLE","unread":0,"thread_id":"direct:shop-social"}
        """.utf8))
        XCTAssertEqual(row.summary, said)
    }

    func testADeckThatSendsNoneDecodesToNothing() throws {
        let row = try DeckCoding.decoder.decode(Agent.self, from: Data("""
        {"name":"atlas","label":"COS","state":"IDLE"}
        """.utf8))
        XCTAssertEqual(row.summary, "")
        XCTAssertNil(row.summaryLine, "an empty description must draw nothing, not a blank claim")
    }

    func testTheDescriptionIsNeverTheCharter() throws {
        let row = try DeckCoding.decoder.decode(Agent.self, from: Data("""
        {"name":"atlas","label":"COS","charter":"You own everything.","state":"IDLE"}
        """.utf8))
        XCTAssertEqual(row.summary, "", "the charter is the desk's brief, written to the desk")
    }

    func testThePatchCarriesTheDescription() throws {
        var agent = makeAgent("chief")
        agent.summary = said
        let body = try XCTUnwrap(JSONSerialization.jsonObject(
            with: try AgentPatch(agent).encoded()) as? [String: Any])
        XCTAssertEqual(body["description"] as? String, said)
    }

    /// The panel is built from the sidebar row, and the row carries no
    /// charter. Sending that `""` back would wipe the desk's whole brief (the
    /// deck stores `charter` and `mission` from it) the first time he edits
    /// the line under its name.
    func testSavingFromARowWithNoCharterNeverSendsAnEmptyCharter() throws {
        var agent = makeAgent("chief")
        agent.detail = ""
        agent.summary = said
        let body = try XCTUnwrap(JSONSerialization.jsonObject(
            with: try AgentPatch(agent).encoded()) as? [String: Any])
        XCTAssertNil(body["charter"], "an empty charter would erase the desk's brief")
    }

    func testTheProfilePanelSavesTheDescription() async throws {
        let client = ScriptedDeckClient()
        let panel = AgentSettingsModel(agent: makeAgent("Chief"), client: client)

        await panel.save(name: "Chief", title: "COS", detail: "", summary: said)

        XCTAssertEqual(client.calls, [.updateAgent("Chief")])
        let saved = await panel.agent
        XCTAssertEqual(saved.summary, said)
    }

    func testEveryFixtureDeskSaysWhatItIsFor() {
        let empty = FixtureDeckClient().agents.filter { $0.summary.isEmpty }.map(\.name)
        XCTAssertEqual(empty, [], "the videos are captured from the fixture deck")
    }

    func testTheHeaderIsOneHeightWithOrWithoutADescription() {
        let with = fittingHeight(ThreadHeader(title: "Chief", subtitle: "COS", connection: .live,
                                              summary: said))
        let without = fittingHeight(ThreadHeader(title: "Chief", subtitle: "COS", connection: .live,
                                                 summary: nil))
        XCTAssertGreaterThan(with, 1, "the header did not lay out")
        XCTAssertEqual(with, without,
                       "switching between a desk with a description and one without "
                       + "would re-measure the transcript")
    }

    func testTheHeaderSaysTheDescription() {
        let header = ThreadHeader(title: "Chief", subtitle: "COS", connection: .live, summary: said)
        XCTAssertTrue(header.spokenText.contains(said))
    }

    private func fittingHeight<V: View>(_ view: V) -> CGFloat {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: view.frame(width: 700))
        probe.frame = NSRect(x: 0, y: 0, width: 700, height: 200)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 700, height: 200),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        let height = probe.fittingSize.height
        window.orderOut(nil)
        window.contentView = nil
        return height
    }
}

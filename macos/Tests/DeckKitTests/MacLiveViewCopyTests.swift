import XCTest
@testable import DeckKit

/// **His Mac's live view speaks about a Mac, never about a desk's computer.**
///
/// Owner, 2026-10-01 21:00, iPhone: the viewer of his awake MacBook Pro said
/// "MacBook Pro has no computer running yet, so there is nothing to watch" and
/// "No computer yet" -- the desk sentence for a container that was never
/// started. A Mac is asleep, paused, or missing a permission; each of those is
/// a different thing for him to do, and the deck says which (`reason`).
@MainActor
final class MacLiveViewCopyTests: XCTestCase {

    private func macStatus(reason: String, detail: String) throws -> AgentScreenStatus {
        let json = """
            {"desk":"MacBook Pro","display":"MacBook Pro",
             "computer":{"running":false,"image":"macOS","container":"mac_b6e7587429e6"},
             "width":1512,"height":982,"stale_after":10,"generated_at":1,
             "reason":"\(reason)","detail":"\(detail)"}
            """
        return try DeckCoding.decoder.decode(AgentScreenStatus.self, from: Data(json.utf8))
    }

    private func shown(_ status: AgentScreenStatus) async -> AgentScreenPresentation {
        let model = AgentComputerModel(desk: MacScreenName.agent(nodeId: "mac_b6e7587429e6"),
                                       displayName: "MacBook Pro", client: FakeScreenClient(status: status))
        await model.refreshStatus()
        return AgentScreenPresentation(desk: "MacBook Pro", state: model.state)
    }

    func testAnAsleepMacSaysSoAndNotNoComputer() async throws {
        let p = await shown(try macStatus(reason: "mac_asleep",
                                          detail: "MacBook Pro is asleep or offline. Wake it."))
        XCTAssertEqual(p.statusLine, "Your Mac is asleep")
        XCTAssertTrue(p.spokenLabel.contains("asleep or offline"), p.spokenLabel)
        XCTAssertFalse(p.spokenLabel.contains("computer"), p.spokenLabel)
    }

    func testScreenRecordingOffSaysWhereToTurnItOn() async throws {
        let p = await shown(try macStatus(reason: "screen_recording_off",
                                          detail: "Screen Recording is off for Agent Deck on MacBook Pro. "
                                            + "On the Mac open Agent Deck Settings -> Mac."))
        XCTAssertEqual(p.statusLine, "Screen Recording is off")
        XCTAssertTrue(p.spokenLabel.contains("Settings"), p.spokenLabel)
        XCTAssertFalse(p.spokenLabel.contains("no computer"), p.spokenLabel)
    }

    func testEveryMacReasonHasItsOwnShortLine() {
        let cases: [(String, String)] = [
            ("mac_asleep", "Your Mac is asleep"),
            ("mac_paused", "Agent Deck is paused"),
            ("screen_recording_off", "Screen Recording is off"),
            ("accessibility_off", "Accessibility is off"),
            ("frame_pending", "Waiting for your Mac"),
            ("full_access_required", "Needs Full access"),
        ]
        for (reason, line) in cases {
            let body = Data(#"{"ok":false,"reason":"\#(reason)","detail":"d"}"#.utf8)
            let refusal = ScreenRefusal(status: 409, body: body)
            XCTAssertEqual(refusal.shortLine, line, reason)
            XCTAssertEqual(refusal.sentence, "d", reason)
        }
    }

    func testFullAccessRefusalWithNoDetailSaysWhereToTurnItOn() {
        let body = Data(#"{"ok":false,"reason":"full_access_required","detail":""}"#.utf8)
        XCTAssertEqual(ScreenRefusal(status: 409, body: body).sentence,
                       "Turn on Full access on your Mac (Agent Deck → Settings → Mac) to control it from your phone.")
    }

    func testADeskWithNoMachineStillSaysNoComputer() async throws {
        let p = await shown(try AgentComputerTests.atlasStatus())
        XCTAssertEqual(p.statusLine, "No computer yet")
    }
}

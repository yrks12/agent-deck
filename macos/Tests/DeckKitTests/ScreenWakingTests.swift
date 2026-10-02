import XCTest
@testable import DeckKit

/// **A stopped desk browser says "Waking browser…", not "No computer yet".**
///
/// The deck now stops idle desk browsers to keep the box alive (measured
/// 2026-10-01: 13 Chromiums on a 7.9 GB box, swap full, 15-24 s per frame).
/// Opening the screen starts the browser again and the status route answers
/// `computer.running: false, waking: true` for the ~5 s that takes (measured
/// against wake-probe on the box: running at 4.9 s, first frame at 5.5 s).
/// "No computer yet" during those seconds would tell him there is nothing to
/// wait for.
@MainActor
final class ScreenWakingTests: XCTestCase {

    static let wakingJSON = """
    {"desk":"atlas","computer":{"running":false,"waking":true,
     "image":"agent-deck/desk-computer:1","container":"deck-desk-atlas"},
     "display":":99","width":1280,"height":800,"stale_after":30.0,
     "frame_url":"/v1/agents/atlas/screen.jpg",
     "input_url":"/v1/agents/atlas/screen/input","generated_at":1790829566.5}
    """

    func testWakingDecodesAndAnOlderDeckWithoutTheFieldIsNotWaking() throws {
        let waking = try DeckCoding.decoder.decode(
            AgentScreenStatus.self, from: Data(Self.wakingJSON.utf8))
        XCTAssertFalse(waking.isRunning)
        XCTAssertTrue(waking.isWaking)

        let old = try AgentComputerTests.atlasStatus()
        XCTAssertFalse(old.isWaking, "a deck that never sends `waking` is not waking")
    }

    func testAWakingDeskIsDrawnAsWakingAndOffersNoOpen() async throws {
        let status = try DeckCoding.decoder.decode(
            AgentScreenStatus.self, from: Data(Self.wakingJSON.utf8))
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas",
                                       client: FakeScreenClient(status: status))
        await model.refreshStatus()

        XCTAssertEqual(model.state, .waking)
        let shown = AgentScreenPresentation(desk: "Atlas", state: model.state)
        XCTAssertEqual(shown.statusLine, "Waking browser…")
        XCTAssertEqual(shown.spokenLabel,
                       "Atlas's browser is waking up. It will appear in a few seconds.")
        XCTAssertFalse(shown.showsOpen)
        XCTAssertNil(shown.frame)
    }

    func testWakingIsNotSaidLikeAnyOtherState() {
        let spoken = Set([AgentScreenState.connecting, .noComputer, .waking]
            .map { AgentScreenPresentation(desk: "Atlas", state: $0).spokenLabel })
        XCTAssertEqual(spoken.count, 3)
    }
}

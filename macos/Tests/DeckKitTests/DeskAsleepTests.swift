import XCTest
@testable import DeckKit

/// **K3/K6 on the agent row: a resting desk reads as resting, and a desk's own
/// voice and face travel with it.**
///
/// `ASLEEP` is a desk with no live session whose last one resumes on the next
/// message. Drawn as `IDLE` (the contract's fallback for an unknown state) it
/// claimed a session was up; drawn as `OFFLINE` it told him "nothing will read
/// this", which is false — the message wakes it.
final class DeskAsleepTests: XCTestCase {

    private func agent(_ json: String) throws -> Agent {
        try DeckCoding.decoder.decode(Agent.self, from: Data(json.utf8))
    }

    func testAsleepIsItsOwnStateAndNotIdle() throws {
        XCTAssertEqual(AgentState(wire: "ASLEEP"), .asleep)
        XCTAssertEqual(try agent(#"{"name":"atlas","state":"ASLEEP"}"#).state, .asleep)
        XCTAssertEqual(AgentState.asleep.label, "Asleep")
    }

    func testAnAsleepDeskSaysItWakesWhenMessaged() throws {
        var atlas = Agent(name: "atlas", title: "COS")
        atlas.state = .asleep
        let status = try XCTUnwrap(DeskStatus.make(
            agent: atlas, connection: .live, approvals: [], isSending: false))
        XCTAssertEqual(status.spokenLabel, "Asleep — it wakes when you message it.")
        XCTAssertEqual(status.tone, .quiet, "asleep is not a fault")
    }

    /// The owner's words for the old copy were that it was scary. No state may
    /// tell him nothing will read what he writes.
    func testNoStateSaysNothingWillReadThis() {
        for state in AgentState.allCases {
            var desk = Agent(name: "atlas", title: "COS")
            desk.state = state
            let said = DeskStatus.make(agent: desk, connection: .live, approvals: [], isSending: false)?
                .spokenLabel ?? ""
            XCTAssertFalse(said.localizedCaseInsensitiveContains("nothing will read"),
                           "\(state.rawValue) says: \(said)")
        }
    }

    /// K6 as shipped: the drawn character comes back as `avatar_look`
    /// (`{"shape","color"}`), and `avatar` stays the opaque string it always
    /// was — often `null` — which must not clear the character.
    func testAnAvatarOverrideDecodesAndChangesTheFace() throws {
        let styled = try agent(#"{"name":"atlas","avatar":null,"avatar_look":{"shape":"cloud","color":7}}"#)
        XCTAssertEqual(styled.avatarStyle, AvatarStyle(shape: .cloud, color: 7))
        XCTAssertNil(styled.avatar)
        XCTAssertEqual(styled.look.shape, .cloud)
        XCTAssertEqual(styled.look.tintIndex, 7)

        let plain = try agent(#"{"name":"atlas","avatar":"atlas.png"}"#)
        XCTAssertEqual(plain.avatar, "atlas.png")
        XCTAssertNil(plain.avatarStyle)
        XCTAssertEqual(plain.look, AvatarLook.forName("atlas"), "no override, the derived face")

        let cleared = try agent(#"{"name":"atlas","avatar":"atlas.png","avatar_look":null}"#)
        XCTAssertNil(cleared.avatarStyle)
        XCTAssertEqual(cleared.avatar, "atlas.png")

        let unknownShape = try agent(#"{"name":"atlas","avatar_look":{"shape":"dodecahedron","color":99}}"#)
        XCTAssertEqual(unknownShape.look.shape, AvatarLook.forName("atlas").shape,
                       "a shape this build cannot draw falls back to the derived one")
        XCTAssertLessThan(unknownShape.look.tintIndex, AvatarLook.tintCount)
    }

    func testAPerDeskVoiceDecodes() throws {
        let voiced = try agent(#"{"name":"atlas","voice":{"id":"com.apple.voice.premium.en-GB.Serena","rate":1.1}}"#)
        XCTAssertEqual(voiced.voice, DeskVoice(id: "com.apple.voice.premium.en-GB.Serena", rate: 1.1))
        XCTAssertNil(try agent(#"{"name":"atlas","voice":null}"#).voice)
    }
}

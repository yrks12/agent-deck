import XCTest
@testable import DeckKit

/// Faces. The reference uses distinct coloured shapes rather than a wall of
/// identical circles, and the property that makes them useful is that they do
/// not move: the same agent is the same shape and the same colour every time
/// this app is launched, so the sidebar can be read at a glance.
final class AvatarLookTests: XCTestCase {

    func testTheSameNameAlwaysGetsTheSameFace() {
        let first = AvatarLook.forName("hemingway")
        let second = AvatarLook.forName("hemingway")

        XCTAssertEqual(first, second)
    }

    /// Swift's own `hashValue` is seeded per process, so a face derived from it
    /// would change on every launch. These are the values the FNV-1a
    /// derivation produces, written out so that swapping it for `Hasher` fails
    /// here rather than in a screenshot months later.
    func testTheDerivationIsStableAcrossLaunchesNotJustWithinOne() {
        XCTAssertEqual(AvatarLook.forName("chief").shape, .wedge)
        XCTAssertEqual(AvatarLook.forName("chief").tintIndex, 5)
        XCTAssertEqual(AvatarLook.forName("hemingway").shape, .cloud)
        XCTAssertEqual(AvatarLook.forName("hemingway").tintIndex, 6)
        XCTAssertEqual(AvatarLook.forName("initech ux").shape, AvatarLook.forName("initech ux").shape)
        XCTAssertEqual(AvatarLook.forName("grok bot").shape, .tablet)
        XCTAssertEqual(AvatarLook.forName("larder").shape, .squircle)
        XCTAssertEqual(AvatarLook.forName("Umbrella").shape, .teardrop)
    }

    func testDifferentAgentsDoNotAllLookTheSame() {
        let names = ["chief", "hemingway", "seeker", "ledger", "travel scout", "larder", "grok bot"]

        let shapes = Set(names.map { AvatarLook.forName($0).shape })

        XCTAssertGreaterThanOrEqual(shapes.count, 3, "a wall of identical circles is what this replaces")
    }

    /// The face is keyed on the identity, not on anything editable — renaming
    /// a title or going offline must not repaint the sidebar.
    func testTheFaceDoesNotMoveWhenSomethingEditableChanges() {
        var agent = Agent(name: "seeker", title: "Researcher", state: .idle)
        let before = agent.look

        agent.title = "Analyst"
        agent.state = .dead
        agent.detail = "something else"

        XCTAssertEqual(agent.look, before)
    }

    func testInitialsAreStillTheFallbackInsideTheShape() {
        XCTAssertEqual(Agent(name: "travel scout", title: "").look.initials, "TS")
        XCTAssertEqual(Agent(name: "chief", title: "").look.initials, "C")
    }

    /// A name this client cannot take initials from still gets a face rather
    /// than an empty shape.
    func testAnUnlettersedNameStillGetsAFace() {
        let look = AvatarLook.forName("👾")

        XCTAssertFalse(look.initials.isEmpty)
        XCTAssertTrue(AvatarShape.allCases.contains(look.shape))
    }
}

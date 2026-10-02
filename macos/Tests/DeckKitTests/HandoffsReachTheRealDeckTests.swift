import XCTest
@testable import DeckKit

/// **The two methods that decide whether a secure handoff ever reaches him.**
///
/// `DeckClient` gives `handoffs()` and `resolveHandoff(id:outcome:)` honest
/// defaults — an empty page and a refusal — so a client built against an older
/// deck degrades instead of crashing. That default is also exactly what an
/// *unwired* transport looks like, and the agent that built the tray said so
/// plainly rather than letting it pass: the tray drew approvals and no
/// handoffs, on a deck that was serving them, and nothing failed.
///
/// A secure handoff is the one class of block where no permission helps
/// because the desk **cannot act**: a 2FA code, a CAPTCHA, an SMS
/// confirmation, `gh auth login`, signing into Google. It is precisely the
/// case the owner complained about — *"agents asking me on whatsup to confirm
/// but not on screen whats the point"* — one layer up from the approval bug
/// that started this. So the test asserts the presence of the good signal: the
/// real transport **asks the deck**, and the answer **carries his verb**.
///
/// Sweeps the class rather than one call: both verbs, and both directions of
/// the pair, because `done` and `skipped` are different instructions and a
/// transport that posted a constant would satisfy any single-verb test.
final class HandoffsReachTheRealDeckTests: XCTestCase {

    private let base = URL(string: "https://deck.local")!

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: base, tokens: store, performer: performer)
    }

    /// One page of steps only he can take, off the route the deck serves.
    func testTheRealTransportAsksTheDeckForTheStepsOnlyAHumanCanTake() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/handoffs"] = Data(#"""
        {"handoffs":[],"generated_at":1.0}
        """#.utf8)

        _ = try await client(performer).handoffs()

        XCTAssertEqual(
            performer.paths, ["/v1/handoffs"],
            "the app never asked the deck what is waiting on him — the protocol "
            + "default answers 'nothing is stuck' whether or not anything is")
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
    }

    /// THE pair. `done` means the desk re-checks that the step actually
    /// worked; `skipped` means it abandons that path for good and reports what
    /// it can no longer finish. `server/handoff.py` calls collapsing the two an
    /// infinite retry loop, so a transport that sent a constant would be the
    /// same defect at a different altitude.
    func testHisVerbIsWhatTravels() async throws {
        for outcome in [HandoffOutcome.done, .skipped] {
            let performer = StubPerformer()
            performer.bodies["/v1/handoffs/h1"] = Data(#"""
            {"ok":true,"handoff":{"id":"h1","status":"resolved"},"resumed":true}
            """#.utf8)

            _ = try await client(performer).resolveHandoff(id: "h1", outcome: outcome)

            let request = try XCTUnwrap(performer.requests.first)
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(performer.paths, ["/v1/handoffs/h1"])
            XCTAssertEqual(
                request.value(forHTTPHeaderField: "Content-Type"), "application/json")

            let body = try XCTUnwrap(request.httpBody)
            let sent = try JSONSerialization.jsonObject(with: body) as? [String: String]
            XCTAssertEqual(
                sent, ["reply": outcome.rawValue],
                "the deck reads exactly `payload.get(\"reply\")`; it received "
                + "\(sent.map(String.init(describing:)) ?? "nothing") for \(outcome)")
        }
    }

    /// The id is his data, not ours. A handoff id with a slash or a space in it
    /// must not silently become a different route — the same rule the peer
    /// thread ids already carry.
    func testAnAwkwardIdIsEncodedIntoThePathRatherThanReshapingIt() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/handoffs/a b/c"] = Data(#"""
        {"ok":true,"handoff":{"id":"a b/c","status":"resolved"},"resumed":false}
        """#.utf8)

        _ = try? await client(performer).resolveHandoff(id: "a b/c", outcome: .done)

        let url = try XCTUnwrap(performer.requests.first?.url?.absoluteString)
        XCTAssertTrue(
            url.contains("a%20b%2Fc"),
            "an id with a space and a slash was pasted into the path raw: \(url)")
    }
}

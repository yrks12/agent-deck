import XCTest
@testable import DeckKit

/// **"Always allow" must stick — and it has to be a button he can tap.**
///
/// The deck offers a browser card's third answer as `always_option`, beside
/// `options` (which stays exactly done|skipped so an older app still decodes).
/// Tapping it posts `reply: always`, and the deck writes a durable grant for
/// that desk + site: the next time that desk hits the same kind of step there,
/// its click goes straight through. Before this, the app drew only the two
/// old buttons, so "Allow always" existed on the server and nowhere he could
/// reach.
///
/// Both the Mac tray and the iPhone's attention cards draw
/// `AttentionItem.actions`, so this is the one place both pick it up.
final class AllowAlwaysForThisSiteTests: XCTestCase {

    private func page(alwaysOption: String) throws -> HandoffsPage {
        try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1756000100,"agent":"atlas","asked_by":"atlas",
          "desk_known":true,"kind":"payment",
          "needs":"Atlas wants to confirm a payment on https://checkout.stripe.com.",
          "state":"nothing submitted","where":"https://checkout.stripe.com/c/pay",
          "evidence":"surface=browser desk=atlas origin=https://checkout.stripe.com",
          "status":"waiting","options":[
            {"reply":"done","available":true,"summary":"Allow — atlas carries on by itself"},
            {"reply":"skipped","available":true,"summary":"No — the desk abandons this step"}]
          \(alwaysOption)}]}
        """.utf8))
    }

    func testABrowserCardDrawsAllowAlwaysForThisSite() throws {
        let decoded = try page(alwaysOption: """
        ,"always_option":{"reply":"always","available":true,
          "summary":"Allow always — atlas may do this (payment) on https://checkout.stripe.com from now on"}
        """)
        let item = AttentionItem.make(
            handoff: try XCTUnwrap(decoded.handoffs.first), agents: [:])

        XCTAssertEqual(item.actions.map(\.label),
                       ["I'm done, continue", "Skip this step",
                        "Allow always for this site"])
        let always = try XCTUnwrap(item.actions.last)
        XCTAssertEqual(always.verb, .handoff(.always))
        XCTAssertTrue(always.isAvailable)
        XCTAssertTrue(always.isStanding, "it writes a rule that outlives this card")
    }

    func testACardWithoutTheOptionStillDrawsItsTwoButtons() throws {
        let decoded = try page(alwaysOption: "")
        let item = AttentionItem.make(
            handoff: try XCTUnwrap(decoded.handoffs.first), agents: [:])
        XCTAssertEqual(item.actions.count, 2)
    }

    func testTappingItPostsReplyAlways() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/handoffs/h1"] = Data(#"""
        {"ok":true,"handoff":{"id":"h1","status":"done"},"allowed":"always","resumed":true}
        """#.utf8)
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: store, performer: performer)

        _ = try await client.resolveHandoff(id: "h1", outcome: .always)

        let body = try XCTUnwrap(performer.requests.first?.httpBody)
        let sent = try JSONSerialization.jsonObject(with: body) as? [String: String]
        XCTAssertEqual(sent, ["reply": "always"])
    }
}

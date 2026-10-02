import XCTest
@testable import DeckKit

/// Measured against the real deck: `/api/state` answered 200, `/v1/agents`
/// answered 401 without a token and 200 with the right one — and the sidebar
/// said "Can't reach the deck — Add your deck API token in Settings". The deck
/// was reachable. The Keychain was empty. The heading sent its reader hunting
/// for a network fault, and the two sentences contradicted each other.
///
/// The wording *is* the behaviour here, so these assert the actual strings.
/// "An error was shown" would have passed the whole time the bug was live.
final class ConnectionFailureTests: XCTestCase {

    // MARK: three states, three headings

    func testNoTokenOnThisMacIsNotWordedAsAnUnreachableDeck() {
        let shown = FailurePresentation.make(.missingToken)

        XCTAssertEqual(shown.heading, "Not connected yet")
        XCTAssertEqual(shown.detail, "Add your deck API token in Settings.")
        XCTAssertEqual(
            shown.action, .openSettings(title: "Open Settings"),
            "retrying without a token can only fail again; the button must go where the fix is"
        )
    }

    func testARejectedTokenSaysTheDeckRejectedIt() {
        let shown = FailurePresentation.make(.unauthorized)

        XCTAssertEqual(shown.heading, "The deck rejected this token")
        XCTAssertEqual(shown.detail, "The deck rejected this token. Set a different one in Settings.")
        XCTAssertEqual(shown.action, .openSettings(title: "Replace the token"))
    }

    func testOnlyATransportFailureSaysTheDeckCannotBeReached() {
        let shown = FailurePresentation.make(.transport("Could not connect to the server."))

        XCTAssertEqual(shown.heading, "Can't reach the deck")
        XCTAssertEqual(shown.action, .retry(title: "Try again"))
    }

    func testTheThreeHeadingsAreNeverInterchangeable() {
        let headings = [DeckError.missingToken, .unauthorized, .transport("refused")]
            .map { FailurePresentation.make($0).heading }

        XCTAssertEqual(Set(headings).count, 3, "three causes, three answers: \(headings)")
        XCTAssertFalse(
            FailurePresentation.make(.missingToken).heading.contains("reach"),
            "an empty Keychain is not a transport fault and must never read like one"
        )
    }

    /// The fourth state the app already had. It keeps its own words, and it
    /// offers no button, because nothing the reader can press fixes a server
    /// that has no token of its own.
    func testADeckWithNoTokenOfItsOwnStillHasItsOwnHeadingAndNoDeadButton() {
        let shown = FailurePresentation.make(.authNotConfigured)

        XCTAssertEqual(shown.heading, "This deck has no token of its own")
        XCTAssertEqual(shown.action, .none)
        XCTAssertNotEqual(shown.heading, FailurePresentation.make(.missingToken).heading)
    }

    func testEveryOfferedButtonCanActuallyDoSomething() {
        // A retry is only offered where retrying is capable of a different
        // answer; the store's own rule and the screen's must not drift apart.
        for error: DeckError in [.missingToken, .unauthorized, .authNotConfigured,
                                 .transport("refused"), .badCursor, .unknownThread] {
            let shown = FailurePresentation.make(error)
            if case .retry = shown.action {
                XCTAssertTrue(error.isRetryable, "offered a retry for \(error), which cannot succeed")
            }
        }
    }

    // MARK: reachable without a network call

    func testTheNoTokenStateIsDecidedBeforeAnyRequestIsIssued() async {
        let performer = StubPerformer()
        let client = HTTPDeckClient(
            baseURL: URL(string: "https://deck.local")!,
            tokens: InMemoryTokenStore(),
            performer: performer
        )

        do {
            _ = try await client.roster()
            XCTFail("an anonymous request to this deck is never a useful thing to have done")
        } catch {
            XCTAssertEqual(error as? DeckError, .missingToken)
        }
        XCTAssertTrue(
            performer.requests.isEmpty,
            "with no deck running, a request first would surface as a transport error instead"
        )
    }

    /// The paired positive: with a token, the request really is issued.
    func testATokenThatExistsIsSentRatherThanRefusedLocally() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents"] = Data(#"{"agents":[],"generated_at":1.0}"#.utf8)
        let client = HTTPDeckClient(
            baseURL: URL(string: "https://deck.local")!,
            tokens: InMemoryTokenStore(token: "sekret"),
            performer: performer
        )

        _ = try await client.roster()

        XCTAssertEqual(performer.paths, ["/v1/agents"])
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
    }
}

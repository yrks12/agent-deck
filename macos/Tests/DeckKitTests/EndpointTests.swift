import XCTest
@testable import DeckKit

/// A shipped app must not open on made-up agents.
///
/// Found live. `Backend.make()` read `DECK_URL` from the environment and, with
/// nothing there, returned the fixture. Double-clicking the app bundle carries
/// no environment, so the only way to see the real deck was to launch it from a
/// shell that had exported the variable — which is not how anyone opens an app.
/// The owner opened it, saw seven agents he had never hired, and asked why his
/// own sessions were missing. They were not missing; he was looking at a mock.
///
/// The fix puts the decision in DeckKit where it can be tested, and inverts the
/// default: the real deck is what you get, and the fixture is something you ask
/// for by name. Both halves are asserted — a resolver that always returned the
/// live URL would pass the first test and make the fixture unreachable.
final class EndpointTests: XCTestCase {

    /// THE test. Revert the default and this fails.
    func testWithNoEnvironmentAtAllTheAppTalksToTheRealDeck() {
        let choice = DeckEndpoint.resolve(environment: [:])
        XCTAssertEqual(choice, .live(DeckEndpoint.localDeck),
                       "an app opened from the Dock showed a mock")
        XCTAssertEqual(DeckEndpoint.localDeck.absoluteString,
                       "http://127.0.0.1:7788")
    }

    /// The paired positive: the fixture is still reachable, or every test and
    /// demo that relies on it breaks and the first test above is worthless.
    func testTheFixtureIsStillAvailableWhenAskedForByName() {
        XCTAssertEqual(DeckEndpoint.resolve(environment: ["DECK_FIXTURE": "1"]),
                       .fixture)
    }

    /// A named deck wins over the default, which is the whole point of keeping
    /// the variable: pointing the app at the box on 10.99.0.1 later.
    func testAnExplicitDeckUrlWins() {
        XCTAssertEqual(DeckEndpoint.resolve(environment: ["DECK_URL": "http://10.99.0.1:7788"]),
                       .live(URL(string: "http://10.99.0.1:7788")!))
    }

    /// Sweeping the class rather than the instance: every way the variable can
    /// be junk must land on the real deck, never silently on a mock. An empty
    /// string is the one that actually happens — `export DECK_URL=` in a shell.
    func testAJunkOrEmptyDeckUrlFallsBackToTheRealDeckAndNeverToTheFixture() {
        for junk in ["", "   ", "not a url at all", "://"] {
            let choice = DeckEndpoint.resolve(environment: ["DECK_URL": junk])
            XCTAssertEqual(choice, .live(DeckEndpoint.localDeck),
                           "DECK_URL=\(junk.debugDescription) did not reach the deck")
        }
    }

    /// Settings has a "Base URL" field. It wrote `deckBaseURL` into
    /// UserDefaults and **nothing read it** — the backend was decided once, at
    /// launch, from the environment alone. So the one control that looks like
    /// "this is where you point the app" did nothing at all, which is worse
    /// than not offering it.
    func testTheBaseUrlSavedInSettingsIsWhatTheAppTalksTo() {
        XCTAssertEqual(
            DeckEndpoint.resolve(environment: [:], stored: "http://10.99.0.1:7788"),
            .live(URL(string: "http://10.99.0.1:7788")!)
        )
    }

    /// The environment still wins, so a launch that names a deck is not
    /// silently overridden by whatever is left in the panel.
    func testTheEnvironmentStillOutranksTheSavedBaseUrl() {
        XCTAssertEqual(
            DeckEndpoint.resolve(environment: ["DECK_URL": "http://10.0.0.9:7788"],
                                 stored: "http://10.99.0.1:7788"),
            .live(URL(string: "http://10.0.0.9:7788")!)
        )
        XCTAssertEqual(
            DeckEndpoint.resolve(environment: ["DECK_FIXTURE": "1"],
                                 stored: "http://10.99.0.1:7788"),
            .fixture
        )
    }

    /// Same sweep as the environment gets: a half-typed URL in the panel must
    /// land on the deck on this Mac, never on the fixture.
    func testAJunkSavedBaseUrlFallsBackToTheRealDeck() {
        for junk in ["", "   ", "not a url at all", "://"] {
            XCTAssertEqual(
                DeckEndpoint.resolve(environment: [:], stored: junk),
                .live(DeckEndpoint.localDeck),
                "stored base URL \(junk.debugDescription) did not reach the deck"
            )
        }
    }

    /// The title has to tell the truth, because "why am I seeing a mock" is
    /// only answerable if the window says so.
    /// Docs screenshots of the fixture (`DECK_DOCS_SCREENSHOT=1`) show the
    /// product's own title; every other fixture run still says it is one.
    func testADocsScreenshotOfTheFixtureIsTitledLikeTheProduct() {
        XCTAssertEqual(DeckEndpoint.windowTitle(.fixture, environment: ["DECK_DOCS_SCREENSHOT": "1"]),
                       "Agent Deck")
        XCTAssertEqual(DeckEndpoint.windowTitle(.fixture, environment: [:]), "Agent Deck (fixture)")
        XCTAssertEqual(DeckEndpoint.windowTitle(.fixture, environment: ["DECK_DOCS_SCREENSHOT": "0"]),
                       "Agent Deck (fixture)")
        XCTAssertEqual(DeckEndpoint.windowTitle(.live(DeckEndpoint.localDeck),
                                                environment: ["DECK_DOCS_SCREENSHOT": "1"]),
                       "Agent Deck")
    }

    func testOnlyTheFixtureIsLabelledAFixture() {
        XCTAssertEqual(DeckEndpoint.Choice.fixture.windowTitle, "Agent Deck (fixture)")
        XCTAssertEqual(DeckEndpoint.Choice.live(DeckEndpoint.localDeck).windowTitle,
                       "Agent Deck")
    }
}

import XCTest
@testable import DeckKit
@testable import DeckUI

/// The "+" at the top of the sidebar. Its whole value is in the refusals: a
/// deck that says no has told you *why*, and "Couldn't create agent" throws
/// that away. Every reason the deck can send has to arrive as a different
/// sentence, and the one that carries a number has to keep the number.
@MainActor
final class CreateAgentTests: XCTestCase {

    private func refusal(_ reason: String, _ detail: String, status: Int = 409) -> DeckError {
        DeckError(
            status: status,
            body: Data(#"{"ok":false,"reason":"\#(reason)","detail":"\#(detail)"}"#.utf8)
        )
    }

    private var validDraft: AgentDraft {
        AgentDraft(
            name: "scribe", title: "Researcher", detail: "Own the sources.",
            directory: "~/Projects/acme", engine: "claude", boss: "chief"
        )
    }

    // MARK: the four refusals

    func testEachRefusalReasonProducesItsOwnSentence() {
        let sentences = [
            refusal("name_taken", "a desk named 'scribe' already exists"),
            refusal("too_deep", "chief is already 3 deep; the cap is 3"),
            refusal("too_many_live", "there are already 8 agents running"),
            refusal("no_such_boss", "no desk named 'chieff'"),
        ].map(\.userFacingText)

        XCTAssertEqual(Set(sentences).count, 4, "four reasons, four sentences: \(sentences)")
        for sentence in sentences {
            XCTAssertFalse(
                sentence.lowercased().contains("couldn't create"),
                "a generic sentence tells him nothing: \(sentence)"
            )
        }
    }

    func testTheNameIsTakenSentenceSendsHimBackToTheNameField() {
        let text = refusal("name_taken", "a desk named 'scribe' already exists").userFacingText

        XCTAssertTrue(text.contains("name"), text)
        XCTAssertTrue(text.contains("a desk named 'scribe' already exists"), text)
    }

    func testTheTooDeepSentenceNamesTheOrgChartAndKeepsTheServersNumbers() {
        let text = refusal("too_deep", "chief is already 3 deep; the cap is 3").userFacingText

        XCTAssertTrue(text.lowercased().contains("reports to")
                      || text.lowercased().contains("org chart"), text)
        XCTAssertTrue(text.contains("the cap is 3"), text)
    }

    /// The example the owner gave: the count is the whole point of the message.
    func testTheTooManyLiveSentenceCarriesHowManyAreAlreadyRunning() {
        let text = refusal("too_many_live", "there are already 8 agents running").userFacingText

        XCTAssertTrue(text.contains("there are already 8 agents running"), text)
    }

    func testTheNoSuchBossSentencePointsAtTheBossField() {
        let text = refusal("no_such_boss", "no desk named 'chieff'").userFacingText

        XCTAssertTrue(text.lowercased().contains("reports to"), text)
        XCTAssertTrue(text.contains("no desk named 'chieff'"), text)
    }

    /// The deck raises this one too — a working directory that is not a
    /// directory — and it is a different fix from any of the four above.
    func testAMissingWorkingDirectoryIsItsOwnSentence() {
        let text = refusal("no_such_cwd", "'~/Projects/nope' is not a directory", status: 400)
            .userFacingText

        XCTAssertTrue(text.lowercased().contains("folder")
                      || text.lowercased().contains("directory"), text)
        XCTAssertNotEqual(text, refusal("name_taken", "x").userFacingText)
    }

    /// §11's sixth refusal: a required field arrived empty. Different fix
    /// again, so different words again.
    func testAMissingFieldIsItsOwnSentence() {
        let text = refusal("missing_field", "cwd", status: 400).userFacingText

        XCTAssertTrue(text.lowercased().contains("missing"), text)
        XCTAssertTrue(text.contains("cwd"), text)
        XCTAssertNotEqual(text, refusal("no_such_cwd", "cwd", status: 400).userFacingText)
    }

    // MARK: the request

    func testTheCreateRequestCarriesTheFormAndNothingElse() async throws {
        let performer = StubPerformer()
        performer.status = 201
        performer.bodies["/v1/agents"] = Data(
            #"{"ok":true,"agent":{"name":"scribe","label":"Researcher"}}"#.utf8
        )
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)

        let agent = try await client.createAgent(validDraft)

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/v1/agents")
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any]
        )
        XCTAssertEqual(json["name"] as? String, "scribe")
        XCTAssertEqual(json["label"] as? String, "Researcher")
        XCTAssertEqual(json["charter"] as? String, "Own the sources.")
        XCTAssertEqual(json["cwd"] as? String, "~/Projects/acme")
        XCTAssertEqual(json["engine"] as? String, "claude")
        XCTAssertEqual(json["reports_to"] as? String, "chief")
        XCTAssertEqual(agent.name, "scribe")
    }

    func testAnAgentWithNoBossSendsAnExplicitNullRatherThanOmittingIt() async throws {
        let performer = StubPerformer()
        performer.status = 201
        performer.bodies["/v1/agents"] = Data(#"{"ok":true,"agent":{"name":"scribe"}}"#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)
        var draft = validDraft
        draft.boss = nil

        _ = try await client.createAgent(draft)

        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests.first?.httpBody))
                as? [String: Any]
        )
        XCTAssertTrue(json.keys.contains("reports_to"), "top of the org chart is a value, not a gap")
        XCTAssertTrue(json["reports_to"] is NSNull)
    }

    // MARK: the form refuses before the network does

    func testANamelessDraftIsRefusedHereAndNeverSent() async {
        let client = ScriptedDeckClient()
        var draft = validDraft
        draft.name = "  "

        XCTAssertNotNil(draft.problem)
        let store = DeckStore(client: client)
        let created = await store.createAgent(draft)

        XCTAssertFalse(created)
        XCTAssertEqual(store.createProblem, draft.problem)
        XCTAssertTrue(client.calls.isEmpty, "nothing should have gone to the deck")
    }

    func testANameTheDeckCouldNotKeyOnIsRefusedWithTheReasonWhy() {
        var draft = validDraft
        draft.name = "Scribe/2"

        let problem = draft.problem

        XCTAssertNotNil(problem)
        XCTAssertTrue(problem?.contains("thread") ?? false,
                      "the name goes into every thread id: \(problem ?? "nil")")
    }

    func testAFilledInFormIsReady() {
        XCTAssertNil(validDraft.problem)
    }

    // MARK: the store

    func testARefusedCreateKeepsTheFormOpenAndQuotesTheDeck() async {
        let client = ScriptedDeckClient()
        client.createError = refusal("too_many_live", "there are already 8 agents running")
        let store = DeckStore(client: client)

        let created = await store.createAgent(validDraft)

        XCTAssertFalse(created)
        XCTAssertEqual(store.createProblem?.contains("there are already 8 agents running"), true)
    }

    /// §11: the 201 carries exactly one element of the roster array, so the
    /// new desk goes straight into the sidebar. A refetch here would be a
    /// second request for something the deck already sent.
    func testAnAcceptedHireLandsInTheSidebarWithoutARefetch() async throws {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(
            agents: [makeAgent("chief")],
            threads: [makeThread("direct:chief", agent: "chief")],
            sectionOrder: ["Work"]
        )
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([])]
        let store = DeckStore(client: client)
        await store.loadRoster()
        let rosterCallsBefore = client.calls.filter { $0 == .roster }.count

        let created = await store.createAgent(validDraft)

        XCTAssertTrue(created)
        XCTAssertEqual(client.calls.filter { $0 == .roster }.count, rosterCallsBefore,
                       "the deck already sent the row; asking again is a wasted request")
        guard case .loaded(let snapshot) = store.roster else {
            return XCTFail("expected a loaded sidebar, got \(store.roster)")
        }
        XCTAssertTrue(snapshot.sections.flatMap(\.rows).map(\.id).contains("scribe"))
        XCTAssertEqual(store.selectedAgentName, "scribe", "a new desk opens where you can talk to it")
    }

    /// The owner sets his own agents up. The fixture exists for the form and the
    /// tests, and it must never look like a way to write to the real roster.
    func testCreatingAgainstTheFixtureLeavesTheCannedRosterAlone() async throws {
        let client = FixtureDeckClient()
        let before = try await client.roster().agents.map(\.name)

        _ = try await client.createAgent(validDraft)

        let after = try await client.roster().agents.map(\.name)
        XCTAssertEqual(before, after, "the fixture roster is canned; nothing may be added to it")
    }
}

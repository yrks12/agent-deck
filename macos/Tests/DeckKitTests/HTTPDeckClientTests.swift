import XCTest
@testable import DeckKit

/// The HTTP adapter is thin on purpose, so what is worth pinning is the shape
/// of the requests it builds and the errors it raises. No sockets are opened:
/// the performer is injected.
final class HTTPDeckClientTests: XCTestCase {

    private let base = URL(string: "https://deck.local")!

    private func client(
        _ performer: StubPerformer,
        token: String? = "sekret"
    ) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        if let token { try? store.setToken(token) }
        return HTTPDeckClient(baseURL: base, tokens: store, performer: performer)
    }

    private let emptyAgents = Data(#"{"agents":[],"generated_at":1.0}"#.utf8)

    func testTheWholeSidebarIsOneRequestToOneRoute() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents"] = emptyAgents

        _ = try await client(performer).roster()

        XCTAssertEqual(performer.paths, ["/v1/agents"], "the row state travels on the agent row")
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
    }

    func testTheThreadListIsASeparateOnDemandCall() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/threads"] = Data(#"{"threads":[],"generated_at":1.0}"#.utf8)

        _ = try await client(performer).threads()

        XCTAssertEqual(performer.paths, ["/v1/threads"])
    }

    func testTheHistoryFetchEchoesTheOpaqueCursorAndAsksForALimit() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/threads/direct:chief/messages"] = Data(
            #"{"thread_id":"direct:chief","participants":["owner","chief"],"read_only":false,"messages":[]}"#.utf8
        )

        _ = try await client(performer).messages(
            threadID: "direct:chief", since: "001756000050000000-9f2a15f5b1bf", limit: 50
        )

        let query = try XCTUnwrap(performer.requests.first?.url?.query)
        XCTAssertTrue(query.contains("since=001756000050000000-9f2a15f5b1bf"), query)
        XCTAssertTrue(query.contains("limit=50"), query)
        XCTAssertEqual(performer.requests.first?.httpMethod, "GET")
    }

    func testAPeerThreadIdIsPercentEncodedIntoThePath() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/threads/peer:chief|hemingway/messages"] = Data(
            #"{"thread_id":"peer:chief|hemingway","participants":[],"read_only":true,"messages":[]}"#.utf8
        )

        _ = try await client(performer).messages(
            threadID: "peer:chief|hemingway", since: nil, limit: 50
        )

        let url = try XCTUnwrap(performer.requests.first?.url?.absoluteString)
        XCTAssertTrue(url.contains("peer:chief%7Chemingway"), url)
    }

    func testSendingPostsTheTextAndUnwrapsTheMessage() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/threads/direct:chief/messages"] = Data("""
        {"ok":true,"delivered":false,
         "message":{"id":"m1","cursor":"0001-m1","thread_id":"direct:chief",
          "author":"owner","role":"owner","ts":1788222777.2,"text":"hi",
          "attachments":[],"via":"office"}}
        """.utf8)

        let message = try await client(performer).send(threadID: "direct:chief", text: "hi")

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: String],
            // K2: said as him, on the text channel — see DecisionAndCallRoutesTests.
            ["text": "hi", "as": "owner", "channel": "text"]
        )
        XCTAssertEqual(message.id, "m1")
        XCTAssertTrue(message.isFromUser)
    }

    func testMarkingReadPostsTheMessageIdItHasOnScreen() async throws {
        let performer = StubPerformer()

        try await client(performer).markRead(agent: "Travel Scout", upTo: "m9")

        // `URL.path` decodes, so the encoding has to be asserted on the raw form.
        XCTAssertEqual(
            performer.requests.first?.url?.absoluteString,
            "https://deck.local/v1/agents/Travel%20Scout/read"
        )
        XCTAssertEqual(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests.first?.httpBody))
                as? [String: String],
            ["up_to": "m9"]
        )
    }

    func testMarkingReadWithNothingOnScreenSendsAnEmptyBody() async throws {
        let performer = StubPerformer()

        try await client(performer).markRead(agent: "chief", upTo: nil)

        XCTAssertEqual(
            try JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests.first?.httpBody))
                as? [String: String],
            [:],
            "an empty body means everything currently visible"
        )
    }

    func testUpdatingAnAgentPatchesOnlyTheSettingsKeys() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents/chief"] = Data("""
        {"name":"chief","label":"The Builder","charter":"ships things","avatar":null,
         "section":"Work","boss":null,"reports":[],"notifications":false,"pinned":false,
         "state":"IDLE","unread":0,"thread_id":"direct:chief","is_desk":true}
        """.utf8)
        var agent = makeAgent("chief", title: "The Builder")
        agent.notificationsEnabled = false

        let updated = try await client(performer).updateAgent(agent)

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "PATCH")
        XCTAssertEqual(request.url?.path, "/v1/agents/chief")
        let keys = Set(try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any]
        ).keys)
        XCTAssertTrue(keys.isDisjoint(with: ["reports_to", "boss"]),
                      "sending the org chart at all is a 409")
        XCTAssertFalse(updated.notificationsEnabled)
    }

    // MARK: the interview door (§11.1) — `pretrust` on the 201

    /// Not yet in `client-api.md`; `server/api.py`'s 201 body is the shape
    /// this asserts against. Ships the `pretrust` block through untouched so
    /// the store can show the owner what the window is waiting on.
    func testInterviewingCarriesThePretrustOutcomeThroughWhenNotOk() async throws {
        let performer = StubPerformer()
        performer.status = 201
        performer.bodies["/v1/agents/interview"] = Data("""
        {"ok": true, "provisional": true, "name": "new-hire-7f3a1c",
         "thread_id": "direct:new-hire-7f3a1c",
         "pretrust": {"ok": false, "reason": "lock_busy"},
         "agent": {"name": "new-hire-7f3a1c", "label": "", "state": "OFFLINE",
                   "thread_id": "direct:new-hire-7f3a1c"}}
        """.utf8)

        let outcome = try await client(performer).startInterview(
            InterviewDraft(roleHint: "watch my open PRs")
        )

        XCTAssertEqual(outcome.agent.name, "new-hire-7f3a1c")
        XCTAssertEqual(outcome.pretrust?.ok, false)
        XCTAssertEqual(outcome.pretrust?.problemSentence, BlockedReason.lockBusy.sentence)
    }

    /// A deck that has not shipped `pretrust` yet — additive, so this must not
    /// throw and must not invent a problem that was never reported.
    func testInterviewingWithNoPretrustFieldAtAllReportsNothingWrong() async throws {
        let performer = StubPerformer()
        performer.status = 201
        performer.bodies["/v1/agents/interview"] = Data("""
        {"ok": true, "provisional": true, "name": "new-hire-7f3a1c",
         "thread_id": "direct:new-hire-7f3a1c",
         "agent": {"name": "new-hire-7f3a1c", "label": "", "state": "OFFLINE",
                   "thread_id": "direct:new-hire-7f3a1c"}}
        """.utf8)

        let outcome = try await client(performer).startInterview(
            InterviewDraft(roleHint: "watch my open PRs")
        )

        XCTAssertNil(outcome.pretrust)
    }

    // MARK: errors, read off `reason`

    func testADeckWithNoTokenOfItsOwnIsDistinctFromARejectedToken() async {
        let closed = StubPerformer()
        closed.status = 503
        closed.defaultBody = Data(
            #"{"ok":false,"reason":"auth_not_configured","detail":"..."}"#.utf8
        )
        let rejected = StubPerformer()
        rejected.status = 401
        rejected.defaultBody = Data(#"{"ok":false,"reason":"unauthorized","detail":"..."}"#.utf8)

        await assertThrows(.authNotConfigured) { try await self.client(closed).roster() }
        await assertThrows(.unauthorized) { try await self.client(rejected).roster() }
    }

    func testPostingToAPeerThreadComesBackAsAReadOnlyRefusal() async {
        let performer = StubPerformer()
        performer.status = 409
        performer.defaultBody = Data(
            #"{"ok":false,"reason":"thread_is_read_only","detail":"..."}"#.utf8
        )

        await assertThrows(.threadIsReadOnly) {
            try await self.client(performer).send(threadID: "peer:a|b", text: "hi")
        }
    }

    func testWithNoTokenInTheStoreTheClientSaysSoInsteadOfSendingAnonymously() async {
        let performer = StubPerformer()

        await assertThrows(.missingToken) {
            try await self.client(performer, token: nil).roster()
        }
    }

    func testTheTokenStoreRoundTripsInMemoryForTests() throws {
        let store = InMemoryTokenStore()
        XCTAssertNil(try store.token())

        try store.setToken("abc")
        XCTAssertEqual(try store.token(), "abc")

        try store.removeToken()
        XCTAssertNil(try store.token(), "clearing the field must clear the store")
    }

    private func assertThrows<T>(
        _ expected: DeckError,
        file: StaticString = #filePath,
        line: UInt = #line,
        _ body: () async throws -> T
    ) async {
        do {
            _ = try await body()
            XCTFail("expected \(expected), nothing was thrown", file: file, line: line)
        } catch let error as DeckError {
            XCTAssertEqual(error, expected, file: file, line: line)
        } catch {
            XCTFail("expected \(expected), got \(error)", file: file, line: line)
        }
    }
}

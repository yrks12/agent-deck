import XCTest
@testable import DeckKit

/// **Two Claude accounts on one deck — the wire (S12a).**
///
/// `GET /v1/usage` grows `accounts[]` and `policy`; an agent row grows
/// `account` and `running_account`; `GET /v1/accounts` and
/// `POST /v1/agents/{name}/account` are new. Premise, ASSUMED until the
/// server lands (docs/plans/2026-10-01-two-accounts.md "API/UI"): key names are
/// the plan's; the shape of `/v1/accounts` and of `policy` beyond `mode` is
/// not fixed there, so both are read tolerantly.
///
/// The thing that must not break is the OLD deck: no `accounts`, no `policy`,
/// no `account` on a row. Those decode exactly as they did.
final class TwoAccountsWireTests: XCTestCase {

    private func usage(_ json: String) throws -> ClaudeUsage {
        try DeckCoding.decoder.decode(ClaudeUsage.self, from: Data(json.utf8))
    }

    private let both = #"""
    {"available":true,"stale":false,"reason":null,"plan":{"name":"Max 20×"},
     "windows":[{"key":"session","label":"5-hour","percent":38,"severity":"normal","resets_at":1790800000}],
     "accounts":[
       {"id":"main","label":"Personal","kind":"subscription","plan":{"name":"Max 20×"},"available":true,
        "stale":false,"reason":null,"fetched_at":1790700000,"refresh_expires_at":1793000000,"desks":["chief","scout"],
        "windows":[{"key":"session","label":"5-hour","percent":38,"severity":"normal","resets_at":1790800000},
                   {"key":"weekly_all","label":"Weekly","percent":72,"severity":"warn"}],
        "extra_usage":{"enabled":false}},
       {"id":"work","label":"Work","kind":"subscription","plan":"pro","available":false,"stale":true,
        "reason":"idle_token","fetched_at":"2026-10-01T08:00:00Z","refresh_expires_at":null,"desks":[],"windows":[]}],
     "policy":{"mode":"failover","threshold_pct":90,"failover_allowed":true,"default":"main"}}
    """#

    func testAccountsAndPolicyDecode() throws {
        let u = try usage(both)
        XCTAssertEqual(u.accounts.map(\.id), ["main", "work"])
        let main = u.accounts[0]
        XCTAssertEqual(main.label, "Personal")
        XCTAssertEqual(main.plan, "Max 20×", "the plan object reads as its name")
        XCTAssertEqual(main.windows.map(\.key), ["session", "weekly_all"])
        XCTAssertEqual(main.desks, ["chief", "scout"])
        XCTAssertEqual(main.refreshExpiresAt, Date(timeIntervalSince1970: 1_793_000_000))
        let work = u.accounts[1]
        XCTAssertEqual(work.plan, "pro", "a bare string plan still reads")
        XCTAssertFalse(work.available)
        XCTAssertTrue(work.stale)
        XCTAssertEqual(work.reason, "idle_token")
        XCTAssertNotNil(work.fetchedAt, "ISO text dates read too")
        XCTAssertNil(work.refreshExpiresAt)
        XCTAssertEqual(u.policy?.mode, "failover")
        XCTAssertEqual(u.policy?.thresholdPct, 90)
        XCTAssertEqual(u.policy?.isAutoSwitch, true)
    }

    func testAnOlderDecksUsageDecodesUnchanged() throws {
        let u = try usage(#"""
        {"available":true,"stale":false,"reason":null,"plan":{"name":"Max 20×"},
         "windows":[{"key":"session","label":"5-hour","percent":38,"severity":"normal"}]}
        """#)
        XCTAssertEqual(u.plan, "Max 20×")
        XCTAssertEqual(u.windows.count, 1)
        XCTAssertEqual(u.accounts, [])
        XCTAssertNil(u.policy)
    }

    func testAFixedPolicyIsAutoSwitchOffAndAJunkAccountDoesNotBlankTheMeter() throws {
        let u = try usage(#"""
        {"available":true,"windows":[],"policy":{"mode":"fixed"},
         "accounts":[{"label":"no id"},{"id":"ok","windows":[]}]}
        """#)
        XCTAssertEqual(u.policy?.isAutoSwitch, false)
        XCTAssertEqual(u.accounts.map(\.id), ["ok"], "an account with no id is dropped, the rest read")
        XCTAssertEqual(u.accounts.first?.label, "ok", "no label falls back to the id")
    }

    // MARK: agent rows

    private func agent(_ extra: String) throws -> Agent {
        try DeckCoding.decoder.decode(Agent.self, from: Data(#"{"name":"scout","state":"IDLE"\#(extra)}"#.utf8))
    }

    func testAnAgentRowCarriesItsAccountAndTheOneItRunsOn() throws {
        let a = try agent(#","account":"work","running_account":"main""#)
        XCTAssertEqual(a.account, "work")
        XCTAssertEqual(a.runningAccount, "main")
    }

    func testAnOldRowHasNoAccountAndAnEmptyOrNullOneMeansDefault() throws {
        XCTAssertNil(try agent("").account)
        XCTAssertNil(try agent("").runningAccount)
        XCTAssertNil(try agent(#","account":"","running_account":null"#).account)
    }

    // MARK: routes

    private let base = URL(string: "https://deck.local")!

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: base, tokens: store, performer: performer)
    }

    func testTheAccountListReadsBothShapes() async throws {
        let wrapped = StubPerformer()
        wrapped.bodies["/v1/accounts"] = Data(#"{"accounts":[{"id":"main","label":"Personal","kind":"subscription","default":true}]}"#.utf8)
        let a = try await client(wrapped).accounts()
        XCTAssertEqual(a?.map(\.id), ["main"])
        XCTAssertEqual(wrapped.requests.first?.httpMethod, "GET")

        let bare = StubPerformer()
        bare.bodies["/v1/accounts"] = Data(#"[{"id":"work","label":"Work"}]"#.utf8)
        let b = try await client(bare).accounts()
        XCTAssertEqual(b?.map(\.label), ["Work"])
    }

    func testADeckWithoutTheAccountsRouteHasNoAccountsRatherThanAnError() async throws {
        let performer = StubPerformer()
        performer.status = 404
        let none = try await client(performer).accounts()
        XCTAssertNil(none)
    }

    func testMovingPostsTheAccountToTheDesksRoute() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents/travel scout/account"] = Data(#"{"ok":true}"#.utf8)
        try await client(performer).move(agent: "travel scout", to: "work")
        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.absoluteString, "https://deck.local/v1/agents/travel%20scout/account")
        let body = try XCTUnwrap(request.httpBody)
        XCTAssertEqual(try JSONSerialization.jsonObject(with: body) as? [String: String], ["account": "work"])
    }

    private func refusal(status: Int, body: String) async -> AccountMoveError? {
        let performer = StubPerformer()
        performer.status = status
        performer.defaultBody = Data(body.utf8)
        do { try await client(performer).move(agent: "scout", to: "work"); return nil }
        catch { return error as? AccountMoveError }
    }

    func testEachRefusalKeepsItsOwnMeaningAndTheDecksSentence() async {
        let busy = await refusal(status: 409, body: #"{"ok":false,"reason":"not_idle","detail":"scout is mid-turn"}"#)
        XCTAssertEqual(busy, .notIdle(detail: "scout is mid-turn"))
        let gone = await refusal(status: 404, body: #"{"ok":false,"reason":"no_account","detail":"no account work"}"#)
        XCTAssertEqual(gone, .noAccount(detail: "no account work"))
        let failed = await refusal(status: 502, body: #"{"ok":false,"reason":"move_failed","detail":"resume failed; back on main"}"#)
        XCTAssertEqual(failed, .moveFailed(detail: "resume failed; back on main"))
    }

    func testTheReasonDecidesAndNotTheStatus() async {
        // 404 is also `unknown_agent`; it must not read as "no such account".
        let unknown = await refusal(status: 404, body: #"{"ok":false,"reason":"unknown_agent"}"#)
        XCTAssertEqual(unknown, .other(.unknownAgent))
        // A bare 404 is a deck that never mounted the route.
        let old = await refusal(status: 404, body: "{}")
        XCTAssertEqual(old, .unsupported)
        // A bare 409 / 502 still reads, off the status, when no reason came.
        let bare409 = await refusal(status: 409, body: "{}")
        XCTAssertEqual(bare409, .notIdle(detail: ""))
        let bare502 = await refusal(status: 502, body: "not json")
        XCTAssertEqual(bare502, .moveFailed(detail: ""))
    }

    func testEveryRefusalHasASentenceHeCanActOn() {
        let all: [AccountMoveError] = [.notIdle(detail: ""), .noAccount(detail: ""), .moveFailed(detail: ""), .unsupported]
        for e in all {
            XCTAssertFalse(e.userFacingText.isEmpty)
        }
        XCTAssertTrue(AccountMoveError.moveFailed(detail: "").userFacingText.contains("still"),
                      "a failed move says the desk stayed where it was")
        XCTAssertTrue(AccountMoveError.notIdle(detail: "scout is mid-turn").userFacingText.contains("mid-turn"),
                      "the deck's own detail is kept")
    }
}

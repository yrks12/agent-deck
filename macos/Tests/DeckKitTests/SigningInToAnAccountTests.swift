import XCTest
@testable import DeckKit

/// **Signing in to a Claude account from the app.** Contract (the backend
/// builder's, not yet live): `POST /v1/accounts/login {id,label}` answers
/// `{login_id, url}`; `POST /v1/accounts/login/{login_id}/code {code}`;
/// `GET /v1/accounts/login/{login_id}` answers `waiting_code | done | failed |
/// expired`. ASSUMED: the refusal reasons below (`bad_code`, `expired`,
/// `account_exists`, `login_failed`) - the contract names none, so each also
/// reads off the status when no reason comes.
///
/// The flow is a state machine in DeckKit so the Mac and the iPhone cannot
/// disagree about it: start -> browser opens -> paste the code -> poll -> done.
@MainActor
final class SigningInToAnAccountTests: XCTestCase {

    // MARK: a client that answers from a script

    final class Script: AccountLoginClient, @unchecked Sendable {
        var startError: AccountLoginError?
        var codeError: AccountLoginError?
        var statuses: [AccountLoginPhase] = [.done]
        var detail: String?
        private(set) var started: [(id: String, label: String)] = []
        private(set) var codes: [(loginID: String, code: String)] = []
        private(set) var polls = 0
        private var next = 0

        func startLogin(id: String, label: String) async throws -> AccountLoginStart {
            if let startError { throw startError }
            started.append((id, label))
            return AccountLoginStart(loginID: "L\(started.count)", url: URL(string: "https://claude.ai/oauth/x\(started.count)")!)
        }
        func submitLoginCode(loginID: String, code: String) async throws {
            if let codeError { throw codeError }
            codes.append((loginID, code))
        }
        func loginStatus(loginID: String) async throws -> AccountLoginStatus {
            polls += 1
            let phase = statuses[min(next, statuses.count - 1)]
            next += 1
            return AccountLoginStatus(status: phase, detail: detail)
        }
    }

    private var opened: [URL] = []
    private var finished = 0
    private var sleeps = 0

    private func model(_ script: Script, maxPolls: Int = 5) -> AccountLoginModel {
        AccountLoginModel(client: script, id: "work", label: "Work",
                          openURL: { [unowned self] in opened.append($0) },
                          onDone: { [unowned self] in finished += 1 },
                          sleep: { [unowned self] in sleeps += 1 },
                          maxPolls: maxPolls)
    }

    // MARK: the happy path

    func testStartingAsksTheDeckAndOpensTheBrowserThenWaitsForTheCode() async {
        let script = Script()
        let m = model(script)
        XCTAssertEqual(m.state, .idle)

        await m.start()

        XCTAssertEqual(script.started.map(\.id), ["work"])
        XCTAssertEqual(script.started.map(\.label), ["Work"])
        XCTAssertEqual(opened, [URL(string: "https://claude.ai/oauth/x1")!])
        XCTAssertEqual(m.state, .awaitingCode(problem: nil))
    }

    func testTheCodeIsTrimmedSentAndPolledUntilDone() async {
        let script = Script()
        script.statuses = [.waitingCode, .waitingCode, .done]
        let m = model(script)
        await m.start()

        await m.submit(code: "  abc#123 \n")

        XCTAssertEqual(script.codes.map(\.code), ["abc#123"])
        XCTAssertEqual(script.codes.map(\.loginID), ["L1"])
        XCTAssertEqual(script.polls, 3)
        XCTAssertEqual(sleeps, 2, "it waits between polls, not after the last")
        XCTAssertEqual(m.state, .done)
        XCTAssertEqual(finished, 1, "accounts and meters refresh exactly once")
    }

    // MARK: not the happy path

    func testAnEmptyCodeIsRefusedHereAndNothingIsSent() async {
        let script = Script()
        let m = model(script)
        await m.start()

        await m.submit(code: "   ")

        XCTAssertTrue(script.codes.isEmpty)
        guard case .awaitingCode(let problem?) = m.state else { return XCTFail("\(m.state)") }
        XCTAssertTrue(problem.lowercased().contains("paste"))
    }

    func testAWrongCodeReturnsToTheCodeFieldWithTheDecksSentence() async {
        let script = Script()
        script.codeError = .badCode(detail: "That code is not right")
        let m = model(script)
        await m.start()

        await m.submit(code: "nope")

        XCTAssertEqual(script.polls, 0)
        guard case .awaitingCode(let problem?) = m.state else { return XCTFail("\(m.state)") }
        XCTAssertTrue(problem.contains("not right"), problem)
        XCTAssertEqual(finished, 0)
    }

    func testAFailedLoginSaysWhyAndRetryStartsAFreshOne() async {
        let script = Script()
        script.statuses = [.failed]
        script.detail = "Claude refused the code"
        let m = model(script)
        await m.start()
        await m.submit(code: "abc")
        guard case .failed(let why) = m.state else { return XCTFail("\(m.state)") }
        XCTAssertTrue(why.contains("refused"), why)
        XCTAssertEqual(finished, 0)

        script.statuses = [.done]
        await m.retry()

        XCTAssertEqual(script.started.count, 2, "a retry is a new login, not the dead one")
        XCTAssertEqual(opened.count, 2)
        XCTAssertEqual(m.state, .awaitingCode(problem: nil))
    }

    func testAnExpiredLoginOffersARetryToo() async {
        let script = Script()
        script.statuses = [.expired]
        let m = model(script)
        await m.start()
        await m.submit(code: "abc")
        XCTAssertEqual(m.state, .expired)
        await m.retry()
        XCTAssertEqual(script.started.count, 2)
    }

    func testAServerThatNeverAnswersDoneEndsInATimeoutNotASpinner() async {
        let script = Script()
        script.statuses = [.waitingCode]
        let m = model(script, maxPolls: 3)
        await m.start()
        await m.submit(code: "abc")
        XCTAssertEqual(script.polls, 3)
        guard case .failed = m.state else { return XCTFail("\(m.state)") }
    }

    func testADeckWithoutTheRoutesSaysSoAndOpensNothing() async {
        let script = Script()
        script.startError = .unsupported
        let m = model(script)
        await m.start()
        XCTAssertEqual(m.state, .failed(AccountLoginError.unsupported.userFacingText))
        XCTAssertTrue(opened.isEmpty)
    }

    func testAnExpiredLoginOnSubmitIsExpiredNotWrongCode() async {
        let script = Script()
        script.codeError = .expired
        let m = model(script)
        await m.start()
        await m.submit(code: "abc")
        XCTAssertEqual(m.state, .expired)
    }

    func testSubmittingBeforeStartingDoesNothing() async {
        let script = Script()
        let m = model(script)
        await m.submit(code: "abc")
        XCTAssertTrue(script.codes.isEmpty)
        XCTAssertEqual(m.state, .idle)
    }

    // MARK: the HTTP routes and their refusals

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    func testTheThreeRoutes() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/accounts/login"] = Data(#"{"login_id":"abc","url":"https://claude.ai/o"}"#.utf8)
        performer.bodies["/v1/accounts/login/abc"] = Data(#"{"status":"waiting_code"}"#.utf8)
        let http = client(performer)

        let start = try await http.startLogin(id: "work", label: "Work")
        XCTAssertEqual(start.loginID, "abc")
        XCTAssertEqual(start.url.absoluteString, "https://claude.ai/o")
        try await http.submitLoginCode(loginID: "abc", code: "c#1")
        let status = try await http.loginStatus(loginID: "abc")
        XCTAssertEqual(status.status, .waitingCode)

        XCTAssertEqual(performer.requests.map { $0.httpMethod ?? "" }, ["POST", "POST", "GET"])
        XCTAssertEqual(performer.paths, ["/v1/accounts/login", "/v1/accounts/login/abc/code", "/v1/accounts/login/abc"])
        let first = try JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests[0].httpBody)) as? [String: String]
        XCTAssertEqual(first, ["id": "work", "label": "Work"])
        let second = try JSONSerialization.jsonObject(with: try XCTUnwrap(performer.requests[1].httpBody)) as? [String: String]
        XCTAssertEqual(second, ["code": "c#1"])
    }

    func testAnUnknownStatusReadsAsFailedNotAsWaiting() throws {
        let s = try DeckCoding.decoder.decode(AccountLoginStatus.self, from: Data(#"{"status":"exploded"}"#.utf8))
        XCTAssertEqual(s.status, .failed)
    }

    private func refusal(_ status: Int, _ body: String) async -> AccountLoginError? {
        let performer = StubPerformer()
        performer.status = status
        performer.defaultBody = Data(body.utf8)
        do { _ = try await client(performer).startLogin(id: "w", label: "W"); return nil }
        catch { return error as? AccountLoginError }
    }

    func testRefusalsMapByReasonThenByStatus() async {
        let missing = await refusal(404, "{}")
        XCTAssertEqual(missing, .unsupported, "a bare 404 is a deck without the routes")
        let exists = await refusal(409, #"{"reason":"account_exists","detail":"work is already signed in"}"#)
        XCTAssertEqual(exists, .alreadyExists(detail: "work is already signed in"))
        let bad = await refusal(422, #"{"reason":"bad_code","detail":"wrong"}"#)
        XCTAssertEqual(bad, .badCode(detail: "wrong"))
        let gone = await refusal(410, "{}")
        XCTAssertEqual(gone, .expired)
        let gone2 = await refusal(404, #"{"reason":"expired"}"#)
        XCTAssertEqual(gone2, .expired)
        let failed = await refusal(502, #"{"reason":"login_failed","detail":"claude exited"}"#)
        XCTAssertEqual(failed, .failed(detail: "claude exited"))
        let auth = await refusal(401, #"{"reason":"unauthorized"}"#)
        XCTAssertEqual(auth, .other(.unauthorized))
    }

    func testEveryErrorHasASentence() {
        let all: [AccountLoginError] = [.unsupported, .badCode(detail: ""), .expired, .failed(detail: ""),
                                        .alreadyExists(detail: ""), .other(.unauthorized)]
        for e in all { XCTAssertFalse(e.userFacingText.isEmpty) }
    }

    // MARK: ids, and when the button shows

    func testANewAccountsIdIsADashedSlugOfItsLabelAndNeverAnExistingOne() {
        XCTAssertEqual(AccountSignIn.newID(for: "Work Pro", existing: ["main"]), "work-pro")
        XCTAssertEqual(AccountSignIn.newID(for: "  Work  ", existing: ["work"]), "work-2")
        XCTAssertEqual(AccountSignIn.newID(for: "Café ☕", existing: []), "caf")
        XCTAssertNil(AccountSignIn.newID(for: "  ☕ ", existing: []), "nothing usable in the label")
    }

    func testTheButtonsShowOnlyWhereTheDeckServesAccounts() {
        let none = ClaudeUsage(available: true, windows: [])
        XCTAssertFalse(AccountSignIn.isOffered(usage: none))
        XCTAssertFalse(AccountSignIn.isOffered(usage: nil))
        let one = ClaudeUsage(available: true, windows: [], accounts: [AccountUsage(id: "main")])
        XCTAssertTrue(AccountSignIn.isOffered(usage: one), "one account can still be signed in again, and a second added")
        let policy = ClaudeUsage(available: true, windows: [], policy: AccountPolicy(mode: "fixed"))
        XCTAssertTrue(AccountSignIn.isOffered(usage: policy))
    }
}

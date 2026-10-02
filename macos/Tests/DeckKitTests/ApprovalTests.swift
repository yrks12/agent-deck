import XCTest
@testable import DeckKit

/// The approval card is the most dangerous control in this app: one of its
/// buttons grants a *standing* permission on the owner's machine. So the rule these
/// tests exist for is structural, not cosmetic —
///
///   **an option cannot be constructed, let alone drawn, without the sentence
///   describing the rule it would write, and that sentence is on screen before
///   the tap.**
///
/// The deck's §12 sends that sentence as `summary` and the rule itself as
/// `rule`. This client refuses an option that arrives without them rather than
/// drawing a button it cannot explain.
final class ApprovalTests: XCTestCase {

    // MARK: fixtures — §12 verbatim

    private func optionJSON(
        reply: String,
        available: Bool = true,
        summary: String?,
        rule: String?
    ) -> String {
        var fields = ["\"reply\":\"\(reply)\"", "\"available\":\(available)"]
        fields.append("\"rule\":\(rule ?? "null")")
        if let summary { fields.append("\"summary\":\"\(summary)\"") }
        return "{\(fields.joined(separator: ","))}"
    }

    private let alwaysRule = """
    {"id":"ask-z47x8","kind":"always_allow","tool":"Bash","pattern":"gh pr*",
     "cwd":"/Users/sam/Projects/acme","note":"from chief on WhatsApp"}
    """

    private let denyRule = """
    {"id":"ask-z47x8","kind":"deny","tool":"Bash","pattern":"gh pr*",
     "cwd":"/Users/sam/Projects/acme","note":"from chief on WhatsApp"}
    """

    private func approvalJSON(
        id: String = "z47x8",
        agent: String = "grok bot",
        options: [String]? = nil
    ) -> String {
        let options = options ?? [
            optionJSON(reply: "once", summary: "just this time", rule: nil),
            optionJSON(reply: "always",
                       summary: "allow `gh pr*` in ~/Projects/acme, never ask again",
                       rule: alwaysRule),
            optionJSON(reply: "never",
                       summary: "refuse `gh pr*` in ~/Projects/acme from now on",
                       rule: denyRule),
        ]
        return """
        {"id":"\(id)","ts":1788225548.976903,"agent":"\(agent)","tool":"Bash",
         "subject":"gh pr create --title 'ship the routes'",
         "cwd":"/Users/sam/Projects/acme","cwd_short":"~/Projects/acme",
         "status":"pending","options":[\(options.joined(separator: ","))]}
        """
    }

    private func decodeApprovals(_ json: String) throws -> ApprovalsPage {
        try DeckCoding.decoder.decode(ApprovalsPage.self, from: Data(json.utf8))
    }

    private func oneApproval() throws -> Approval {
        let page = try decodeApprovals("{\"approvals\":[\(approvalJSON())]}")
        return try XCTUnwrap(page.approvals.first)
    }

    // MARK: the sentence is inseparable from the option

    func testEveryOptionCarriesTheSentenceForTheRuleItWouldWrite() throws {
        let approval = try oneApproval()

        XCTAssertEqual(approval.options.map(\.reply), [.once, .always, .never])
        for option in approval.options {
            XCTAssertFalse(option.ruleText.isEmpty, "\(option.label) has no sentence")
        }
    }

    /// The one string this whole feature is about. It is the deck's own
    /// `summary`, shown on the button's own row, before anything is tapped —
    /// §12 is explicit that the word alone ("Always allow") is how somebody
    /// grants far more than they meant to.
    func testAlwaysAllowStatesItsStandingRuleBeforeItIsTapped() throws {
        let always = try XCTUnwrap(try oneApproval().options.first { $0.reply == .always })

        XCTAssertEqual(always.label, "Always allow")
        XCTAssertEqual(always.ruleText, "allow `gh pr*` in ~/Projects/acme, never ask again")
        XCTAssertEqual(always.rule?.pattern, "gh pr*", "the rule itself travels with the button")
        XCTAssertTrue(always.isStanding)
    }

    func testAllowOnceAndDenySayWhatTheyDoToo() throws {
        let approval = try oneApproval()
        let once = try XCTUnwrap(approval.options.first { $0.reply == .once })
        let never = try XCTUnwrap(approval.options.first { $0.reply == .never })

        XCTAssertEqual(once.label, "Allow once")
        XCTAssertEqual(once.ruleText, "just this time")
        XCTAssertFalse(once.isStanding, "`once` writes no rule at all")
        XCTAssertEqual(never.label, "Deny")
        XCTAssertEqual(never.ruleText, "refuse `gh pr*` in ~/Projects/acme from now on")
    }

    /// An option with no sentence cannot be built, so there is no path to a
    /// button that grants an unstated permission.
    func testAnOptionWithNoSentenceCannotBeBuilt() throws {
        let page = try decodeApprovals("""
        {"approvals":[\(approvalJSON(id: "bad", options: [
            optionJSON(reply: "once", summary: "just this time", rule: nil),
            optionJSON(reply: "always", summary: nil, rule: alwaysRule),
        ])),\(approvalJSON(id: "good"))]}
        """)

        XCTAssertEqual(page.approvals.map(\.id), ["good"])
        XCTAssertEqual(page.unreadable, 1)
    }

    /// A standing grant whose rule did not arrive is the same danger by
    /// another route: the sentence would be there and the thing it describes
    /// would not.
    func testAnAvailableStandingOptionWithNoRuleIsRefused() throws {
        let page = try decodeApprovals("""
        {"approvals":[\(approvalJSON(id: "bad", options: [
            optionJSON(reply: "once", summary: "just this time", rule: nil),
            optionJSON(reply: "always", summary: "allow everything", rule: nil),
            optionJSON(reply: "never", summary: "refuse it", rule: denyRule),
        ]))]}
        """)

        XCTAssertTrue(page.approvals.isEmpty)
        XCTAssertEqual(page.unreadable, 1)
        XCTAssertEqual(
            page.unreadableNotice,
            "1 approval request could not be read and is not shown here. Answer it in the agent's own terminal."
        )
    }

    /// §12: credentials, payments and irreversible actions ask every time. The
    /// option is drawn disabled **with its reason**, never silently greyed.
    func testAnUnavailableAlwaysOptionKeepsItsExplanationAndIsNotOfferable() throws {
        let page = try decodeApprovals("""
        {"approvals":[\(approvalJSON(options: [
            optionJSON(reply: "once", summary: "just this time", rule: nil),
            optionJSON(reply: "always", available: false,
                       summary: "not available here (credential, payment or irreversible)",
                       rule: nil),
            optionJSON(reply: "never", summary: "refuse it from now on", rule: denyRule),
        ]))]}
        """)

        let always = try XCTUnwrap(page.approvals.first?.options.first { $0.reply == .always })
        XCTAssertFalse(always.isAvailable)
        XCTAssertEqual(always.ruleText, "not available here (credential, payment or irreversible)")
        XCTAssertEqual(page.unreadable, 0, "an unavailable option is a legal payload, not a broken one")
    }

    func testAReadablePageHasNoUnreadableNotice() throws {
        let page = try decodeApprovals("{\"approvals\":[\(approvalJSON())]}")

        XCTAssertEqual(page.unreadable, 0)
        XCTAssertNil(page.unreadableNotice)
    }

    // MARK: the card the reference draws

    func testTheCardSaysWhoseComputerTheToolWouldRunOn() throws {
        let card = ApprovalCard.make(
            approval: try oneApproval(),
            agents: ["grok bot": makeAgent("grok bot", title: "Researcher")]
        )

        XCTAssertEqual(card.runsOn, "Runs on Grok Bot's computer")
    }

    func testTheCardExplainsWhyItIsAskingAndHidesTheRawCommandBehindADisclosure() throws {
        let card = ApprovalCard.make(approval: try oneApproval(), agents: [:])

        XCTAssertEqual(card.title, "gh pr create --title 'ship the routes'")
        XCTAssertEqual(
            card.why,
            "Grok Bot stopped here: this Bash call is not covered by its rules. It would run in ~/Projects/acme."
        )
        XCTAssertEqual(card.disclosureTitle, "Show the details")
        XCTAssertTrue(card.details.contains("gh pr create --title 'ship the routes'"), card.details)
        XCTAssertTrue(card.details.contains("/Users/sam/Projects/acme"), card.details)
        XCTAssertEqual(card.options.map(\.label), ["Allow once", "Always allow", "Deny"])
    }

    func testACardIsAddressedToTheAgentsOwnThread() throws {
        let approval = try oneApproval()

        XCTAssertEqual(approval.agentName, "grok bot")
        XCTAssertEqual(approval.threadID, "direct:grok bot")
    }

    // MARK: answering one

    /// §12's body is exactly one key. Sending anything else invites a deck that
    /// trusts the client about what it is granting.
    func testAnsweringSendsTheReplyAndOnlyTheReply() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/approvals/z47x8"] = Data(#"{"ok":true,"ask":{},"rule":null}"#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)
        let always = try XCTUnwrap(try oneApproval().options.first { $0.reply == .always })

        try await client.decideApproval(id: "z47x8", option: always)

        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/v1/approvals/z47x8")
        let json = try XCTUnwrap(
            JSONSerialization.jsonObject(with: try XCTUnwrap(request.httpBody)) as? [String: Any]
        )
        XCTAssertEqual(json.keys.sorted(), ["reply"])
        XCTAssertEqual(json["reply"] as? String, "always")
    }

    func testAnUnavailableOptionIsNeverSent() async throws {
        let performer = StubPerformer()
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)
        let page = try decodeApprovals("""
        {"approvals":[\(approvalJSON(options: [
            optionJSON(reply: "once", summary: "just this time", rule: nil),
            optionJSON(reply: "always", available: false,
                       summary: "not available here (credential, payment or irreversible)",
                       rule: nil),
        ]))]}
        """)
        let blocked = try XCTUnwrap(page.approvals.first?.options.first { $0.reply == .always })

        do {
            try await client.decideApproval(id: "z47x8", option: blocked)
            XCTFail("an option the deck says is unavailable must not be sent")
        } catch {
            XCTAssertTrue(performer.requests.isEmpty)
        }
    }

    func testTheApprovalsListIsOneGet() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/approvals"] = Data("{\"approvals\":[\(approvalJSON())]}".utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)

        let page = try await client.approvals()

        XCTAssertEqual(performer.paths, ["/v1/approvals"])
        XCTAssertEqual(page.approvals.map(\.id), ["z47x8"])
    }

    // MARK: what the deck says it did — §12's response, verbatim

    /// The 200 from `POST /v1/approvals/{id}`, copied out of the contract. This
    /// used to be discarded whole (`_ = try await raw(request)`), so a tap that
    /// wrote a standing permission on his Mac left nothing on screen saying so.
    ///
    /// Three separate facts, and the card reads differently for each: **which**
    /// reply the deck recorded, **what rule** it wrote, and whether the desk was
    /// actually **resumed**. Getting `answered` from the wrong place is not a
    /// crash — it is a card that quietly says "Allowed once" for a permanent
    /// grant, which is the worst failure this screen has.
    func testTheAnswerCarriesTheReply_TheRuleItWrote_AndWhetherTheDeskMoved() throws {
        let wire = """
        {
          "ok": true,
          "ask": {"id": "z47x8", "ts": 1788225548.976903, "agent": "chief",
                  "tool": "Bash", "subject": "gh pr create --title 'ship the routes'",
                  "cwd": "/Users/sam/Projects/acme", "answered": "always",
                  "expired_at": null, "status": "answered"},
          "rule": {"id": "ask-z47x8", "kind": "always_allow", "tool": "Bash",
                   "pattern": "gh pr*", "cwd": "/Users/sam/Projects/acme",
                   "note": "from chief on WhatsApp"},
          "resumed": true
        }
        """

        let decision = try DeckCoding.decoder.decode(
            ApprovalDecision.self, from: Data(wire.utf8))

        XCTAssertEqual(decision.answered, "always",
                       "read off `ask.answered`, not off the button that was pressed")
        XCTAssertEqual(decision.rule?.pattern, "gh pr*")
        XCTAssertEqual(decision.rule?.kind, "always_allow")
        XCTAssertTrue(decision.resumed, "§12: the desk was told to carry on with it")
    }

    /// A `once` answer writes no rule, and a deck that predates `resumed`
    /// simply omits it. Neither is an error, and neither may be read as "the
    /// desk was restarted" — claiming work moved when nothing was told to move
    /// is the one thing `resumed` exists to stop.
    func testAnAnswerWithNoRuleAndNoResumedFieldIsReadHonestly() throws {
        let decision = try DeckCoding.decoder.decode(
            ApprovalDecision.self,
            from: Data(#"{"ok":true,"ask":{"answered":"once"},"rule":null}"#.utf8))

        XCTAssertEqual(decision.answered, "once")
        XCTAssertNil(decision.rule)
        XCTAssertFalse(decision.resumed,
                       "an absent `resumed` must not be read as a desk that moved")
    }

    /// And the adapter has to return it, not swallow it.
    func testTheHTTPAdapterHandsBackWhatTheDeckAnswered() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/approvals/z47x8"] = Data(#"""
        {"ok":true,"ask":{"answered":"never"},
         "rule":{"kind":"deny","tool":"Bash","pattern":"gh pr*","cwd":"/x"},
         "resumed":false}
        """#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!,
                                    tokens: tokens, performer: performer)
        let never = try XCTUnwrap(oneApproval().options.first { $0.reply == .never })

        let decision = try await client.decideApproval(id: "z47x8", option: never)

        XCTAssertEqual(decision.answered, "never")
        XCTAssertEqual(decision.rule?.kind, "deny")
        XCTAssertFalse(decision.resumed, "a refusal grants nothing and restarts nobody")
    }

    // MARK: refusals that are not successes

    /// §12: a reply landing on a settled question must not look like it decided
    /// something. Four refusals, four sentences.
    func testTheApprovalRefusalsEachSayWhatActuallyHappened() {
        func refusal(_ reason: String, _ detail: String) -> String {
            DeckError(
                status: 409,
                body: Data(#"{"ok":false,"reason":"\#(reason)","detail":"\#(detail)"}"#.utf8)
            ).userFacingText
        }

        let answered = refusal("already_answered", "answered with 'always'")
        let expired = refusal("expired", "asked more than four hours ago")
        let notAvailable = refusal("always_not_available", "credential")
        let badReply = refusal("bad_reply", "'maybe'")

        XCTAssertEqual(Set([answered, expired, notAvailable, badReply]).count, 4)
        XCTAssertTrue(answered.contains("already answered"), answered)
        XCTAssertTrue(expired.contains("expired"), expired)
        XCTAssertTrue(notAvailable.lowercased().contains("every time"), notAvailable)
        // The good signal, stated: an expiry decided nothing in either
        // direction, and the sentence has to say so.
        XCTAssertTrue(expired.contains("nothing was granted or refused"), expired)
        XCTAssertTrue(answered.contains("Your tap changed nothing"), answered)
    }

    // MARK: the model that holds them

    func testAnAnsweredApprovalLeavesTheThreadItWasAskedIn() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try decodeApprovals("{\"approvals\":[\(approvalJSON())]}")
        let model = ApprovalsModel(client: client)

        await model.load()
        let waiting = await model.pending(forAgent: "grok bot")
        XCTAssertEqual(waiting.count, 1)

        let approval = try XCTUnwrap(waiting.first)
        let never = try XCTUnwrap(approval.options.first { $0.reply == .never })
        await model.answer(approval: approval, with: never)

        let afterwards = await model.pending(forAgent: "grok bot")
        XCTAssertEqual(afterwards.count, 0)
        XCTAssertEqual(client.decidedApprovals.map(\.0), ["z47x8"])
        XCTAssertEqual(client.decidedApprovals.map(\.1), ["never"])
    }

    /// A refused answer puts the card back rather than pretending it was
    /// decided: a permission that silently failed to register is a lie.
    func testAFailedAnswerKeepsTheCardAndSaysWhy() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try decodeApprovals("{\"approvals\":[\(approvalJSON())]}")
        client.decideError = .queueFailed
        let model = ApprovalsModel(client: client)
        await model.load()
        let queued = await model.pending(forAgent: "grok bot")
        let approval = try XCTUnwrap(queued.first)

        await model.answer(approval: approval, with: approval.options[0])

        let stillWaiting = await model.pending(forAgent: "grok bot")
        let problem = await model.problem
        XCTAssertEqual(stillWaiting.count, 1, "still waiting")
        XCTAssertEqual(problem, DeckError.queueFailed.userFacingText)
    }

    /// A question that was already settled elsewhere is gone — but the card
    /// still says which way it went, rather than looking answered by this tap.
    func testAQuestionSettledElsewhereLeavesTheListWithItsOwnExplanation() async throws {
        let client = ScriptedDeckClient()
        client.approvalsPage = try decodeApprovals("{\"approvals\":[\(approvalJSON())]}")
        client.decideError = DeckError(
            status: 409,
            body: Data(#"{"ok":false,"reason":"already_answered","detail":"answered with 'always'"}"#.utf8)
        )
        let model = ApprovalsModel(client: client)
        await model.load()
        let queued = await model.pending(forAgent: "grok bot")
        let approval = try XCTUnwrap(queued.first)

        await model.answer(approval: approval, with: approval.options[0])

        let left = await model.pending(forAgent: "grok bot")
        let problem = await model.problem
        XCTAssertTrue(left.isEmpty, "the question is settled; the card must not linger")
        XCTAssertEqual(problem?.contains("already answered"), true)
    }

    func testAFailedLoadIsReportedRatherThanLookingLikeNoApprovals() async throws {
        let client = ScriptedDeckClient()
        client.approvalsError = .transport("no route to host")
        let model = ApprovalsModel(client: client)

        await model.load()

        let problem = await model.problem
        let waiting = await model.pending(forAgent: "grok bot")
        XCTAssertEqual(problem, DeckError.transport("no route to host").userFacingText)
        XCTAssertEqual(waiting.count, 0)
    }
}

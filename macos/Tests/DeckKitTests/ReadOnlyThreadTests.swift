import XCTest
@testable import DeckKit

/// Failure mode being pinned: a peer thread (agent talking to agent) shows a
/// composer, the user types into it, and the server rejects the post. The user
/// should never have been offered the box.
///
/// The read-only flag comes off the thread payload. It is never inferred from
/// "this thread has two participants" — both directions of that guess are
/// pinned below.
final class ReadOnlyThreadTests: XCTestCase {

    private let agents: [String: Agent] = [
        "Chief": Agent(name: "Chief", title: "The Builder", detail: "", section: "Work"),
        "Hemingway": Agent(name: "Hemingway", title: "Designer", detail: "", section: "Work"),
    ]

    // MARK: the negative case, stated positively

    func testAPeerThreadOffersAViewOnlyFooterInsteadOfAComposer() {
        let presentation = ThreadPresentation.make(
            threadID: "t9",
            participants: ["Chief", "Hemingway"],
            isReadOnly: true,
            agents: agents
        )

        XCTAssertEqual(
            presentation.composer,
            .viewOnly(footer: "This chat is view-only", closeButtonTitle: "Close Chat"),
            "a peer thread must present the footer, which is what replaces the composer"
        )
    }

    func testAPeerThreadHeaderNamesBothAgents() {
        let presentation = ThreadPresentation.make(
            threadID: "t9",
            participants: ["Chief", "Hemingway"],
            isReadOnly: true,
            agents: agents
        )

        XCTAssertEqual(presentation.headerTitle, "Chief ⇄ Hemingway")
    }

    // MARK: the paired positive

    func testADirectThreadOffersAComposer() {
        let presentation = ThreadPresentation.make(
            threadID: "t1",
            participants: ["Hemingway"],
            isReadOnly: false,
            agents: agents
        )

        XCTAssertEqual(presentation.composer, .enabled(placeholder: "Message Hemingway"))
        XCTAssertEqual(presentation.headerTitle, "Hemingway")
        XCTAssertEqual(presentation.headerSubtitle, "Designer")
    }

    // MARK: the flag is the only authority

    func testTwoParticipantsAloneDoNotMakeAThreadReadOnly() {
        let presentation = ThreadPresentation.make(
            threadID: "t5",
            participants: ["Chief", "Hemingway"],
            isReadOnly: false,
            agents: agents
        )

        XCTAssertEqual(
            presentation.composer,
            .enabled(placeholder: "Message Chief"),
            "read-only is a payload flag; participant count must not be used to guess it"
        )
    }

    func testOneParticipantWithTheFlagSetIsStillReadOnly() {
        let presentation = ThreadPresentation.make(
            threadID: "t6",
            participants: ["Hemingway"],
            isReadOnly: true,
            agents: agents
        )

        XCTAssertEqual(
            presentation.composer,
            .viewOnly(footer: "This chat is view-only", closeButtonTitle: "Close Chat")
        )
    }

    // MARK: the send path behind the composer

    func testSubmittingToAReadOnlyThreadIsRefusedByTheClientBeforeTheNetwork() async {
        let client = ScriptedDeckClient()
        client.sendResult = makeMessage("mX", cursor: "0099-mX")
        let composer = ThreadComposer(client: client, threadID: "t9", isReadOnly: true)

        let outcome = await composer.submit("let me in")

        XCTAssertEqual(outcome, .refusedViewOnly)
        XCTAssertEqual(client.sendCallCount, 0, "no POST is attempted for a view-only thread")
    }

    func testSubmittingToADirectThreadSendsExactlyOnce() async {
        let client = ScriptedDeckClient()
        let expected = makeMessage("mX", cursor: "0099-mX", text: "on it", author: "owner", role: .owner)
        client.sendResult = expected
        let composer = ThreadComposer(client: client, threadID: "t1", isReadOnly: false)

        let outcome = await composer.submit("on it")

        XCTAssertEqual(outcome, .sent(expected))
        XCTAssertEqual(client.sendCallCount, 1)
    }

    func testWhitespaceOnlyTextIsRefusedRatherThanPosted() async {
        let client = ScriptedDeckClient()
        client.sendResult = makeMessage("mX", cursor: "0099-mX")
        let composer = ThreadComposer(client: client, threadID: "t1", isReadOnly: false)

        let outcome = await composer.submit("   \n ")

        XCTAssertEqual(outcome, .refusedEmpty)
        XCTAssertEqual(client.sendCallCount, 0)
    }

    func testTheReadOnlyFlagIsCarriedOffTheWireOntoThePresentation() throws {
        let json = Data("""
        {
          "thread_id": "t9",
          "participants": ["Chief", "Hemingway"],
          "read_only": true,
          "messages": []
        }
        """.utf8)

        let page = try DeckCoding.decoder.decode(MessagePage.self, from: json)

        XCTAssertTrue(page.isReadOnly, "read_only on the payload is what the UI must obey")
        XCTAssertEqual(page.participants, ["Chief", "Hemingway"])
    }
}

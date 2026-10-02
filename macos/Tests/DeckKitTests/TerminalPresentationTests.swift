import XCTest
@testable import DeckKit

/// What the terminal's bar says, written once for the Mac and the phone.
final class TerminalPresentationTests: XCTestCase {

    func testTheWindowsHaveNamesAPersonReads() {
        XCTAssertEqual(TerminalWindow.deck.title, "My shell")
        XCTAssertEqual(TerminalWindow.agent.title, "Agent's commands")
    }

    func testEveryStateSaysWhatItIs() {
        XCTAssertEqual(TerminalStreamState.connecting.caption(desk: "Atlas"), "Connecting to Atlas's terminal…")
        XCTAssertNil(TerminalStreamState.live.caption(desk: "Atlas"), "a live terminal needs no caption")
        XCTAssertEqual(TerminalStreamState.readOnly.caption(desk: "Atlas"),
                       "Watching what Atlas runs. Read-only.")
        XCTAssertEqual(TerminalStreamState.ended(code: 0).caption(desk: "Atlas"), "The shell ended.")
        XCTAssertEqual(TerminalStreamState.ended(code: 2).caption(desk: "Atlas"), "The shell ended (exit 2).")
        XCTAssertEqual(TerminalStreamState.failed(.unauthorized).caption(desk: "Atlas"),
                       TerminalStreamFailure.unauthorized.sentence)
    }

    func testOnlyAnEndOrAFailureOffersToStartAgain() {
        XCTAssertTrue(TerminalStreamState.ended(code: 0).offersRestart)
        XCTAssertTrue(TerminalStreamState.failed(.computerNotRunning("")).offersRestart)
        XCTAssertFalse(TerminalStreamState.live.offersRestart)
        XCTAssertFalse(TerminalStreamState.connecting.offersRestart)
        XCTAssertFalse(TerminalStreamState.readOnly.offersRestart)
    }
}

import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"Atlas's terminal — Command, in /home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17"**
///
/// MEASURED on the box, 2026-09-30. `POST /v1/agents/{name}/terminal` runs
/// inside the desk's own computer (`docker exec` into `deck-desk-<name>`,
/// whose home is `/home/agent`). The panel opened its prompt at the desk's
/// *host* workspace path instead, which does not exist inside that container,
/// so the very first command on every desk answered:
///
///     OCI runtime exec failed: ... chdir to cwd
///     ("/home/deckop/.claude/agent-bus/workspaces/new-hire-77ec17") ... no such file
///
/// and for Atlas the path even carried another name — its pre-rename
/// placeholder — so the owner read it as another desk's terminal.
///
/// The good signal: the panel's model starts at the computer's own home and
/// the first command is sent from there.
@MainActor
final class TerminalStartsInTheDesksComputerTests: XCTestCase {

    func testTheComputersHomeIsTheOneTheDeckMountsPerDesk() {
        // Pinned to server/sandbox.py `DESK_HOME`; tests/test_terminal_home.py
        // holds the other end.
        XCTAssertEqual(DeskShellModel.computerHome, "/home/agent")
    }

    func testThePanelOpensAtTheDesksComputerAndNeverAtAHostPath() async {
        let shell = FakeShellClient()
        let model = DeskTerminalPanel.makeModel(desk: "atlas", displayName: "Atlas",
                                                client: shell)

        XCTAssertEqual(model.cwd, DeskShellModel.computerHome,
                       "The prompt opened somewhere that is not inside the desk's computer.")
        XCTAssertFalse(model.cwd.contains("agent-bus/workspaces"),
                       "A host workspace path does not exist inside the container.")

        await model.run("ls")

        XCTAssertEqual(shell.calls.first?.agent, "atlas")
        XCTAssertEqual(shell.calls.first?.cwd, DeskShellModel.computerHome,
                       "The first command was sent from a directory the container "
                       + "does not have, which is the OCI chdir failure.")
    }

    func testEachDeskGetsItsOwnPanelModel() {
        let shell = FakeShellClient()
        let atlas = DeskTerminalPanel.makeModel(desk: "atlas", displayName: "Atlas",
                                                client: shell)
        let globex = DeskTerminalPanel.makeModel(desk: "globex-lead",
                                                displayName: "Globex-lead", client: shell)
        XCTAssertEqual(atlas.desk, "atlas")
        XCTAssertEqual(globex.desk, "globex-lead")
        XCTAssertFalse(atlas === globex)
    }

    func testThePanelNoLongerTakesAHostWorkspace() {
        // Compiles only with the signature that has no workspace in it: a
        // caller cannot hand the terminal a host path again.
        _ = DeskTerminalPanel(desk: "atlas", displayName: "Atlas",
                              client: FakeShellClient())
    }
}

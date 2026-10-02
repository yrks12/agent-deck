import XCTest
@testable import DeckKit
@testable import DeckUI

/// **"I always see Atlas's computer, no matter where I am."**
///
/// His screenshot: a conversation with another desk open, and the take-over
/// sheet titled "Atlas's screen" over it, with Atlas's picture in the
/// inspector's thumbnail too.
///
/// MEASURED on the box, 2026-10-01: the deck serves each desk its own
/// container — `screen.jpg` and the stream's first frame differ for atlas,
/// growth-eng, growth-social and ops, and each `computer_mcp` runs
/// with its own `--desk`. So the desk was chosen wrong on this side.
///
/// The fault: every way into a screen (`takeoverOffers`, `takeOver(focus:)`,
/// the inspector's thumbnail) read `selectedAgentName` / `settingsAgent`, and
/// only `select(agent:)` ever moved those. Atlas is the chief and the first
/// row, so it is selected at launch; the lines in its thread that open another
/// desk's conversation go through `open(threadID:)`, which moved the thread
/// and left the desk behind. Every screen after that was Atlas's.
@MainActor
final class TheScreenFollowsTheDeskHeIsInTests: XCTestCase {

    private func twoDeskDeck(_ screens: RecordingScreenClient) async -> DeckStore {
        let client = ScriptedDeckClient()
        let atlas = Agent(name: "atlas", title: "Chief", section: "Work",
                          state: .idle, workspace: "/home/deckop/ws/atlas")
        let eng = Agent(name: "eng", title: "Engineer", section: "Work",
                        state: .idle, workspace: "/home/deckop/ws/eng")
        client.rosterPayload = RosterPayload(
            agents: [atlas, eng],
            threads: [makeThread("direct:atlas", agent: "atlas"),
                      makeThread("direct:eng", agent: "eng")],
            sectionOrder: ["Work"])
        client.pages = [
            MessagePage(threadID: "direct:atlas", messages: [], isReadOnly: false,
                        participants: ["owner", "atlas"]),
            MessagePage(threadID: "peer:atlas|eng", messages: [], isReadOnly: true,
                        participants: ["atlas", "eng"]),
            MessagePage(threadID: "direct:eng", messages: [], isReadOnly: false,
                        participants: ["owner", "eng"]),
        ]
        client.feeds = [.emitThenFinish([]), .emitThenFinish([]), .emitThenFinish([])]
        let store = DeckStore(client: client, shell: FakeShellClient(), screens: screens,
                              approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        return store
    }

    /// Open A's screen, go to B, open B's screen: B's frames, title and input.
    private func assertScreenIsEng(_ store: DeckStore, _ screens: RecordingScreenClient,
                                   via route: String,
                                   file: StaticString = #filePath, line: UInt = #line) async {
        XCTAssertEqual(store.settingsAgent?.name, "eng",
                       "after \(route) the inspector (and its thumbnail) is still on "
                       + "\(store.settingsAgent?.name ?? "nothing")", file: file, line: line)
        store.takeOver(focus: .screen)
        guard let request = store.takeover else {
            return XCTFail("no screen opened after \(route)", file: file, line: line)
        }
        XCTAssertEqual(request.desk, "eng",
                       "after \(route) the screen button opened \(request.desk)'s screen",
                       file: file, line: line)

        // What the sheet builds from that request — the picture, the title,
        // and where a click goes.
        let model = AgentComputerModel(desk: request.desk, displayName: request.displayName,
                                       client: screens)
        XCTAssertEqual(model.presentation.caption, "Eng's screen", file: file, line: line)
        screens.reset()
        await model.pollOnce()
        await model.send(.click(x: 10, y: 10))
        XCTAssertEqual(Set(screens.desksAsked), ["eng"],
                       "frames/input after \(route) went to \(screens.desksAsked)",
                       file: file, line: line)
        store.endTakeover()
    }

    func testOpeningAnotherDesksThreadFromAtlasMovesTheScreenToThatDesk() async {
        let screens = RecordingScreenClient()
        let store = await twoDeskDeck(screens)
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()
        store.takeOver(focus: .screen)
        XCTAssertEqual(store.takeover?.desk, "atlas", "the good signal: A's screen is A's")
        store.endTakeover()

        // The line in Atlas's thread that opens its conversation with eng.
        store.open(threadID: "peer:atlas|eng")
        await store.settle()
        await assertScreenIsEng(store, screens, via: "opening peer:atlas|eng")
    }

    func testOpeningADirectThreadByIDMovesTheScreenToThatDesk() async {
        let screens = RecordingScreenClient()
        let store = await twoDeskDeck(screens)
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()

        store.open(threadID: "direct:eng")
        await store.settle()
        XCTAssertEqual(store.selectedAgentName, "eng")
        await assertScreenIsEng(store, screens, via: "opening direct:eng")
    }

    func testTheSidebarRouteStillWorks() async {
        let screens = RecordingScreenClient()
        let store = await twoDeskDeck(screens)
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()
        store.select(agent: "eng", threadID: "direct:eng")
        await store.settle()
        await assertScreenIsEng(store, screens, via: "the sidebar")
    }
}

/// Records which desk every screen call was made for.
final class RecordingScreenClient: AgentScreenClient, @unchecked Sendable {
    private let lock = NSLock()
    private var _desks: [String] = []
    var desksAsked: [String] { lock.lock(); defer { lock.unlock() }; return _desks }
    func reset() { lock.lock(); _desks = []; lock.unlock() }
    private func note(_ desk: String) { lock.lock(); _desks.append(desk); lock.unlock() }

    func screenStatus(agent: String) async throws -> AgentScreenStatus {
        note(agent)
        return AgentScreenStatus(desk: agent, isRunning: true, image: "x",
                                 container: "deck-desk-\(agent)", display: ":99",
                                 width: 1280, height: 800, staleAfter: 30, generatedAt: Date())
    }
    func screenFrame(agent: String) async throws -> AgentScreenFrame {
        note(agent)
        return AgentScreenFrame(jpeg: Data([0xFF, 0xD8]), serverAge: 0.1, display: ":99",
                                receivedAt: Date())
    }
    func sendScreenInput(agent: String, _ input: ScreenInput) async throws { note(agent) }
}

import XCTest
@testable import DeckKit

/// **He picks which of his Mac's displays to watch and drive (2026-10-02).**
///
/// MEASURED: with a second monitor on his MacBook Pro, the phone and the Mac
/// app showed only the built-in display and every tap landed on it, so a
/// window on the second monitor was out of reach.
///
/// * The picker shows only when there are two or more displays. Picking one
///   asks for it on every route, and clicks use its size.
/// * His last choice is remembered per Mac.
/// * A display that is unplugged falls back to the main one with a short
///   note, and the remembered choice is cleared.
final class MacDisplayPickerTests: XCTestCase {
    private func status(edit: (inout [String: Any]) -> Void = { _ in }) throws -> AgentScreenStatus {
        try MacDisplayWireTests.status(edit: edit)
    }

    private func memory() -> MacDisplayMemory {
        let name = "mac-display-\(UUID().uuidString)"
        return MacDisplayMemory(defaults: UserDefaults(suiteName: name)!)
    }

    // MARK: the model

    @MainActor
    func testPickingADisplayAsksForItRemembersItAndRefreshesTheSize() async throws {
        let client = DisplayFakeClient(status: try status())
        let memory = memory()
        let model = AgentComputerModel(desk: "mac:mac_1", displayName: "MacBook Pro", client: client,
                                       displayMemory: memory)
        await model.refreshStatus()
        XCTAssertEqual(model.displays.map(\.id), [1, 3])
        XCTAssertTrue(model.showsDisplayPicker)
        client.next = try status { $0["display_id"] = 3; $0["width"] = 1600; $0["height"] = 900 }
        await model.chooseDisplay(3)
        XCTAssertEqual(model.macDisplay, 3)
        XCTAssertEqual(memory.load(nodeId: "mac_1"), 3)
        XCTAssertEqual(client.agents.last, "mac:mac_1@3")
        XCTAssertEqual(model.fit(inViewWidth: 1600, height: 900)?.displayWidth, 1600, "clicks use display 3's size")
        await model.send(.click(x: 5, y: 5))
        XCTAssertEqual(client.inputAgents.last, "mac:mac_1@3")

        let again = AgentComputerModel(desk: "mac:mac_1", displayName: "MacBook Pro", client: client,
                                       displayMemory: memory)
        XCTAssertEqual(again.macDisplay, 3, "his last choice is remembered")
    }

    @MainActor
    func testAnUnpluggedDisplayFallsBackToMainWithANote() async throws {
        let memory = memory()
        memory.save(nodeId: "mac_1", display: 3)
        let client = DisplayFakeClient(status: try status {
            $0["display_id"] = 1
            $0["display_note"] = "Display 3 is no longer connected. Showing Display 1 · Built-in."
            $0["displays"] = [($0["displays"] as! [Any])[0]]
        })
        let model = AgentComputerModel(desk: "mac:mac_1", displayName: "MacBook Pro", client: client,
                                       displayMemory: memory)
        await model.refreshStatus()
        XCTAssertNil(model.macDisplay)
        XCTAssertNil(memory.load(nodeId: "mac_1"))
        XCTAssertEqual(model.displayNote, "Display 3 is no longer connected. Showing Display 1 · Built-in.")
        XCTAssertFalse(model.showsDisplayPicker, "one display: no picker")
    }

    @MainActor
    func testAFrameFromAnotherDisplayRereadsTheStatus() async throws {
        let client = DisplayFakeClient(status: try status { $0["display_id"] = 3 })
        let memory = memory()
        memory.save(nodeId: "mac_1", display: 3)
        let model = AgentComputerModel(desk: "mac:mac_1", displayName: "MacBook Pro", client: client,
                                       displayMemory: memory)
        await model.pollOnce()
        let before = client.statusCalls
        client.frameDisplay = 1   // the deck fell back: display 3 is gone
        client.next = try status {
            $0["display_id"] = 1
            $0["display_note"] = "Display 3 is no longer connected. Showing Display 1 · Built-in."
        }
        await model.pollOnce()
        await model.pollOnce()
        XCTAssertGreaterThan(client.statusCalls, before)
        XCTAssertNil(model.macDisplay)
        XCTAssertNotNil(model.displayNote)
    }

    @MainActor
    func testADeskNeverShowsAPicker() async throws {
        let client = DisplayFakeClient(status: try status())
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client, displayMemory: memory())
        await model.refreshStatus()
        XCTAssertFalse(model.showsDisplayPicker)
        await model.chooseDisplay(3)
        XCTAssertNil(model.macDisplay)
        XCTAssertEqual(client.agents.last, "atlas")
    }
}

/// Answers a scripted status and records which name each call used.
final class DisplayFakeClient: AgentScreenClient, @unchecked Sendable {
    private let lock = NSLock()
    private var _status: AgentScreenStatus
    private var _agents: [String] = []
    private var _inputAgents: [String] = []
    private var _statusCalls = 0
    private var _frameDisplay: Int?

    init(status: AgentScreenStatus) { _status = status }

    var next: AgentScreenStatus {
        get { lock.withLock { _status } }
        set { lock.withLock { _status = newValue } }
    }
    var frameDisplay: Int? {
        get { lock.withLock { _frameDisplay } }
        set { lock.withLock { _frameDisplay = newValue } }
    }
    var agents: [String] { lock.withLock { _agents } }
    var inputAgents: [String] { lock.withLock { _inputAgents } }
    var statusCalls: Int { lock.withLock { _statusCalls } }

    func screenStatus(agent: String) async throws -> AgentScreenStatus {
        lock.withLock { _agents.append(agent); _statusCalls += 1; return _status }
    }

    func screenFrame(agent: String) async throws -> AgentScreenFrame {
        lock.withLock {
            _agents.append(agent)
            return AgentScreenFrame(jpeg: Data([0xFF, 0xD8]), serverAge: 0.1, display: "MacBook Pro",
                                    receivedAt: Date(), displayId: _frameDisplay ?? MacScreenName.display(agent) ?? 1)
        }
    }

    func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        lock.withLock { _inputAgents.append(agent) }
    }
}

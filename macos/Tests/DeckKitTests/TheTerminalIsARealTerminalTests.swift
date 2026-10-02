import XCTest
import AppKit
import SwiftTerm
@testable import DeckKit
@testable import DeckUI

/// **"The terminal of each computer should look and feel like a terminal."**
///
/// The drawer used to be a text field and a transcript: one `bash -lc` per
/// line, no vim, no top. This pins the wiring between SwiftTerm's emulator and
/// `DeskTerminalModel`: bytes from the socket are drawn by the emulator, what
/// he types leaves as input bytes, the grid the view lays out is the grid the
/// PTY is told, and while the terminal has his keys the take-over's keyboard
/// forwarding leaves them alone. Fake socket, off-screen window, no deck.
@MainActor
final class TheTerminalIsARealTerminalTests: XCTestCase {

    func testOutputFromTheSocketIsDrawnByTheEmulator() async throws {
        let (bridge, socket, model) = try await live()
        socket.push(.output(Data("hello \u{1b}[1mvim\u{1b}[0m".utf8)))
        try await until { Self.row(0, of: bridge).contains("hello vim") }
        XCTAssertEqual(model.processedBytes, Data("hello \u{1b}[1mvim\u{1b}[0m".utf8).count,
                       "an escape sequence is the emulator's to read, not text")
        model.stop()
    }

    func testWhatHeTypesLeavesAsInputBytes() async throws {
        let (bridge, socket, model) = try await live()
        bridge.view.send(txt: "ls\r")
        try await until { socket.sent.contains(.input(Data("ls\r".utf8))) }
        model.stop()
    }

    func testTheViewsGridIsTheGridThePTYIsTold() async throws {
        let (bridge, socket, model) = try await live()
        bridge.view.setFrameSize(NSSize(width: 900, height: 500))
        let terminal = bridge.view.getTerminal()
        let expected = TerminalStreamWire.resize(cols: terminal.cols, rows: terminal.rows)
        try await until { socket.sent.contains(.text(expected)) }
        model.stop()
    }

    func testANewSessionClearsTheScreen() async throws {
        let (bridge, socket, model) = try await live()
        socket.push(.output(Data("old window".utf8)))
        try await until { Self.row(0, of: bridge).contains("old window") }
        bridge.terminalReset()
        XCTAssertFalse(Self.row(0, of: bridge).contains("old window"))
        model.stop()
    }

    func testTheTakeOverDoesNotStealKeysFromTheTerminal() {
        let window = NSWindow.offscreenForTest(contentRect: NSRect(x: 0, y: 0, width: 400, height: 300))
        defer { window.close() }
        let client = FakeTerminalClient()
        let bridge = DeckTerminalBridge(model: DeskTerminalModel(desk: "atlas", client: client))
        window.contentView?.addSubview(bridge.view)
        XCTAssertTrue(window.makeFirstResponder(bridge.view))
        XCTAssertTrue(TakeoverSurface.isTypingIntoThisApp(window),
                      "with the terminal focused, his keys must not be forwarded to the screen")
    }

    // MARK: - helpers

    private func live() async throws -> (DeckTerminalBridge, FakeTerminalSocket, DeskTerminalModel) {
        let client = FakeTerminalClient()
        let socket = client.nextSocket()
        let model = DeskTerminalModel(desk: "atlas", client: client, idleAckDelay: nil)
        let bridge = DeckTerminalBridge(model: model)
        model.start()
        socket.push(.hello(.fixture(window: .deck)))
        try await until { model.state == .live }
        return (bridge, socket, model)
    }

    private static func row(_ row: Int, of bridge: DeckTerminalBridge) -> String {
        bridge.view.getTerminal().getLine(row: row)?.translateToString(trimRight: true) ?? ""
    }

    private func until(_ condition: @MainActor () -> Bool,
                       file: StaticString = #filePath, line: UInt = #line) async throws {
        for _ in 0..<400 {
            if condition() { return }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
        XCTFail("condition never became true", file: file, line: line)
        throw CancellationError()
    }
}

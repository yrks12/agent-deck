import XCTest
@testable import DeckKit

/// **The real PTY: it echoes, resizes and dies on Stop — and nothing listens.**
///
/// `/bin/sh` stands in for his login shell and tmux is never used here, so
/// nothing touches his own sessions. The listener check asks the kernel
/// (`lsof`) about this very test process while a shell is open: the Mac
/// dials out; it must never be dialled.
final class MacPTYShellTests: XCTestCase {

    /// A clean interactive sh standing in for his login shell: the "-l" it
    /// is given is ignored, and no profile of this machine is read.
    private func cleanShell() throws -> String {
        let path = NSTemporaryDirectory() + "pty-sh-\(UUID().uuidString)"
        try "#!/bin/sh\nunset PROMPT_COMMAND ENV BASH_ENV\nexport PS1='$ '\nexec /bin/sh --noprofile --norc -i\n"
            .write(toFile: path, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: path)
        addTeardownBlock { try? FileManager.default.removeItem(atPath: path) }
        return path
    }

    final class Screen: @unchecked Sendable {
        let lock = NSLock()
        var text = ""
        var total = 0
    }

    /// Reads the shell the way a session does (and acks it), into `Screen`.
    private func reader(_ shell: MacPTYShell) -> Screen {
        let screen = Screen()
        Task {
            while let d = await shell.next() {
                let total = screen.lock.withLock { () -> Int in
                    screen.text += String(decoding: d, as: UTF8.self)
                    screen.total += d.count
                    return screen.total
                }
                shell.acked(total)
            }
        }
        return screen
    }

    private func wait(_ screen: Screen, for needle: String, timeout: TimeInterval = 5) async -> String {
        let end = Date().addingTimeInterval(timeout)
        while Date() < end {
            let t = screen.lock.withLock { screen.text }
            if t.contains(needle) { return t }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
        return screen.lock.withLock { screen.text }
    }

    func testARealShellOnAPTYEchoesResizesAndDiesOnStop() async throws {
        let shell = try MacPTYShell(cols: 100, rows: 30, shell: try cleanShell(), tmux: nil)
        let screen = reader(shell)
        _ = await wait(screen, for: "$")   // typed before the prompt, readline flushes it
        shell.write(Data("echo hel''lo && stty size\n".utf8))
        let out = await wait(screen, for: "30 100")
        XCTAssertTrue(out.contains("hello"), out)
        XCTAssertTrue(out.contains("30 100"), out)
        shell.resize(cols: 120, rows: 40)
        shell.write(Data("stty size\n".utf8))
        let resized = await wait(screen, for: "40 120")
        XCTAssertTrue(resized.contains("40 120"), resized)
        shell.end(kill: true)
        let end = Date().addingTimeInterval(5)
        while shell.isAlive, Date() < end { try await Task.sleep(nanoseconds: 20_000_000) }
        XCTAssertFalse(shell.isAlive, "Stop left the shell running")
        XCTAssertNotEqual(Darwin.kill(shell.pid, 0) == 0 ? "alive" : "gone", "alive")
    }

    func testTmuxWhenPresentLoginShellAlways() {
        XCTAssertEqual(MacPTYShell.argv(shell: "/bin/zsh", tmux: "/opt/homebrew/bin/tmux"),
                       ["/opt/homebrew/bin/tmux", "-u", "new-session", "-A", "-s", "shaliach", "/bin/zsh", "-l"])
        XCTAssertEqual(MacPTYShell.argv(shell: "/bin/zsh", tmux: nil), ["/bin/zsh", "-l"])
        XCTAssertEqual(MacPTYShell.findTmux { $0 == "/usr/local/bin/tmux" }, "/usr/local/bin/tmux")
        XCTAssertNil(MacPTYShell.findTmux { _ in false })
    }

    func testNothingListensWhileATerminalIsOpen() async throws {
        let shell = try MacPTYShell(cols: 80, rows: 24, shell: try cleanShell(), tmux: nil)
        defer { shell.end(kill: true) }
        let screen = reader(shell)
        _ = await wait(screen, for: "$")
        shell.write(Data("echo re''ady\n".utf8))
        let ready = await wait(screen, for: "ready")
        XCTAssertTrue(ready.contains("ready"), ready)
        for pid in [getpid(), shell.pid] {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
            p.arguments = ["-nP", "-a", "-p", String(pid), "-i"]
            let pipe = Pipe()
            p.standardOutput = pipe
            p.standardError = FileHandle.nullDevice
            try p.run()
            p.waitUntilExit()
            let out = String(decoding: pipe.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            XCTAssertFalse(out.contains("LISTEN"), "pid \(pid) listens:\n\(out)")
            XCTAssertFalse(out.contains("UDP"), "pid \(pid) has a UDP socket:\n\(out)")
        }
        // And the code that does it cannot: no listening API in any of it.
        let here = URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .appendingPathComponent("../../Sources/DeckKit/MacBridge").standardized
        for file in ["MacTerminal.swift", "MacPTYShell.swift"] {
            let src = try String(contentsOf: here.appendingPathComponent(file), encoding: .utf8)
            for api in ["NWListener", "listen(", "bind(", "CFSocketCreate", "accept(", "NWConnection"] {
                XCTAssertFalse(src.contains(api), "\(file) uses \(api)")
            }
        }
    }
}

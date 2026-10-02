import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"The UI blocks stuff."** — the owner, of the take-over in the Mac app.
///
/// His screenshot: under the "Atlas's computer" header sat a second bar,
/// "Atlas's screen", floating over the top of the desk's picture (Chromium's
/// title, tabs and half the address bar under it), and the keys bar floated
/// over the bottom (the desk's taskbar under it). The rule now: **nothing is
/// drawn over the live picture.** One header above it, the keys bar docked
/// below it, the picture letterboxed in what is left.
///
/// Two detectors: the layout as numbers (`TakeoverChrome.frames`), and the
/// rendered sheet — a solid green desk, where any control drawn over it shows
/// up as a pixel inside the picture that is not green.
@MainActor
final class NothingCoversTheDesksScreenTests: XCTestCase {

    static let display = CGSize(width: 1600, height: 1000)
    /// The smallest sheet, his window (measured), a laptop's 90%, a big one.
    static let sizes: [CGSize] = [CGSize(width: 900, height: 600), CGSize(width: 1208, height: 949),
                                  CGSize(width: 1440, height: 900), CGSize(width: 1555, height: 971),
                                  CGSize(width: 2200, height: 1300)]

    // MARK: the numbers

    func testThePictureIntersectsNoControlAtAnyCommonSize() {
        for size in Self.sizes {
            for view in [ComputerView.screen, .both] {
                let f = TakeoverChrome.frames(view, area: size, display: Self.display)
                XCTAssertFalse(f.picture.isEmpty, "\(view) \(size): no picture")
                for (name, control) in [("header", f.header), ("keys bar", f.keysBar), ("terminal", f.terminal)] {
                    XCTAssertFalse(f.picture.intersection(control).width > 0.5
                                   && f.picture.intersection(control).height > 0.5,
                                   "\(view) \(size): the picture is under the \(name)")
                }
                XCTAssertTrue(CGRect(origin: .zero, size: size).contains(f.picture.insetBy(dx: 0.5, dy: 0.5)))
                XCTAssertEqual(f.picture.width / f.picture.height, Self.display.width / Self.display.height,
                               accuracy: 0.01, "\(view) \(size): the picture is stretched")
            }
        }
    }

    func testOneHeaderAboveEverythingAndTheKeysBarUnderThePicture() {
        let f = TakeoverChrome.frames(.both, area: CGSize(width: 1440, height: 900), display: Self.display)
        XCTAssertEqual(f.header.minY, 0)
        XCTAssertEqual(f.header.width, 1440, "one header across the screen and the terminal")
        XCTAssertGreaterThanOrEqual(f.picture.minY, f.header.maxY)
        XCTAssertGreaterThanOrEqual(f.keysBar.minY, f.picture.maxY - 0.5)
        XCTAssertGreaterThanOrEqual(f.terminal.minY, f.header.maxY)
    }

    func testTerminalAloneHasNoPictureAndNoKeysBar() {
        let f = TakeoverChrome.frames(.terminal, area: CGSize(width: 1208, height: 949), display: Self.display)
        XCTAssertTrue(f.picture.isEmpty)
        XCTAssertTrue(f.keysBar.isEmpty)
        XCTAssertEqual(f.terminal.height, 949 - f.header.height, accuracy: 0.5)
    }

    // MARK: the rendered sheet

    /// A desk whose whole screen is one green.
    struct GreenDesk: AgentScreenClient {
        let jpeg: Data
        init() {
            let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: 320, pixelsHigh: 200,
                                       bitsPerSample: 8, samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                       colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
            NSGraphicsContext.saveGraphicsState()
            NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)
            NSColor(deviceRed: 0, green: 1, blue: 0, alpha: 1).setFill()
            NSRect(x: 0, y: 0, width: 320, height: 200).fill()
            NSGraphicsContext.restoreGraphicsState()
            jpeg = rep.representation(using: .jpeg, properties: [.compressionFactor: 1.0])!
        }
        func screenStatus(agent: String) async throws -> AgentScreenStatus {
            AgentScreenStatus(desk: agent, isRunning: true, image: "agent-desktop",
                              container: "desk-\(agent)", display: ":99", width: 1600,
                              height: 1000, staleAfter: 60, generatedAt: Date())
        }
        func screenFrame(agent: String) async throws -> AgentScreenFrame {
            AgentScreenFrame(jpeg: jpeg, serverAge: 0.2, display: ":99", receivedAt: Date())
        }
        func sendScreenInput(agent: String, _ input: ScreenInput) async throws {}
    }

    private func render(terminal: Bool, _ size: CGSize, name: String) async throws -> NSBitmapImageRep {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let view = TakeoverStageView(
            request: TakeoverRequest(desk: "atlas", displayName: "Atlas",
                                     workspace: terminal ? "/home/agent/work" : "",
                                     focus: terminal ? .terminal : .screen),
            screens: GreenDesk(), shells: terminal ? FakeShellClient() : nil,
            tellDesk: { _ in true }, onExpand: {}, onClose: {})
        let host = NSHostingView(rootView: AnyView(view.frame(width: size.width, height: size.height)
            .environment(\.colorScheme, .dark)))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: size.width, height: size.height),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host
        window.orderBack(nil)
        defer { host.rootView = AnyView(EmptyView()); window.contentView = nil; window.close() }
        try await Task.sleep(nanoseconds: 1_200_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        let dir = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("UITests/Artifacts/takeover")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try RightPaneFixture.png(rep)?.write(to: dir.appendingPathComponent(name))
        return rep
    }

    private static func isGreen(_ c: NSColor?) -> Bool {
        guard let c = c?.usingColorSpace(.deviceRGB) else { return false }
        return c.greenComponent > 0.9 && c.redComponent < 0.6 && c.blueComponent < 0.5
    }

    /// The picture's box (the green's extent) and how many sampled pixels
    /// inside it are something else — a control drawn over the desk.
    private static func covered(_ rep: NSBitmapImageRep) -> (box: CGRect, foreign: Int, sampled: Int) {
        var minX = Int.max, maxX = -1, minY = Int.max, maxY = -1
        for y in stride(from: 0, to: rep.pixelsHigh, by: 2) {
            for x in stride(from: 0, to: rep.pixelsWide, by: 2) where isGreen(rep.colorAt(x: x, y: y)) {
                minX = min(minX, x); maxX = max(maxX, x); minY = min(minY, y); maxY = max(maxY, y)
            }
        }
        guard maxX >= 0 else { return (.zero, 0, 0) }
        var foreign = 0, sampled = 0
        // Inset past the edge's anti-aliasing.
        for y in stride(from: minY + 3, to: maxY - 2, by: 3) {
            for x in stride(from: minX + 3, to: maxX - 2, by: 3) {
                sampled += 1
                if !isGreen(rep.colorAt(x: x, y: y)) { foreign += 1 }
            }
        }
        return (CGRect(x: minX, y: minY, width: maxX - minX, height: maxY - minY), foreign, sampled)
    }

    func testNoControlIsDrawnOverTheScreen() async throws {
        for size in [CGSize(width: 900, height: 600), CGSize(width: 1208, height: 949),
                     CGSize(width: 1440, height: 900)] {
            for terminal in [false, true] {
                let name = "uncovered-\(terminal ? "both" : "screen")-\(Int(size.width))x\(Int(size.height)).png"
                let rep = try await render(terminal: terminal, size, name: name)
                let (box, foreign, sampled) = Self.covered(rep)
                let scale = CGFloat(rep.pixelsWide) / size.width
                XCTAssertGreaterThan(box.width / scale, size.width * 0.4, "\(name): the picture is not on the sheet")
                XCTAssertEqual(foreign, 0, "\(name): \(foreign) of \(sampled) pixels of the desk's screen "
                               + "are covered by a control")
                // One header: "Atlas's computer", never a second "Atlas's screen" bar.
                let words = RightPaneFixture.readText(rep).map(\.text)
                XCTAssertEqual(words.filter { $0.contains("computer") }.count, 1, "\(name): \(words)")
                XCTAssertFalse(words.contains { $0.contains("s screen") }, "\(name): two headers: \(words)")
            }
        }
    }
}

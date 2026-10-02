import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The sentence beside the Keyboard switch is the route, not a guess at it.**
///
/// `TheTakeOverKeyboardGoesWhereItSaysTests` pins where the keys land. This
/// file pins that the caption he reads is computed from the same value the
/// monitor routes by, in every state the take-over can be in — including the
/// one the old caption could not express at all: the window does not have the
/// keyboard (the app was denied the front at launch, or a keychain dialog took
/// it), so a key goes to some other window and nothing here is told.
@MainActor
final class TheKeyboardCaptionIsTheRouteTests: XCTestCase {

    private final class KeyWindow: NSWindow {
        override var isKeyWindow: Bool { true }
        override var canBecomeKey: Bool { true }
    }

    private func pump(_ seconds: TimeInterval = 0.25) {
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
    }

    private func host(in window: NSWindow) throws
        -> (surface: TakeoverSurface, client: FakeScreenClient, host: NSView)
    {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client)
        let surface = TakeoverSurface()
        let host = NSHostingView(rootView: AgentScreenStage(
            model: model, surface: surface))
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        pump(0.5)
        return (surface, client, host)
    }

    private func press(_ character: String, in window: NSWindow) {
        NSApp.sendEvent(NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil, characters: character,
            charactersIgnoringModifiers: character, isARepeat: false, keyCode: 0)!)
        pump(0.15)
    }

    private func clickThePicture(in window: NSWindow) {
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            NSApp.sendEvent(NSEvent.mouseEvent(
                with: type, location: NSPoint(x: 400, y: 500), modifierFlags: [],
                timestamp: 0, windowNumber: window.windowNumber, context: nil,
                eventNumber: 0, clickCount: 1, pressure: 1)!)
        }
        pump()
    }

    private func lineField(in view: NSView) -> NSTextField? {
        if let field = view as? NSTextField, field.isEditable { return field }
        for child in view.subviews { if let found = lineField(in: child) { return found } }
        return nil
    }

    private func forwarded(_ client: FakeScreenClient) -> [String] {
        client.inputs.compactMap { if case .type(let text) = $0 { return text }; return nil }
    }

    func testTheCaptionPredictsWhereEachKeystrokeLands() throws {
        let window = KeyWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 1120, height: 860),
            styleMask: [.titled], backing: .buffered, defer: false)
        let (surface, client, host) = try host(in: window)
        defer { window.orderOut(nil); window.contentView = nil }
        let field = try XCTUnwrap(lineField(in: host))

        // Switch on, nothing chosen: Atlas.
        XCTAssertEqual(surface.route, .agent)
        press("a", in: window)
        XCTAssertEqual(forwarded(client), ["a"])

        // He clicks the picture, then the field: this app, and it says so.
        clickThePicture(in: window)
        window.makeFirstResponder(field)
        pump()
        XCTAssertEqual(surface.route, .thisApp)
        press("b", in: window)
        XCTAssertEqual(forwarded(client), ["a"])
        XCTAssertEqual(field.stringValue, "b")

        // Back on the picture: Atlas again, and the caption moved with it.
        clickThePicture(in: window)
        XCTAssertEqual(surface.route, .agent)
        press("c", in: window)
        XCTAssertEqual(forwarded(client), ["a", "c"])

        // Switch off: nothing is forwarded, and the caption says it stays.
        surface.switchOn = false
        XCTAssertEqual(surface.route, .thisApp)
        press("d", in: window)
        XCTAssertEqual(forwarded(client), ["a", "c"])
    }

    func testAWindowWithoutTheKeyboardSaysSoAndForwardsNothing() throws {
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 1120, height: 860),
            styleMask: [.titled], backing: .buffered, defer: false)
        let (surface, client, _) = try host(in: window)
        defer { window.orderOut(nil); window.contentView = nil }

        XCTAssertFalse(window.isKeyWindow, "premise: this window never became key")
        XCTAssertEqual(surface.route, .anotherWindow)
        XCTAssertTrue(surface.route.caption(agent: "Atlas").contains("another window"))
        press("a", in: window)
        XCTAssertEqual(forwarded(client), [])
    }

    /// Every state has its own sentence, so no two of them can be mistaken for
    /// each other on screen.
    func testEveryRouteReadsDifferently() {
        let all: [KeyRoute] = [.agent, .thisApp, .anotherWindow]
        XCTAssertEqual(Set(all.map { $0.caption(agent: "Atlas") }).count, all.count)
        XCTAssertEqual(KeyRoute.decide(switchOn: true, windowIsKey: false,
                                       textFieldHasKeys: false), .anotherWindow)
        XCTAssertEqual(KeyRoute.decide(switchOn: true, windowIsKey: true,
                                       textFieldHasKeys: true), .thisApp)
        XCTAssertEqual(KeyRoute.decide(switchOn: true, windowIsKey: true,
                                       textFieldHasKeys: false), .agent)
        XCTAssertEqual(KeyRoute.decide(switchOn: false, windowIsKey: true,
                                       textFieldHasKeys: false), .thisApp)
    }
}

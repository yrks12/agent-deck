import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"Atlas's screen": the Keyboard switch is on and he cannot type.**
///
/// His screenshot: the switch ON, the caption beside it reading *"Your keys
/// stay in this app"*, a blue focus ring on "Paste or type a line", and the
/// keys reaching nobody he was looking at.
///
/// MEASURED, before this file existed (a real SwiftUI `.sheet`, hosted here):
///
///     onAppear            fieldFocused=false  armed=true
///     (no event at all)   fieldFocused -> true
///     firstResponder      _SystemTextFieldFieldEditor
///
/// A sheet hands its first text field the keyboard on its own. So the moment
/// the take-over opened, the switch said "on" and every key went to the line
/// field — and clicking the picture did not take them back, because a SwiftUI
/// gesture never moves AppKit's first responder. There was no pointer path
/// from "switch on" to "keys on Atlas's computer" at all.
///
/// The stage decided where keys go from **two** answers — `@FocusState` for
/// the caption and the arming, the window's real first responder for the
/// monitor — and nothing on screen was computed from the one that routes the
/// key. These tests ask only the real thing: where did the keystroke land.
@MainActor
final class TheTakeOverKeyboardGoesWhereItSaysTests: XCTestCase {

    /// A window AppKit treats as key without activating the test process,
    /// which would take the keyboard off whoever is using this Mac.
    private final class KeyWindow: NSWindow {
        override var isKeyWindow: Bool { true }
        override var canBecomeKey: Bool { true }
    }

    private struct Harness {
        let window: NSWindow
        let host: NSView
        let client: FakeScreenClient
    }

    private func pump(_ seconds: TimeInterval = 0.25) {
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
    }

    private func open(in window: NSWindow) throws -> Harness {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let stage = TakeoverStageView(
            request: TakeoverRequest(desk: "atlas", displayName: "Atlas",
                                     workspace: "", focus: .screen),
            screens: client, shells: nil, onClose: {})
        let host = NSHostingView(rootView: stage)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        pump(0.5)
        return Harness(window: window, host: host, client: client)
    }

    private func keyWindow() -> NSWindow {
        KeyWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: 1120, height: 860),
                  styleMask: [.titled], backing: .buffered, defer: false)
    }

    private func close(_ harness: Harness) {
        harness.window.orderOut(nil)
        harness.window.contentView = nil
    }

    private func lineField(in view: NSView) -> NSTextField? {
        if let field = view as? NSTextField, field.isEditable { return field }
        for child in view.subviews { if let found = lineField(in: child) { return found } }
        return nil
    }

    private func press(_ character: String, in window: NSWindow) {
        let event = NSEvent.keyEvent(
            with: .keyDown, location: .zero, modifierFlags: [], timestamp: 0,
            windowNumber: window.windowNumber, context: nil, characters: character,
            charactersIgnoringModifiers: character, isARepeat: false, keyCode: 0)!
        NSApp.sendEvent(event)
        pump(0.15)
    }

    /// A press and release in the middle of the picture, in window coordinates.
    private func clickThePicture(in window: NSWindow) {
        let point = NSPoint(x: 400, y: 500)
        for type in [NSEvent.EventType.leftMouseDown, .leftMouseUp] {
            let event = NSEvent.mouseEvent(
                with: type, location: point, modifierFlags: [], timestamp: 0,
                windowNumber: window.windowNumber, context: nil, eventNumber: 0,
                clickCount: 1, pressure: 1)!
            NSApp.sendEvent(event)
        }
        pump(0.25)
    }

    private func typed(_ harness: Harness) -> [String] {
        harness.client.inputs.compactMap {
            if case .type(let text) = $0 { return text }
            return nil
        }
    }

    // MARK: the sheet he actually sees

    /// The shape the app presents it in: `DeckRootView`'s `.sheet(item:)`.
    private struct Presenter: View {
        let client: FakeScreenClient
        @State var request: TakeoverRequest?
        var body: some View {
            Color.clear.frame(width: 1100, height: 800)
                .sheet(item: $request) { request in
                    TakeoverStageView(request: request, screens: client, shells: nil,
                                      onClose: { self.request = nil })
                }
        }
    }

    func testOpeningTheTakeOverInASheetDoesNotHandTheKeyboardToTheLineField() throws {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let client = FakeScreenClient(status: try AgentComputerTests.runningStatus())
        let host = NSHostingView(rootView: Presenter(
            client: client,
            request: TakeoverRequest(desk: "atlas", displayName: "Atlas",
                                     workspace: "", focus: .screen)))
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 1100, height: 800),
            styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        pump(1.0)
        defer { window.orderOut(nil); window.contentView = nil }

        let sheet = try XCTUnwrap(window.attachedSheet, "the take-over never presented")
        XCTAssertFalse(sheet.firstResponder is NSTextView,
                       "the Keyboard switch comes up ON, and the sheet handed every key to "
                       + "the line field instead: first responder is "
                       + "\(String(describing: sheet.firstResponder))")
        window.endSheet(sheet)
    }

    // MARK: with the switch on, keys reach Atlas

    func testAFieldThatTookTheKeyboardOnItsOwnDoesNotKeepTheFirstKeystroke() throws {
        let harness = try open(in: keyWindow())
        defer { close(harness) }
        let field = try XCTUnwrap(lineField(in: harness.host))

        // What the sheet does on open, before he has touched anything.
        harness.window.makeFirstResponder(field)
        pump()
        press("a", in: harness.window)

        XCTAssertEqual(typed(harness), ["a"],
                       "the switch is on and he has not chosen the field; the key "
                       + "belongs to Atlas's computer")
    }

    func testClickingThePictureTakesTheKeysBackFromTheLineField() throws {
        let harness = try open(in: keyWindow())
        defer { close(harness) }
        let field = try XCTUnwrap(lineField(in: harness.host))

        // He chose the field: it has his keys.
        clickThePicture(in: harness.window)
        harness.window.makeFirstResponder(field)
        pump()
        press("x", in: harness.window)
        XCTAssertEqual(field.stringValue, "x")
        XCTAssertEqual(typed(harness), [], "a key typed into the field went to Atlas too")

        // Then the picture — the Gmail compose box on Atlas's screen.
        clickThePicture(in: harness.window)
        press("y", in: harness.window)

        XCTAssertEqual(typed(harness), ["y"],
                       "he clicked Atlas's screen and typed; the key stayed with the "
                       + "line field he had left")
        XCTAssertEqual(field.stringValue, "x")
    }
}

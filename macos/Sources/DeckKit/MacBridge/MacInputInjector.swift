#if os(macOS)
import ApplicationServices
import Carbon.HIToolbox
import CoreGraphics
import Foundation

/// **His hands win.** Any real mouse or key event pauses agent input for a
/// few seconds. Our own events carry `MacInputInjector.marker` in
/// `eventSourceUserData`, so the app's global monitor can tell them apart
/// (MEASURED: posted events also reset the HID idle clock, so "seconds since
/// the last event" cannot).
public final class MacOwnerActivity: @unchecked Sendable {
    public static let shared = MacOwnerActivity()
    public static let pause: TimeInterval = 3

    private let lock = NSLock()
    private var last: Date?

    public init() {}

    /// A real event from him (not carrying our marker).
    public func touched(at date: Date = Date()) { lock.withLock { last = date } }

    public func isActive(now: Date = Date()) -> Bool {
        lock.withLock { last.map { now.timeIntervalSince($0) < Self.pause } ?? false }
    }
}

/// **Posts one gesture with CGEvent**, on the main display, in points.
///
/// Refuses, with the reason the desk acts on: `tcc_denied` without the
/// Accessibility permission (posting would silently do nothing),
/// `owner_active` while he is using the Mac, `secure_field` when a password
/// field has focus or secure input is on (never typed into, by anyone).
public final class MacInputInjector: MacInputPerforming, @unchecked Sendable {
    /// "DECK": stamped on every event we post.
    public static let marker: Int64 = 0x4445_434B
    static let typeDelay: UInt64 = 8_000_000      // ns between typed characters
    static let dragSteps = 12

    private let owner: MacOwnerActivity
    private let source = CGEventSource(stateID: .privateState)

    public init(owner: MacOwnerActivity = .shared) { self.owner = owner }

    public static var accessibilityGranted: Bool { AXIsProcessTrusted() }

    /// The live view's and the agent's reference display.
    public static var mainDisplay: MacDisplayFrame {
        let b = CGDisplayBounds(CGMainDisplayID())
        return MacDisplayFrame(x: b.origin.x, y: b.origin.y, width: b.width, height: b.height)
    }

    public func perform(_ gesture: MacInputGesture) async throws {
        guard Self.accessibilityGranted else { throw MacOpError("tcc_denied", MacControlCopy.noAccessibility) }
        if owner.isActive() { throw MacOpError("owner_active", MacControlCopy.ownerActive) }
        if gesture.isKeyboard, Self.secureFieldFocused() {
            throw MacOpError("secure_field", MacControlCopy.secureField)
        }
        let display = Self.mainDisplay
        func at(_ x: Int, _ y: Int, _ space: MacSpace) -> CGPoint {
            let p = MacInputMapping.point(x: x, y: y, space: space, display: display)
            return CGPoint(x: p.x, y: p.y)
        }
        switch gesture {
        case let .click(x, y, button, count, space):
            let p = at(x, y, space)
            post(mouse(.mouseMoved, p, .left))
            for n in 1...count {
                post(mouse(down(button), p, cg(button), clicks: n))
                post(mouse(up(button), p, cg(button), clicks: n))
            }
        case let .move(x, y, space):
            post(mouse(.mouseMoved, at(x, y, space), .left))
        case let .drag(x, y, tx, ty, button, space):
            let a = at(x, y, space), b = at(tx, ty, space)
            post(mouse(.mouseMoved, a, .left))
            post(mouse(down(button), a, cg(button)))
            for i in 1...Self.dragSteps {
                let f = Double(i) / Double(Self.dragSteps)
                let p = CGPoint(x: a.x + (b.x - a.x) * f, y: a.y + (b.y - a.y) * f)
                post(mouse(dragged(button), p, cg(button)))
                try await Task.sleep(nanoseconds: 10_000_000)
            }
            post(mouse(up(button), b, cg(button)))
        case let .scroll(x, y, dx, dy, space):
            post(mouse(.mouseMoved, at(x, y, space), .left))
            if let e = CGEvent(scrollWheelEvent2Source: source, units: .line, wheelCount: 2,
                               wheel1: Int32(dy), wheel2: Int32(-dx), wheel3: 0) {
                post(e)
            }
        case let .key(name):
            guard let chord = MacKeyChord.parse(name) else { throw MacOpError("bad_input", "unknown key \(name)") }
            press(chord)
        case let .type(text):
            for (i, ch) in text.enumerated() {
                if i % 32 == 31, Self.secureFieldFocused() {
                    throw MacOpError("secure_field", MacControlCopy.secureField)
                }
                if owner.isActive() { throw MacOpError("owner_active", MacControlCopy.ownerActive) }
                switch ch {
                case "\n", "\r", "\r\n": press(MacKeyChord(keyCode: 36, flags: []))
                case "\t": press(MacKeyChord(keyCode: 48, flags: []))
                default: typeCharacter(ch)
                }
                try await Task.sleep(nanoseconds: Self.typeDelay)
            }
        }
    }

    // MARK: events

    private func post(_ event: CGEvent?) {
        guard let event else { return }
        event.setIntegerValueField(.eventSourceUserData, value: Self.marker)
        event.post(tap: .cghidEventTap)
    }

    private func mouse(_ type: CGEventType, _ p: CGPoint, _ button: CGMouseButton, clicks: Int = 1) -> CGEvent? {
        let e = CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: p, mouseButton: button)
        e?.setIntegerValueField(.mouseEventClickState, value: Int64(clicks))
        return e
    }

    private func cg(_ b: MacMouseButton) -> CGMouseButton {
        switch b {
        case .left: return .left
        case .right: return .right
        case .middle: return .center
        }
    }

    private func down(_ b: MacMouseButton) -> CGEventType {
        switch b {
        case .left: return .leftMouseDown
        case .right: return .rightMouseDown
        case .middle: return .otherMouseDown
        }
    }

    private func up(_ b: MacMouseButton) -> CGEventType {
        switch b {
        case .left: return .leftMouseUp
        case .right: return .rightMouseUp
        case .middle: return .otherMouseUp
        }
    }

    private func dragged(_ b: MacMouseButton) -> CGEventType {
        switch b {
        case .left: return .leftMouseDragged
        case .right: return .rightMouseDragged
        case .middle: return .otherMouseDragged
        }
    }

    private func press(_ chord: MacKeyChord) {
        var flags: CGEventFlags = []
        if chord.flags.contains(.shift) { flags.insert(.maskShift) }
        if chord.flags.contains(.control) { flags.insert(.maskControl) }
        if chord.flags.contains(.option) { flags.insert(.maskAlternate) }
        if chord.flags.contains(.command) { flags.insert(.maskCommand) }
        for isDown in [true, false] {
            let e = CGEvent(keyboardEventSource: source, virtualKey: CGKeyCode(chord.keyCode), keyDown: isDown)
            e?.flags = flags
            post(e)
        }
    }

    private func typeCharacter(_ ch: Character) {
        let units = Array(String(ch).utf16)
        for isDown in [true, false] {
            let e = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: isDown)
            e?.flags = []
            units.withUnsafeBufferPointer { buf in
                e?.keyboardSetUnicodeString(stringLength: buf.count, unicodeString: buf.baseAddress)
            }
            post(e)
        }
    }

    // MARK: the safety floor

    /// A password field has focus, or some app turned secure input on.
    public static func secureFieldFocused() -> Bool {
        if IsSecureEventInputEnabled() { return true }
        let system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(system, 0.5)
        var focused: CFTypeRef?
        guard AXUIElementCopyAttributeValue(system, kAXFocusedUIElementAttribute as CFString, &focused) == .success,
              let element = focused, CFGetTypeID(element) == AXUIElementGetTypeID() else { return false }
        var subrole: CFTypeRef?
        guard AXUIElementCopyAttributeValue(element as! AXUIElement, kAXSubroleAttribute as CFString, &subrole) == .success
        else { return false }
        return (subrole as? String) == (kAXSecureTextFieldSubrole as String)
    }
}
#endif

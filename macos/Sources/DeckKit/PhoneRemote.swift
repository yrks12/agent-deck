import Foundation

// MARK: - The agent's computer as a remote desktop
//
// "VERY easy to operate from the phone — Jump Desktop / Screens quality."
// Those apps are easy because the fiddly parts are right: a trackpad cursor
// for targets smaller than a fingertip, gestures that mean what every remote
// desktop means by them, modifiers that stick, and a picture that follows the
// cursor instead of losing it. Each rule is here, pure, and tested in
// `PhoneRemoteTests`; the iOS view only draws and forwards touches.

/// How a finger becomes a click.
public enum RemoteMode: String, Sendable, CaseIterable {
    /// Tap where you touch. Quick, and fine for anything button-sized.
    case touch
    /// A visible cursor moved by relative drag; tap clicks at the cursor.
    /// For links, close boxes and anything smaller than a fingertip.
    case trackpad
}

// MARK: zoom: opening, forward mapping, following

extension ScreenZoom {
    /// Desktop text is 11–13 px; below ~0.85pt per pixel it cannot be read on
    /// a phone. The screen opens at least this large.
    public static let readablePointsPerPixel = 0.85

    /// The zoom the screen opens at: readable, anchored at the desktop's top
    /// left (the menus and the browser's address bar live there).
    public static func opening(fit: ScreenFit, viewWidth: Double, viewHeight: Double) -> ScreenZoom {
        let scale = max(1, readablePointsPerPixel / max(fit.scale, 0.0001))
        let spareX = max(0, (fit.drawnWidth * scale - viewWidth) / 2)
        let spareY = max(0, (fit.drawnHeight * scale - viewHeight) / 2)
        return ScreenZoom(scale: scale, offsetX: spareX, offsetY: spareY)
            .clamped(to: fit, viewWidth: viewWidth, viewHeight: viewHeight)
    }

    /// Where a display pixel is drawn in the view: the inverse of
    /// `displayPoint`. Used to draw the cursor, the loupe and the ripple.
    public func viewPoint(ofDisplayX x: Double, y: Double, in fit: ScreenFit) -> (x: Double, y: Double) {
        let fx = fit.drawnX + (x / Double(fit.displayWidth)) * fit.drawnWidth
        let fy = fit.drawnY + (y / Double(fit.displayHeight)) * fit.drawnHeight
        let cx = fit.drawnX + fit.drawnWidth / 2
        let cy = fit.drawnY + fit.drawnHeight / 2
        return (cx + (fx - cx) * scale + offsetX, cy + (fy - cy) * scale + offsetY)
    }

    /// Panned just enough that a display point (the cursor) is inside the
    /// view with a margin, and not at all if it already is.
    public func following(displayX x: Double, y: Double, in fit: ScreenFit,
                          viewWidth: Double, viewHeight: Double) -> ScreenZoom {
        let at = viewPoint(ofDisplayX: x, y: y, in: fit)
        let mx = min(60, viewWidth / 4), my = min(60, viewHeight / 4)
        var next = self
        if at.x < mx { next.offsetX += mx - at.x }
        if at.x > viewWidth - mx { next.offsetX -= at.x - (viewWidth - mx) }
        if at.y < my { next.offsetY += my - at.y }
        if at.y > viewHeight - my { next.offsetY -= at.y - (viewHeight - my) }
        guard next != self else { return self }
        return next.clamped(to: fit, viewWidth: viewWidth, viewHeight: viewHeight)
    }
}

// MARK: trackpad cursor

/// The cursor in trackpad mode, in display pixels. Moved relatively, so a
/// finger never covers what it is pointing at. It is local state, not a
/// request, so keeping it on the display is not a clamp of anything sent:
/// every point it can be at is one the deck accepts.
public struct TrackpadCursor: Equatable, Sendable {
    /// Cursor travel per point of finger travel, as seen on the phone.
    /// A little over 1 so one thumb stroke crosses a zoomed view.
    public static let gain = 1.25

    public private(set) var x: Double
    public private(set) var y: Double
    public let displayWidth: Int
    public let displayHeight: Int

    public init(displayWidth: Int, displayHeight: Int) {
        self.displayWidth = max(1, displayWidth)
        self.displayHeight = max(1, displayHeight)
        x = Double(self.displayWidth / 2)
        y = Double(self.displayHeight / 2)
    }

    public var point: DisplayPoint { DisplayPoint(x: Int(x), y: Int(y)) }

    public mutating func move(byViewDX dx: Double, dy: Double, fit: ScreenFit, zoom: ScreenZoom) {
        let pointsPerPixel = fit.scale * zoom.scale
        guard pointsPerPixel > 0, dx.isFinite, dy.isFinite else { return }
        let perPoint = Self.gain / pointsPerPixel
        x = min(Double(displayWidth - 1), max(0, x + dx * perPoint))
        y = min(Double(displayHeight - 1), max(0, y + dy * perPoint))
    }
}

// MARK: gestures

/// The gestures every remote desktop agrees on.
public enum RemoteGesture: Equatable, Sendable {
    case tap, doubleTap, longPress, twoFingerTap
}

public enum RemoteGestures {
    /// Tap = click, double-tap = double-click, long-press and two-finger tap =
    /// right-click. In touch mode at the finger (nil on the letterbox); in
    /// trackpad mode at the cursor, wherever the finger is.
    public static func input(for gesture: RemoteGesture, mode: RemoteMode,
                             atViewX x: Double, y: Double, cursor: TrackpadCursor?,
                             fit: ScreenFit, zoom: ScreenZoom) -> ScreenInput? {
        let target: DisplayPoint?
        switch mode {
        case .touch: target = zoom.displayPoint(atViewX: x, y: y, in: fit)
        case .trackpad: target = cursor?.point
        }
        guard let p = target else { return nil }
        switch gesture {
        case .tap: return .click(x: p.x, y: p.y, button: MouseButton.left.rawValue, count: 1)
        case .doubleTap: return .click(x: p.x, y: p.y, button: MouseButton.left.rawValue, count: 2)
        case .longPress, .twoFingerTap:
            return .click(x: p.x, y: p.y, button: MouseButton.right.rawValue, count: 1)
        }
    }

    /// A press held and released: moved, it was a drag (select text, move a
    /// window); released where it started, it was a right-click.
    public static func pressEnded(from start: DisplayPoint, to end: DisplayPoint) -> ScreenInput {
        if start == end {
            return .click(x: start.x, y: start.y, button: MouseButton.right.rawValue, count: 1)
        }
        return .drag(x: start.x, y: start.y, toX: end.x, toY: end.y, button: MouseButton.left.rawValue)
    }
}

// MARK: keys, modifiers, combos

/// The named keys of the accessory row, from the Mac's `ScreenKeys` table.
public enum RemoteKeys {
    public static let escape = name(53)
    public static let tab = name(48)
    public static let enter = name(36)
    public static let backspace = name(51)
    public static let left = name(123)
    public static let right = name(124)
    public static let down = name(125)
    public static let up = name(126)

    private static func name(_ code: UInt16) -> String { ScreenKeys.namedKeys[code] ?? "Escape" }
}

/// ⌘ ⌃ ⌥ ⇧ that stick, the way the iOS shift key does: one tap latches for
/// the next key, a second tap locks, a third turns it off.
///
/// **⌘ means Ctrl on the agent's machine.** It is Linux: copy is ctrl+c. He
/// is a Mac user, so ⌘C must copy there too; ⌘ and ⌃ together are one ctrl.
public struct StickyModifiers: Equatable, Sendable {
    public enum Key: String, CaseIterable, Sendable { case command, control, option, shift }

    private var latched: Set<Key> = []
    private var locked: Set<Key> = []

    public init() {}

    public mutating func tap(_ key: Key) {
        if locked.contains(key) {
            locked.remove(key)
        } else if latched.contains(key) {
            latched.remove(key)
            locked.insert(key)
        } else {
            latched.insert(key)
        }
    }

    public func isOn(_ key: Key) -> Bool { latched.contains(key) || locked.contains(key) }
    public func isLocked(_ key: Key) -> Bool { locked.contains(key) }
    public var isAnyOn: Bool { !latched.isEmpty || !locked.isEmpty }

    /// `ctrl+alt+shift+`, always in that order.
    public var prefix: String {
        var parts: [String] = []
        if isOn(.command) || isOn(.control) { parts.append("ctrl") }
        if isOn(.option) { parts.append("alt") }
        if isOn(.shift) { parts.append("shift") }
        return parts.isEmpty ? "" : parts.joined(separator: "+") + "+"
    }

    /// One key with whatever is on. Spends the latched ones.
    public mutating func chord(_ keysym: String) -> ScreenInput {
        let input = ScreenInput.key(prefix + keysym)
        latched.removeAll()
        return input
    }

    /// Text from the phone's keyboard. With a modifier on, one character is a
    /// chord (⌘ then c is copy); otherwise text is text, sent verbatim.
    public mutating func typed(_ text: String) -> [ScreenInput] {
        guard !text.isEmpty else { return [] }
        if isAnyOn, let sym = ScreenKeys.keysym(for: text) {
            return [chord(sym)]
        }
        latched.removeAll()
        return [.type(text)]
    }
}

/// The combos a remote-desktop toolbar offers, as the agent's Linux spells them.
public enum RemoteCombo: String, CaseIterable, Sendable {
    case copy, paste, selectAll, undo, back, forward, reload, addressBar, newTab, closeTab

    public var title: String {
        switch self {
        case .copy: return "Copy"
        case .paste: return "Paste (agent's clipboard)"
        case .selectAll: return "Select all"
        case .undo: return "Undo"
        case .back: return "Back"
        case .forward: return "Forward"
        case .reload: return "Reload"
        case .addressBar: return "Address bar"
        case .newTab: return "New tab"
        case .closeTab: return "Close tab"
        }
    }

    public var symbol: String {
        switch self {
        case .copy: return "doc.on.doc"
        case .paste: return "doc.on.clipboard"
        case .selectAll: return "selection.pin.in.out"
        case .undo: return "arrow.uturn.backward"
        case .back: return "chevron.backward"
        case .forward: return "chevron.forward"
        case .reload: return "arrow.clockwise"
        case .addressBar: return "link"
        case .newTab: return "plus.square.on.square"
        case .closeTab: return "xmark.square"
        }
    }

    public var input: ScreenInput {
        switch self {
        case .copy: return .key("ctrl+c")
        case .paste: return .key("ctrl+v")
        case .selectAll: return .key("ctrl+a")
        case .undo: return .key("ctrl+z")
        case .back: return .key("alt+\(RemoteKeys.left)")
        case .forward: return .key("alt+\(RemoteKeys.right)")
        case .reload: return .key("ctrl+r")
        case .addressBar: return .key("ctrl+l")
        case .newTab: return .key("ctrl+t")
        case .closeTab: return .key("ctrl+w")
        }
    }
}

/// The browser, driven by keys the input route already accepts.
public enum BrowserActions {
    /// Focus the address bar, type the address, go.
    public static func open(_ address: String) -> [ScreenInput] {
        let trimmed = address.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return [] }
        return [RemoteCombo.addressBar.input, .type(trimmed), .key(RemoteKeys.enter)]
    }
}

extension ScreenTyping {
    /// The deck refuses to type more than this many characters in one call.
    public static let maxTypeLength = 4096

    /// The iPhone's clipboard, typed onto the agent's machine, in pieces the
    /// deck accepts. Counted in code points, as the deck counts, and never
    /// splitting a character.
    public static func paste(_ text: String?) -> [ScreenInput] {
        guard let text, !text.isEmpty else { return [] }
        var out: [ScreenInput] = []
        var chunk = ""
        var size = 0
        for character in text {
            let n = character.unicodeScalars.count
            if size + n > maxTypeLength, !chunk.isEmpty {
                out.append(.type(chunk))
                chunk = ""
                size = 0
            }
            chunk.append(character)
            size += n
        }
        if !chunk.isEmpty { out.append(.type(chunk)) }
        return out
    }
}

/// Typing that reaches the agent as he types, like every remote desktop: the
/// field's change is turned into Backspaces for what went and text for what
/// came. An autocorrect is a rewrite and goes down as one.
public enum LiveTyping {
    public static func inputs(from old: String, to new: String) -> [ScreenInput] {
        let a = Array(old), b = Array(new)
        var common = 0
        while common < a.count, common < b.count, a[common] == b[common] { common += 1 }
        var out = [ScreenInput](repeating: .key(RemoteKeys.backspace), count: a.count - common)
        if common < b.count { out.append(.type(String(b[common...]))) }
        return out
    }
}

// MARK: take over / hand back

/// **Who is driving.** The deck has no lock that stops an agent's clicks, so
/// taking over is said to the agent in its own thread, in words it acts on:
/// stop touching the computer until handed back. Handing back tells it to
/// look again, because he may have changed what it left.
public struct ScreenControl: Equatable, Sendable {
    public enum Driver: Equatable, Sendable { case agent, owner }

    public let agentName: String
    public private(set) var driver: Driver = .agent

    public init(agentName: String) { self.agentName = agentName }

    public var buttonTitle: String {
        driver == .agent ? "Take over" : "Hand back to \(agentName)"
    }

    public var takeOverMessage: String {
        "I'm taking over your computer from my phone. Stop every click, keystroke and "
            + "browser action on it now and wait — don't touch it until I hand it back."
    }

    public var handBackMessage: String {
        "I've handed your computer back. Look at the screen again before you carry on — "
            + "I may have changed what you left."
    }

    /// Closing the screen while driving hands back: an agent must never be
    /// left paused by a screen nobody is looking at.
    public var messageOnClose: String? { driver == .owner ? handBackMessage : nil }

    public mutating func tookOver() { driver = .owner }
    public mutating func handedBack() { driver = .agent }
}

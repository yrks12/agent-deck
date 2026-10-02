import Foundation

/// **Mac control (owner, 2026-10-01): agents click, type, press keys and
/// scroll on this Mac, under a switch he turns on per session.**
///
/// This file is the pure half: the grant, one gesture, pixels to points, and
/// key names to virtual keys. Posting the events (`MacInputInjector`) and
/// capturing the screen (`MacScreenStreamer`) are macOS-only and live apart.

// MARK: - the grant

/// "Let agents control this Mac": one desk or all, for 30 minutes.
///
/// **Never saved.** It lives in the bridge's memory only, so quitting the app
/// ends it with no code path to forget; locking the Mac, Stop and the hotkey
/// end it on purpose.
public struct MacControlGrant: Equatable, Sendable {
    /// The owner's own hands (the live view on his phone). Not a legal desk
    /// name, so no desk can borrow it; it rides any live grant.
    public static let ownerDesk = "@owner"
    public static let duration: Double = 30 * 60

    /// nil = every desk.
    public var scope: String?
    public var since: Double
    public var until: Double

    public init(scope: String?, since: Double, until: Double) {
        self.scope = scope
        self.since = since
        self.until = until
    }

    public static func start(scope: String?, now: Double) -> MacControlGrant {
        MacControlGrant(scope: scope, since: now, until: now + duration)
    }

    public func live(at now: Double) -> Bool { now < until }

    public func covers(_ desk: String, now: Double) -> Bool {
        guard live(at: now) else { return false }
        return scope == nil || desk == scope || desk == Self.ownerDesk
    }

    /// What the deck mirrors from each poll.
    public var wire: MacWireControl { MacWireControl(scope: scope ?? "all", until: until) }
}

/// The grant as the poll carries it: `{"scope": "all"|desk, "until": ts}`.
public struct MacWireControl: Codable, Equatable, Sendable {
    public var scope: String
    public var until: Double

    public init(scope: String, until: Double) {
        self.scope = scope
        self.until = until
    }
}

/// A picture's pixel size: the space every coordinate is in, and which
/// display it is a picture of (nil = the main display).
public struct MacSpace: Codable, Equatable, Sendable {
    public var width: Int
    public var height: Int
    public var display: Int?

    public init(width: Int, height: Int, display: Int? = nil) {
        self.width = width
        self.height = height
        self.display = display
    }
}

/// Accessibility (to post events) and Screen Recording (to see), as the poll
/// reports them so the deck can tell the owner which one is missing.
public struct MacPermissions: Codable, Equatable, Sendable {
    public var accessibility: Bool
    public var screenRecording: Bool

    enum CodingKeys: String, CodingKey {
        case accessibility
        case screenRecording = "screen_recording"
    }

    public init(accessibility: Bool, screenRecording: Bool) {
        self.accessibility = accessibility
        self.screenRecording = screenRecording
    }
}

/// The words, in one place: the banner, the menu bar, Settings, the refusals.
public enum MacControlCopy {
    public static let switchTitle = "Let agents control this Mac"
    /// ⌃⌥⌘. — Control-Option-Command-Period, anywhere, at once.
    public static let hotkey = "⌃⌥⌘."
    public static let allowTitle = "Allow for 30 minutes"

    public static func banner(scope: String?, now: Double, until: Double) -> String {
        let who = scope.map { MacGrantCopy.displayName($0) + " is" } ?? "Agents are"
        let left = max(1, Int(((until - now) / 60).rounded(.up)))
        return "\(who) controlling your Mac · \(left) min left"
    }

    public static func askTitle(desk: String) -> String {
        "\(MacGrantCopy.displayName(desk)) wants to control this Mac"
    }

    public static let askBody = "Clicks, typing and keys for 30 minutes. You'll see a banner the whole time; "
        + "press \(hotkey) or move your mouse to stop it."

    public static let offDetail = "Mac control is off for you. Ask the owner to turn on \"\(switchTitle)\" in "
        + "Shaliach on his Mac (a card asking him is on his screen); do not retry until you are told it is on."

    public static let fullAccessForPhone = "Turn on Full access on your Mac (Shaliach → Settings → Mac) "
        + "to control it from your phone."

    public static let ownerActive = "The owner is using his Mac right now. Wait a few seconds, take a fresh "
        + "screenshot and retry."

    public static let secureField = "A password field has focus. Shaliach never types into password fields; "
        + "ask the owner to type it himself."

    public static let noAccessibility = "macOS has not let Shaliach control the mouse and keyboard. The owner "
        + "must allow it in System Settings → Privacy & Security → Accessibility → Shaliach."
}

// MARK: - one gesture

public enum MacMouseButton: String, Equatable, Sendable { case left, middle, right }

/// One `input` job, checked. Coordinates are in `space` (the sender's picture).
public enum MacInputGesture: Equatable, Sendable {
    case click(x: Int, y: Int, button: MacMouseButton, count: Int, space: MacSpace)
    case move(x: Int, y: Int, space: MacSpace)
    case drag(x: Int, y: Int, toX: Int, toY: Int, button: MacMouseButton, space: MacSpace)
    /// `dy > 0` is up, `dx > 0` is right, as every viewer sends.
    case scroll(x: Int, y: Int, dx: Int, dy: Int, space: MacSpace)
    case type(String)
    case key(String)

    public static let typeMax = 4096

    /// The picture a pointer gesture is in; nil for the keyboard.
    public var space: MacSpace? {
        switch self {
        case let .click(_, _, _, _, space), let .move(_, _, space), let .drag(_, _, _, _, _, space),
             let .scroll(_, _, _, _, space):
            return space
        case .type, .key:
            return nil
        }
    }

    public var isKeyboard: Bool {
        switch self {
        case .type, .key: return true
        default: return false
        }
    }

    public static func parse(_ a: MacJobArgs) throws -> MacInputGesture {
        func bad(_ detail: String) -> MacOpError { MacOpError("bad_input", detail) }
        func inside(_ x: Int?, _ y: Int?, _ space: MacSpace?) throws -> (Int, Int, MacSpace) {
            guard let space, space.width > 0, space.height > 0 else {
                throw bad("space is the pixel size of the picture the coordinates are in")
            }
            guard let x, let y, (0..<space.width).contains(x), (0..<space.height).contains(y) else {
                throw bad("(\(a.x.map(String.init) ?? "?"),\(a.y.map(String.init) ?? "?")) is outside the "
                          + "\(space.width)x\(space.height) picture")
            }
            return (x, y, space)
        }
        func button(_ raw: String?) throws -> MacMouseButton {
            guard let b = MacMouseButton(rawValue: raw ?? "left") else { throw bad("button is left, middle or right") }
            return b
        }
        switch a.action {
        case "type":
            guard let text = a.text, !text.isEmpty, text.count <= typeMax else { throw bad("nothing to type") }
            return .type(text)
        case "key":
            guard let key = a.key, MacKeyChord.parse(key) != nil else {
                throw bad("key is a name or chord like Return, Escape, cmd+s")
            }
            return .key(key)
        case "click":
            let (x, y, space) = try inside(a.x, a.y, a.space)
            let count = a.count ?? 1
            guard (1...3).contains(count) else { throw bad("count is 1, 2 or 3") }
            return .click(x: x, y: y, button: try button(a.button), count: count, space: space)
        case "move":
            let (x, y, space) = try inside(a.x, a.y, a.space)
            return .move(x: x, y: y, space: space)
        case "drag":
            let (x, y, space) = try inside(a.x, a.y, a.space)
            let (tx, ty, _) = try inside(a.toX, a.toY, a.space)
            return .drag(x: x, y: y, toX: tx, toY: ty, button: try button(a.button), space: space)
        case "scroll":
            let (x, y, space) = try inside(a.x, a.y, a.space)
            let dy = a.dy ?? 0, dx = a.dx ?? 0
            guard (dy == 0) != (dx == 0), abs(dy) <= 10, abs(dx) <= 10 else {
                throw bad("scroll needs exactly one of dy or dx, 1..10 notches")
            }
            return .scroll(x: x, y: y, dx: dx, dy: dy, space: space)
        default:
            throw bad("action is one of click, move, drag, scroll, type, key")
        }
    }
}

// MARK: - pixels to points

/// A display's frame in global points (CGDisplayBounds).
public struct MacDisplayFrame: Equatable, Sendable {
    public var x: Double, y: Double, width: Double, height: Double

    public init(x: Double, y: Double, width: Double, height: Double) {
        self.x = x
        self.y = y
        self.width = width
        self.height = height
    }
}

public enum MacInputMapping {
    /// A pixel of the sender's picture of `display`, as a global point.
    /// On Retina the picture has twice the pixels of the points CGEvent takes;
    /// a scaled-down screenshot has fewer. The ratio covers both.
    public static func point(x: Int, y: Int, space: MacSpace, display: MacDisplayFrame) -> (x: Double, y: Double) {
        let sx = display.width / Double(max(1, space.width))
        let sy = display.height / Double(max(1, space.height))
        return (display.x + Double(x) * sx, display.y + Double(y) * sy)
    }
}

// MARK: - keys

/// One key and its modifiers, as macOS virtual key codes (Carbon `kVK_*`).
public struct MacKeyChord: Equatable, Sendable {
    public struct Flags: OptionSet, Equatable, Sendable {
        public let rawValue: Int
        public init(rawValue: Int) { self.rawValue = rawValue }
        public static let shift = Flags(rawValue: 1)
        public static let control = Flags(rawValue: 2)
        public static let option = Flags(rawValue: 4)
        public static let command = Flags(rawValue: 8)
    }

    public var keyCode: UInt16
    public var flags: Flags

    public init(keyCode: UInt16, flags: Flags) {
        self.keyCode = keyCode
        self.flags = flags
    }

    static let modifiers: [String: Flags] = [
        "shift": .shift, "ctrl": .control, "control": .control, "alt": .option, "option": .option, "opt": .option,
        "cmd": .command, "command": .command, "super": .command, "meta": .command, "win": .command,
    ]

    /// X11 names (what every viewer sends) plus the obvious spellings.
    static let named: [String: UInt16] = [
        "return": 36, "enter": 36, "kp_enter": 76, "tab": 48, "backspace": 51, "escape": 53, "esc": 53,
        "delete": 117, "help": 114, "space": 49, "left": 123, "right": 124, "down": 125, "up": 126,
        "home": 115, "end": 119, "prior": 116, "page_up": 116, "pageup": 116, "next": 121, "page_down": 121,
        "pagedown": 121, "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
        "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        "minus": 27, "equal": 24, "bracketleft": 33, "bracketright": 30, "backslash": 42, "semicolon": 41,
        "apostrophe": 39, "comma": 43, "period": 47, "slash": 44, "grave": 50,
    ]

    /// Shifted punctuation: the base key plus Shift.
    static let shifted: [String: UInt16] = [
        "exclam": 18, "at": 19, "numbersign": 20, "dollar": 21, "percent": 23, "asciicircum": 22,
        "ampersand": 26, "asterisk": 28, "parenleft": 25, "parenright": 29, "underscore": 27, "plus": 24,
        "braceleft": 33, "braceright": 30, "bar": 42, "colon": 41, "quotedbl": 39, "less": 43,
        "greater": 47, "question": 44, "asciitilde": 50,
    ]

    /// ANSI letters and digits.
    static let letters: [Character: UInt16] = [
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
        "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
        "9": 25, "7": 26, "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40,
        "n": 45, "m": 46,
    ]

    /// `cmd+shift+t`, `Return`, `ctrl+minus` → one key and its flags; nil when
    /// any part is not a key this table knows (never a guess).
    public static func parse(_ chord: String) -> MacKeyChord? {
        let parts = chord.split(separator: "+", omittingEmptySubsequences: false).map(String.init)
        guard let last = parts.last, !last.isEmpty, parts.count <= 5 else { return nil }
        var flags: Flags = []
        for part in parts.dropLast() {
            guard let f = modifiers[part.lowercased()] else { return nil }
            flags.insert(f)
        }
        if let code = named[last.lowercased()] { return MacKeyChord(keyCode: code, flags: flags) }
        if let code = shifted[last.lowercased()] { return MacKeyChord(keyCode: code, flags: flags.union(.shift)) }
        if last.count == 1, let ch = last.first, let code = letters[Character(ch.lowercased())] {
            if ch.isUppercase { flags.insert(.shift) }
            return MacKeyChord(keyCode: code, flags: flags)
        }
        return nil
    }
}

// MARK: - the viewer

/// The live view of a Mac rides the desk screen viewer: an "agent" named
/// `mac:<node_id>` is read from `/v1/nodes/<node_id>/screen…` instead.
/// `mac:<node_id>@<display>` is the same Mac's display `<display>`: the
/// routes are the Mac's, with `?display=<display>`.
public enum MacScreenName {
    public static let prefix = "mac:"

    public static func agent(nodeId: String, display: Int? = nil) -> String {
        prefix + nodeId + (display.map { "@\($0)" } ?? "")
    }

    /// The node id, when this viewer name is a Mac.
    public static func nodeId(_ agent: String) -> String? {
        guard agent.hasPrefix(prefix) else { return nil }
        let rest = agent.dropFirst(prefix.count)
        return String(rest.split(separator: "@", maxSplits: 1, omittingEmptySubsequences: false).first ?? rest)
    }

    /// The display this viewer name picks; nil = the main one (or a desk).
    public static func display(_ agent: String) -> Int? {
        guard agent.hasPrefix(prefix), let at = agent.lastIndex(of: "@") else { return nil }
        return Int(agent[agent.index(after: at)...])
    }

    /// `?display=<id>` for a picked display, nothing otherwise.
    public static func query(_ agent: String) -> [URLQueryItem]? {
        display(agent).map { [URLQueryItem(name: "display", value: String($0))] }
    }

    /// `/v1/nodes/<id>` for a Mac, `/v1/agents/<name>` for a desk.
    public static func base(_ agent: String) -> String {
        nodeId(agent).map { "/v1/nodes/\($0)" } ?? "/v1/agents/\(agent)"
    }
}

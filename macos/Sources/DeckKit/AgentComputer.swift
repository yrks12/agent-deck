import Foundation

/// **The agent's computer, as far as looking at it goes.**
///
/// Every desk can have its own Linux machine: a container running an X display
/// with a Chromium the agent drives. `docs/client-api.md` §15 is the wire and
/// `server/screen.py` is the argument for its shape. Three routes, all behind
/// the bearer token this app already holds:
///
/// * `GET /v1/agents/{name}/screen` — is there a screen, and how big is it.
/// * `GET /v1/agents/{name}/screen.jpg` — one frame, captured now.
/// * `POST /v1/agents/{name}/screen/input` — one click, one keystroke, one
///   string typed.
///
/// Three properties of this file are the whole point, and each has a test.
///
/// **1. A frame without an age is a lie.** `server/screen.py` puts it exactly
/// that way: *"a checkout page looks the same a second old and twenty minutes
/// dead"*. So `AgentScreenFrame` never stores a picture without also storing
/// what the deck said its age was — and it keeps ageing it against this Mac's
/// clock afterwards, because the failure that matters is a poll that stopped
/// answering while the last frame stayed on screen. A frame that arrived with
/// no `X-Frame-Age` at all is `ageUnknown`, never live: defaulting a missing
/// age to zero is the same lie with a different cause.
///
/// **2. Nothing is clamped.** `screen/input` takes coordinates in the
/// display's own space, and the panel draws the JPEG at whatever size it has,
/// so a click is scaled back before it is sent. A point outside the drawn
/// picture is not sent at all. Measured against the deck on the box:
/// `{"action":"click","x":99999,"y":10}` answers `400 bad_input — click
/// (99999,10) is outside the 1280x800 screen`. The deck refuses on purpose so
/// a client's arithmetic bug is visible instead of clicking somewhere
/// plausible; a client that clamped would put the bug back.
///
/// **3. The poll costs nothing when nobody is looking.** Each JPEG is one
/// `ffmpeg` grab inside the container, on the same machine as the Chromium
/// being watched. `AgentComputerModel` polls only while the panel is on screen
/// *and* its window is key, and stops on either.
///
/// This file imports no UI framework, so all three are exercisable with no
/// window and no deck.

// MARK: - the status

/// `GET /v1/agents/{name}/screen`, decoded.
///
/// **`computer.running: false` is a 200, not a 404.** "This desk has no
/// machine yet" and "there is no such desk" are different things and the panel
/// draws them differently, so the route answers even when the machine is down.
/// Measured against `atlas` on the box, where no container has ever existed:
/// a 200 carrying the whole body with `running: false`.
public struct AgentScreenStatus: Equatable, Sendable, Decodable {
    public let desk: String
    public let isRunning: Bool
    /// `computer.waking`: the deck stopped this desk's idle browser and is
    /// starting it again because the screen was asked for. Absent on an older
    /// deck, which reads as not waking.
    public let isWaking: Bool
    public let image: String
    public let container: String
    /// The X display the capture is pointed at. Stated by the deck and never
    /// inferred — `server/screen.py` refuses to read `DISPLAY` from its own
    /// environment for the same reason.
    public let display: String
    /// The display's own size. **These are the numbers a click is in.** Read
    /// from here rather than from the JPEG: if the capture is ever scaled, the
    /// image size and the click space stop being the same number.
    public let width: Int
    public let height: Int
    /// The deck's line between "this is what the agent is looking at" and
    /// "this is a photograph of something that has gone". A constant in this
    /// app would be a second, drifting answer.
    public let staleAfter: TimeInterval
    public let generatedAt: Date
    /// A Mac's own refusal when it is not running (`mac_asleep`,
    /// `screen_recording_off`, ...), said in Mac words. Nil for a desk, whose
    /// "not running" is the plain no-computer state.
    public let macRefusal: ScreenRefusal?

    private struct Computer: Decodable {
        let running: Bool
        let waking: Bool?
        let image: String
        let container: String
    }

    private enum CodingKeys: String, CodingKey {
        case desk, computer, display, width, height
        case staleAfter = "stale_after"
        case generatedAt = "generated_at"
        case reason, detail
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let computer = try container.decode(Computer.self, forKey: .computer)
        desk = try container.decode(String.self, forKey: .desk)
        isRunning = computer.running
        isWaking = computer.waking ?? false
        image = computer.image
        self.container = computer.container
        display = try container.decode(String.self, forKey: .display)
        width = try container.decode(Int.self, forKey: .width)
        height = try container.decode(Int.self, forKey: .height)
        staleAfter = try container.decode(TimeInterval.self, forKey: .staleAfter)
        generatedAt = Date(timeIntervalSince1970:
                            try container.decode(TimeInterval.self, forKey: .generatedAt))
        let reason = (try? container.decodeIfPresent(String.self, forKey: .reason)) ?? nil
        let detail = (try? container.decodeIfPresent(String.self, forKey: .detail)) ?? nil
        macRefusal = reason.flatMap { ScreenRefusal.mac(reason: $0, detail: detail ?? "") }
    }

    public init(desk: String, isRunning: Bool, image: String, container: String,
                display: String, width: Int, height: Int,
                staleAfter: TimeInterval, generatedAt: Date, isWaking: Bool = false) {
        self.desk = desk
        self.isRunning = isRunning
        self.isWaking = isWaking
        self.image = image
        self.container = container
        self.display = display
        self.width = width
        self.height = height
        self.staleAfter = staleAfter
        self.generatedAt = generatedAt
        self.macRefusal = nil
    }
}

// MARK: - the frame

/// One JPEG, and the two facts that stop it being a lie: how old the deck said
/// it was, and when this Mac received it.
public struct AgentScreenFrame: Equatable, Sendable {
    public let jpeg: Data
    /// `X-Frame-Age` — seconds between the capture and the response.
    /// **Optional on purpose.** A frame that arrived without the header did not
    /// say how old it is, and turning that into `0` is precisely the claim the
    /// header exists to stop anyone making.
    public let serverAge: TimeInterval?
    /// `X-Frame-Display`.
    public let display: String
    public let receivedAt: Date
    /// The pixels, decoded off the main thread before the frame was handed to
    /// a view (`ScreenFrameDecoder`). Nil only when decoding failed; a view
    /// then decodes `jpeg` itself.
    public let decoded: DecodedScreenImage?

    public init(jpeg: Data, serverAge: TimeInterval?, display: String, receivedAt: Date,
                decoded: DecodedScreenImage? = nil) {
        self.jpeg = jpeg
        self.serverAge = serverAge
        self.display = display
        self.receivedAt = receivedAt
        self.decoded = decoded
    }

    func with(decoded: DecodedScreenImage?) -> AgentScreenFrame {
        AgentScreenFrame(jpeg: jpeg, serverAge: serverAge, display: display,
                         receivedAt: receivedAt, decoded: decoded)
    }

    /// How old this picture is *now*, or nil if it never said.
    ///
    /// The deck's age plus however long this client has been holding it. The
    /// second half is not decoration: when the poll stops answering, the last
    /// frame stays on screen, and this is the only arithmetic that notices.
    public func age(at now: Date) -> TimeInterval? {
        guard let serverAge else { return nil }
        return serverAge + max(0, now.timeIntervalSince(receivedAt))
    }
}

// MARK: - the states the panel draws

/// Every state the slot can be in. All five are drawn and all five are tested;
/// none of them is an error code on screen.
public enum AgentScreenState: Equatable, Sendable {
    /// Asked, nothing back yet. The reference product's own word for this slot.
    case connecting
    /// A frame the deck's own `stale_after` says is current.
    case live(AgentScreenFrame)
    /// A frame past `stale_after`. Kept — it is still worth seeing — and
    /// marked, because the difference between "the agent is thinking" and "the
    /// feed died" is the difference between waiting and going to look.
    case stale(AgentScreenFrame, age: TimeInterval)
    /// A frame that arrived with no `X-Frame-Age`. Same treatment as stale,
    /// different sentence: this one cannot say how old it is at all.
    case ageUnknown(AgentScreenFrame)
    /// `computer.running: false`. A state, not a failure.
    case noComputer
    /// `computer.running: false, waking: true`: the deck stopped the idle
    /// browser and is starting it again. Seconds, not a fault.
    case waking
    /// The machine, or the deck, refused. Said as a sentence.
    case unavailable(ScreenRefusal)

    /// Where the staleness rule lives, in one place, taking the deck's own
    /// number rather than a constant of this app's.
    public static func forFrame(_ frame: AgentScreenFrame,
                                staleAfter: TimeInterval,
                                now: Date) -> AgentScreenState {
        guard let age = frame.age(at: now) else { return .ageUnknown(frame) }
        return age < staleAfter ? .live(frame) : .stale(frame, age: age)
    }
}

/// What the panel puts on screen and says out loud, worked out once, away from
/// SwiftUI so it can be asserted on.
///
/// **The spoken label is not the caption.** He glances at this panel; a screen
/// reader has to be told the same thing the picture tells a sighted glance,
/// which includes how old the picture is. A silent image is useless.
public struct AgentScreenPresentation: Equatable, Sendable {
    /// `Atlas's screen` — the caption under the thumbnail, as in the reference.
    public let caption: String
    /// The one line that says what is wrong, or nil when a live frame is
    /// showing and nothing is.
    public let statusLine: String?
    /// A **still** SF Symbol for the states with no picture. Deliberately not a
    /// spinner: see `AgentScreenPanel`.
    public let symbol: String?
    public let spokenLabel: String
    public let isFrameLive: Bool
    /// Open is offered over a machine that is running, and never over an empty
    /// slot — an Open button on a desk with no computer opens a black
    /// rectangle.
    public let showsOpen: Bool
    public let frame: AgentScreenFrame?

    public init(desk displayName: String, state: AgentScreenState) {
        caption = "\(displayName)'s screen"
        switch state {
        case .connecting:
            statusLine = "Connecting"
            symbol = "antenna.radiowaves.left.and.right"
            spokenLabel = "\(caption). Connecting."
            isFrameLive = false
            showsOpen = false
            frame = nil
        case .live(let shot):
            statusLine = nil
            symbol = nil
            // Live means inside the deck's own `stale_after` and the poll runs
            // at 1 Hz, so a frame in this state is a second old at most in
            // practice; the moment it is not, the next tick makes it `.stale`.
            spokenLabel = "\(caption), live. Captured just now."
            isFrameLive = true
            showsOpen = true
            frame = shot
        case .stale(let shot, let age):
            let phrase = Self.agePhrase(age)
            statusLine = "Last seen \(phrase)"
            symbol = "clock.badge.exclamationmark"
            spokenLabel = "\(caption), last captured \(phrase). This is not what "
                + "\(displayName) is looking at now."
            isFrameLive = false
            showsOpen = true
            frame = shot
        case .ageUnknown(let shot):
            statusLine = "Age unknown"
            symbol = "questionmark.circle"
            spokenLabel = "\(caption). This picture did not say how old it is, so it "
                + "may not be current."
            isFrameLive = false
            showsOpen = true
            frame = shot
        case .noComputer:
            statusLine = "No computer yet"
            symbol = "desktopcomputer.trianglebadge.exclamationmark"
            spokenLabel = "\(displayName) has no computer running yet, so there is "
                + "nothing to watch."
            isFrameLive = false
            showsOpen = false
            frame = nil
        case .waking:
            statusLine = "Waking browser…"
            symbol = "sunrise"
            spokenLabel = "\(displayName)'s browser is waking up. It will appear in a "
                + "few seconds."
            isFrameLive = false
            showsOpen = false
            frame = nil
        case .unavailable(let refusal):
            statusLine = refusal.shortLine
            symbol = "exclamationmark.triangle"
            spokenLabel = "\(caption) is not available. \(refusal.sentence)"
            isFrameLive = false
            showsOpen = false
            frame = nil
        }
    }

    /// "just now", "40 seconds ago", "4 minutes ago". Read aloud as well as
    /// drawn, so it is words and never a number with a unit stuck on it.
    public static func agePhrase(_ seconds: TimeInterval) -> String {
        let value = max(0, seconds)
        if value < 2 { return "just now" }
        if value < 60 { return "\(Int(value)) seconds ago" }
        let minutes = Int(value / 60)
        if minutes == 1 { return "a minute ago" }
        if minutes < 60 { return "\(minutes) minutes ago" }
        let hours = minutes / 60
        return hours == 1 ? "an hour ago" : "\(hours) hours ago"
    }
}

// MARK: - refusals

/// What the screen routes refuse with, as sentences.
///
/// A separate type from `DeckError` on purpose: these six slugs are this
/// surface's own, each one a different thing for him to do — `docker
/// unavailable` is "start Docker", `computer not running` is "this desk has no
/// machine yet", and `no frame` is "the machine is up and the capture failed",
/// which is the one worth looking at.
public enum ScreenRefusal: Error, Equatable, Sendable {
    case noComputerYet(String)
    case noFrame(String)
    case dockerUnavailable(String)
    case notResponding(String)
    case inputRefused(String)
    case badInput(String)
    case unknownAgent(String)
    case unauthorized
    case authNotConfigured
    case missingToken
    case transport(String)
    /// His Mac, in Mac words: what it is missing and where to fix it. The
    /// deck's detail is the whole sentence (it names the Mac).
    case macAsleep(String)
    case macPaused(String)
    case screenRecordingOff(String)
    case accessibilityOff(String)
    /// His phone's taps need the Mac in Full access (a leaked token alone must not drive it).
    case fullAccessRequired(String)
    /// Watching just started; the Mac's first picture is a second away.
    case framePending(String)
    case other(status: Int, reason: String, detail: String)

    /// The Mac-only reasons, or nil for anything a desk could also say.
    public static func mac(reason: String, detail: String) -> ScreenRefusal? {
        switch reason {
        case "mac_asleep": return .macAsleep(detail)
        case "mac_paused": return .macPaused(detail)
        case "screen_recording_off": return .screenRecordingOff(detail)
        case "accessibility_off": return .accessibilityOff(detail)
        case "full_access_required": return .fullAccessRequired(detail)
        case "frame_pending": return .framePending(detail)
        default: return nil
        }
    }

    public init(status: Int, body: Data) {
        struct Envelope: Decodable { let reason: String?; let detail: String? }
        let envelope = try? JSONDecoder().decode(Envelope.self, from: body)
        let reason = envelope?.reason ?? ""
        let detail = envelope?.detail ?? ""

        if let mac = Self.mac(reason: reason, detail: detail) {
            self = mac
            return
        }
        switch reason {
        case "computer_not_running": self = .noComputerYet(detail)
        case "no_frame": self = .noFrame(detail)
        case "docker_unavailable": self = .dockerUnavailable(detail)
        case "computer_not_responding": self = .notResponding(detail)
        case "input_refused": self = .inputRefused(detail)
        case "bad_input", "bad_path": self = .badInput(detail)
        case "unknown_agent": self = .unknownAgent(detail)
        case "unauthorized": self = .unauthorized
        case "auth_not_configured": self = .authNotConfigured
        default:
            switch status {
            case 401, 403: self = .unauthorized
            case 503 where reason.isEmpty: self = .authNotConfigured
            default: self = .other(status: status, reason: reason, detail: detail)
            }
        }
    }

    /// The whole sentence, with the deck's own detail kept: "the machine is not
    /// up" without naming the container is half a message.
    public var sentence: String {
        switch self {
        case .noComputerYet(let detail):
            return withDetail("This desk has no computer running yet.", detail)
        case .noFrame(let detail):
            return withDetail("The computer is up, but taking a picture of its "
                              + "screen failed.", detail)
        case .dockerUnavailable(let detail):
            return withDetail("Docker is not answering on the deck's machine, so no "
                              + "desk has a computer right now.", detail)
        case .notResponding(let detail):
            return withDetail("The computer stopped answering.", detail)
        case .inputRefused(let detail):
            return withDetail("The computer would not take that click or keystroke.",
                              detail)
        case .badInput(let detail):
            return withDetail("The deck refused that gesture.", detail)
        case .unknownAgent(let detail):
            return withDetail("That desk is no longer on this deck.", detail)
        case .unauthorized:
            return "The deck rejected this token. Set a different one in Settings."
        case .authNotConfigured:
            return "This deck is closed until its own API token is set on the server."
        case .missingToken:
            return "Add your deck API token in Settings."
        case .transport(let detail):
            return withDetail("Could not reach the deck.", detail)
        case .macAsleep(let detail):
            return orDefault(detail, "Your Mac is asleep or offline. Wake it and open Agent Deck on it.")
        case .macPaused(let detail):
            return orDefault(detail, "Agent Deck is paused on your Mac. Resume it there.")
        case .screenRecordingOff(let detail):
            return orDefault(detail, "Screen Recording is off for Agent Deck. On the Mac open "
                             + "Agent Deck Settings → Mac and turn it on.")
        case .accessibilityOff(let detail):
            return orDefault(detail, "Accessibility is off for Agent Deck, so it cannot click or type. "
                             + "On the Mac open Agent Deck Settings → Mac and turn it on.")
        case .fullAccessRequired(let detail):
            return orDefault(detail, MacControlCopy.fullAccessForPhone)
        case .framePending(let detail):
            return orDefault(detail, "Waiting for your Mac's first picture.")
        case .other(let status, _, let detail):
            return withDetail("The deck answered \(status).", detail)
        }
    }

    private func orDefault(_ detail: String, _ fallback: String) -> String {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? fallback : trimmed
    }

    /// The caption-sized version, for the line under the empty slot.
    public var shortLine: String {
        switch self {
        case .noComputerYet: return "No computer yet"
        case .noFrame: return "No picture"
        case .dockerUnavailable: return "Docker is not answering"
        case .notResponding: return "Not answering"
        case .inputRefused: return "Refused"
        case .badInput: return "Refused"
        case .unknownAgent: return "Desk is gone"
        case .unauthorized, .authNotConfigured, .missingToken: return "Not allowed"
        case .transport: return "Cannot reach the deck"
        case .macAsleep: return "Your Mac is asleep"
        case .macPaused: return "Agent Deck is paused"
        case .screenRecordingOff: return "Screen Recording is off"
        case .accessibilityOff: return "Accessibility is off"
        case .fullAccessRequired: return "Needs Full access"
        case .framePending: return "Waiting for your Mac"
        case .other: return "Unavailable"
        }
    }

    private func withDetail(_ sentence: String, _ detail: String) -> String {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? sentence : "\(sentence) \(trimmed)"
    }
}

// MARK: - the gesture

/// Which button. The route's own numbering, so there is one place that knows
/// `3` means right and no `2` floating loose in a view.
public enum MouseButton: Int, Equatable, Sendable {
    case left = 1
    case middle = 2
    case right = 3
}

/// One take-over gesture. `POST /v1/agents/{name}/screen/input` takes exactly
/// one per call and there is no `run` action on this route, by design.
///
/// **This is the vocabulary, and the vocabulary was the defect.** With only
/// `click`, `type` and `key` a person can see the agent's desktop and not use
/// it: no context menu, no double click to open anything, no wheel to reach the
/// bottom of a page, no drag to select a line of text or move a window.
public enum ScreenInput: Equatable, Sendable, Encodable {
    /// **Display coordinates.** Not the drawn image's — see `ScreenFit`.
    /// `button` is 1/2/3 and `count` is 1/2/3; anything else is a 400.
    case click(x: Int, y: Int, button: Int, count: Int)
    /// The pointer, moved and not pressed. Hovers open menus and reveal
    /// tooltips, and without it half of a web page never responds.
    case move(x: Int, y: Int)
    /// The wheel. **Exactly one of `dy`/`dx` goes on the wire** — the route
    /// refuses both — so a zero axis is omitted rather than sent as a zero.
    /// `dy < 0` is down, `dy > 0` is up, `dx > 0` is right, magnitude 1...10.
    case scroll(x: Int, y: Int, dy: Int, dx: Int)
    /// Press at one point, release at another. Selecting text, moving a window,
    /// dragging a file — none of which a click can express.
    case drag(x: Int, y: Int, toX: Int, toY: Int, button: Int)
    /// Sent verbatim. He types passwords through this: it goes down as a single
    /// argument, never through a shell, so a backtick, a `$` or a leading dash
    /// is a character. This client does not escape it and does not trim it.
    case type(String)
    /// A keysym or a chord — `Return`, `Tab`, `ctrl+l`. **Every one this app
    /// emits has to match the deck's `^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$`**, so
    /// they all come out of `ScreenKeys` and never out of a string literal in a
    /// view.
    case key(String)

    /// The plain left single click, kept as its own spelling so the common call
    /// does not have to restate the two defaults every time.
    public static func click(x: Int, y: Int) -> ScreenInput {
        .click(x: x, y: y, button: MouseButton.left.rawValue, count: 1)
    }

    public static func scroll(x: Int, y: Int, dy: Int) -> ScreenInput {
        .scroll(x: x, y: y, dy: dy, dx: 0)
    }

    public static func scroll(x: Int, y: Int, dx: Int) -> ScreenInput {
        .scroll(x: x, y: y, dy: 0, dx: dx)
    }

    private enum CodingKeys: String, CodingKey {
        case action, x, y, text, key, button, count, dy, dx
        case toX = "to_x"
        case toY = "to_y"
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .click(let x, let y, let button, let count):
            try container.encode("click", forKey: .action)
            try container.encode(x, forKey: .x)
            try container.encode(y, forKey: .y)
            try container.encode(button, forKey: .button)
            try container.encode(count, forKey: .count)
        case .move(let x, let y):
            try container.encode("move", forKey: .action)
            try container.encode(x, forKey: .x)
            try container.encode(y, forKey: .y)
        case .scroll(let x, let y, let dy, let dx):
            try container.encode("scroll", forKey: .action)
            try container.encode(x, forKey: .x)
            try container.encode(y, forKey: .y)
            // One axis, never both and never a zero standing in for "no". The
            // route reads a present key as an instruction.
            if dy != 0 { try container.encode(dy, forKey: .dy) }
            if dx != 0 { try container.encode(dx, forKey: .dx) }
        case .drag(let x, let y, let toX, let toY, let button):
            try container.encode("drag", forKey: .action)
            try container.encode(x, forKey: .x)
            try container.encode(y, forKey: .y)
            try container.encode(toX, forKey: .toX)
            try container.encode(toY, forKey: .toY)
            try container.encode(button, forKey: .button)
        case .type(let text):
            try container.encode("type", forKey: .action)
            try container.encode(text, forKey: .text)
        case .key(let key):
            try container.encode("key", forKey: .action)
            try container.encode(key, forKey: .key)
        }
    }
}

/// **The wheel, in the route's units.**
///
/// A trackpad reports a continuous delta and `screen/input` takes whole notches
/// in `1...10`. A fling is thousands of pixels; sent as itself it is a `400` and
/// the page does not move at all. The cap is not a clamp of a coordinate — it is
/// how many notches one gesture is worth, decided here so there is one answer.
public enum ScreenScroll {
    public static let maxNotches = 10

    /// Lines of wheel travel to notches, sign preserved. Any real movement is
    /// worth at least one notch: rounding a small flick to zero is a wheel that
    /// does nothing, which is the complaint.
    public static func notches(fromWheelDelta delta: Double) -> Int {
        guard delta != 0, delta.isFinite else { return 0 }
        let size = min(maxNotches, max(1, Int(abs(delta).rounded(.up))))
        return delta < 0 ? -size : size
    }
}

// MARK: - the keyboard

/// One press, as a value. **Not an `NSEvent`** — this file imports no UI
/// framework, and the whole point of the mapping below is that it can be run
/// against a table of thirty keys with no window in the room.
///
/// The two strings are AppKit's two: `characters` is what the key produced with
/// its modifiers applied (`ctrl+c` produces `U+0003`), `unmodified` is what it
/// would have produced without them (`c`). The chord needs the second one; the
/// typed text needs the first.
public struct KeyStroke: Equatable, Sendable {
    public let keyCode: UInt16
    public let characters: String
    public let unmodified: String
    public let control: Bool
    public let option: Bool
    public let command: Bool
    public let shift: Bool

    public init(keyCode: UInt16, characters: String = "", unmodified: String = "",
                control: Bool = false, option: Bool = false,
                command: Bool = false, shift: Bool = false) {
        self.keyCode = keyCode
        self.characters = characters
        self.unmodified = unmodified
        self.control = control
        self.option = option
        self.command = command
        self.shift = shift
    }
}

/// **A Mac keyboard, turned into keysyms the deck will accept.**
///
/// The route matches every `key` against `^[A-Za-z0-9][A-Za-z0-9_+]{0,39}$` and
/// answers `400 bad_input` to anything else — so `Return` is fine and `\r`,
/// `⌘`, `Page Up` and `ctrl+-` are all a dead key in the take-over. Nothing in
/// a view is allowed to build a keysym by hand; it comes from here, and
/// `TakeoverInputTests` runs a table of the keys a person actually presses
/// through this and matches the deck's own regex against every result. That is
/// the whole `400 bad_input` class retired without a server.
///
/// **Command is never forwarded.** ⌘Q, ⌘W and ⌘, belong to the Mac he is
/// sitting at; a take-over that swallowed them would close the app out from
/// under him and the container would not want them anyway — Linux chords are
/// `ctrl`.
public enum ScreenKeys {

    /// Keys with no character of their own, by macOS virtual key code. The
    /// names are X11's, which is what the deck's `xdotool` speaks.
    ///
    /// Space is deliberately absent: it has a character, he types it, and
    /// naming it would send `space` for every word break. It is only ever named
    /// as part of a chord, below.
    public static let namedKeys: [UInt16: String] = [
        36: "Return", 76: "KP_Enter", 48: "Tab", 51: "BackSpace", 53: "Escape",
        117: "Delete", 114: "Help",
        123: "Left", 124: "Right", 125: "Down", 126: "Up",
        115: "Home", 119: "End", 116: "Prior", 121: "Next",
        122: "F1", 120: "F2", 99: "F3", 118: "F4", 96: "F5", 97: "F6",
        98: "F7", 100: "F8", 101: "F9", 109: "F10", 103: "F11", 111: "F12",
    ]

    /// X11's names for the punctuation a chord can land on. `ctrl+-` is a 400;
    /// `ctrl+minus` is a zoom out.
    public static let punctuationKeysyms: [Character: String] = [
        " ": "space", "-": "minus", "=": "equal", "[": "bracketleft",
        "]": "bracketright", "\\": "backslash", ";": "semicolon",
        "'": "apostrophe", ",": "comma", ".": "period", "/": "slash",
        "`": "grave", "!": "exclam", "@": "at", "#": "numbersign",
        "$": "dollar", "%": "percent", "^": "asciicircum", "&": "ampersand",
        "*": "asterisk", "(": "parenleft", ")": "parenright", "_": "underscore",
        "+": "plus", "{": "braceleft", "}": "braceright", "|": "bar",
        ":": "colon", "\"": "quotedbl", "<": "less", ">": "greater",
        "?": "question", "~": "asciitilde",
    ]

    /// The gesture one press is worth, or nil when this app has nothing legal
    /// to say for it.
    ///
    /// **Nil is a decision, not a gap.** A key with no keysym this deck accepts
    /// is dropped here rather than guessed at, because the guess is a `400` and
    /// a `400` is a red banner on his screen for a key he did not mean to press.
    public static func input(for stroke: KeyStroke) -> ScreenInput? {
        if stroke.command { return nil }

        if let name = namedKeys[stroke.keyCode] {
            return .key(modifierPrefix(stroke) + name)
        }

        // Control and Option make it a chord; Shift alone does not, because a
        // shifted letter is simply a capital and he wants it typed.
        if stroke.control || stroke.option {
            guard let base = keysym(for: stroke.unmodified) else { return nil }
            return .key(modifierPrefix(stroke) + base)
        }

        let text = stroke.characters
        guard !text.isEmpty, text.unicodeScalars.allSatisfy(isTypable) else { return nil }
        return .type(text)
    }

    /// `ctrl+alt+shift+`, in that order, so the same chord is always spelled the
    /// same way.
    public static func modifierPrefix(_ stroke: KeyStroke) -> String {
        var parts: [String] = []
        if stroke.control { parts.append("ctrl") }
        if stroke.option { parts.append("alt") }
        if stroke.shift { parts.append("shift") }
        return parts.isEmpty ? "" : parts.joined(separator: "+") + "+"
    }

    /// The keysym for the base of a chord.
    public static func keysym(for characters: String) -> String? {
        guard characters.count == 1, let character = characters.first else { return nil }
        if character.isASCII && (character.isLetter || character.isNumber) {
            return String(character)
        }
        return punctuationKeysyms[character]
    }

    /// A character he meant to type, rather than a control code the key
    /// happened to produce. `U+0003` is what ⌃C emits and typing it verbatim
    /// would put a byte on the agent's terminal that it reads as an interrupt.
    private static func isTypable(_ scalar: Unicode.Scalar) -> Bool {
        scalar.value >= 0x20 && scalar.value != 0x7F
    }
}

/// A point on the agent's display. Never a point in the view.
public struct DisplayPoint: Equatable, Sendable {
    public let x: Int
    public let y: Int
    public init(x: Int, y: Int) {
        self.x = x
        self.y = y
    }
}

/// **Where the picture actually is inside the view, and how to get back out of
/// it.**
///
/// The JPEG is drawn to fit, so it letterboxes: at a 400x400 slot a 1280x800
/// display is 400x250 with 75pt of nothing above and below. Those bars are not
/// the agent's screen and a click on one is not a click the deck should ever be
/// asked to make — `displayPoint` answers nil for them, and the caller sends
/// nothing.
///
/// Nothing here clamps. The deck refuses an off-screen coordinate with a 400
/// rather than clicking somewhere plausible, and a client that clamped would
/// hide its own arithmetic bug behind a click that landed in the wrong place.
public struct ScreenFit: Equatable, Sendable {
    public let displayWidth: Int
    public let displayHeight: Int
    public let drawnX: Double
    public let drawnY: Double
    public let drawnWidth: Double
    public let drawnHeight: Double

    /// Nil when there is nothing to draw into, or nothing to draw — a zero
    /// dimension has no transform and must not become a divide by zero that
    /// sends a click to (0,0).
    public init?(displayWidth: Int, displayHeight: Int,
                 viewWidth: Double, viewHeight: Double) {
        guard displayWidth > 0, displayHeight > 0,
              viewWidth > 0, viewHeight > 0 else { return nil }
        self.displayWidth = displayWidth
        self.displayHeight = displayHeight

        let scale = min(viewWidth / Double(displayWidth), viewHeight / Double(displayHeight))
        drawnWidth = Double(displayWidth) * scale
        drawnHeight = Double(displayHeight) * scale
        drawnX = (viewWidth - drawnWidth) / 2
        drawnY = (viewHeight - drawnHeight) / 2
    }

    /// How much smaller than life the picture is drawn.
    public var scale: Double { drawnWidth / Double(displayWidth) }

    /// The display pixel under a point in the view, or nil if that point is not
    /// on the picture at all.
    public func displayPoint(atViewX x: Double, y: Double) -> DisplayPoint? {
        let localX = x - drawnX
        let localY = y - drawnY
        guard localX >= 0, localX < drawnWidth,
              localY >= 0, localY < drawnHeight else { return nil }
        let px = Int((localX / drawnWidth) * Double(displayWidth))
        let py = Int((localY / drawnHeight) * Double(displayHeight))
        // Floating point, not policy: the arithmetic above is already inside
        // the display for every input this guard admits, and this only stops a
        // rounding landing exactly on the width. It is not a clamp of an
        // out-of-range request — those are refused above, unsent.
        guard px >= 0, px < displayWidth, py >= 0, py < displayHeight else { return nil }
        return DisplayPoint(x: px, y: py)
    }
}

// MARK: - the client

/// The three screen routes.
///
/// Its own protocol rather than three more members on `DeckClient`: every
/// existing conformer would otherwise have to grow a stub for a surface it has
/// nothing to do with, and a stub that throws is a silent failure waiting for
/// a caller.
public protocol AgentScreenClient: Sendable {
    func screenStatus(agent: String) async throws -> AgentScreenStatus
    func screenFrame(agent: String) async throws -> AgentScreenFrame
    func sendScreenInput(agent: String, _ input: ScreenInput) async throws
}

// MARK: - the poll

/// The wait between frames, injected so a test can spend six ticks in no time
/// at all instead of racing a real second.
public protocol ScreenPollClock: Sendable {
    func wait(_ seconds: TimeInterval) async throws
}

public struct TaskPollClock: ScreenPollClock {
    public init() {}
    public func wait(_ seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
    }
}

/// Why something is watching, which is the same question as how often it needs
/// a new picture.
public enum WatchRate: Equatable, Sendable {
    /// A glance in the inspector.
    case thumbnail
    /// His hands on the machine.
    case takeover
}

/// **The panel's own model. It does not touch `DeckStore`.**
///
/// That is deliberate and it is the performance rule: `DeckStore.composerDraft`
/// is `@Published`, so anything that publishes on the store rebuilds every view
/// observing it — one rebuild per character he types. A 1 Hz image poll that
/// republished the store would be that defect at 60 times the rate. This object
/// publishes only to the panel that owns it.
@MainActor
public final class AgentComputerModel: ObservableObject {
    /// The wire name, used to build the routes.
    public let desk: String
    /// The name a person reads, used in every sentence.
    public let displayName: String

    @Published public private(set) var state: AgentScreenState = .connecting
    /// The last refusal from a click or a keystroke, so a take-over that was
    /// rejected says so instead of looking like nothing happened.
    @Published public private(set) var lastInputRefusal: ScreenRefusal?

    public private(set) var status: AgentScreenStatus?

    private let client: AgentScreenClient
    private let clock: ScreenPollClock
    /// **Two rates, because there are two reasons to be looking.**
    ///
    /// A thumbnail in the inspector is a glance: one frame every 2 s is plenty and every tick
    /// costs a `docker exec ffmpeg` in the container. The open take-over is his
    /// hands on the machine, and at 1 Hz he cannot see the result of his own
    /// click — which is the whole complaint. One interval for both is either a
    /// dead take-over or a box grabbing frames all day for pictures nobody is
    /// driving, so there are two.
    private let thumbnailInterval: TimeInterval
    private let takeoverInterval: TimeInterval
    private let now: @Sendable () -> Date

    private var lastFrame: AgentScreenFrame?
    private var thumbnailWatchers = 0
    private var takeoverWatchers = 0
    private var isWindowActive = false
    private var pollTask: Task<Void, Never>?
    /// Which interval the loop that is actually running was started with, and a
    /// counter that tells a loop whether it is still the current one. A rate
    /// change restarts the poll rather than waiting out the tick it is in, and
    /// the outgoing loop must not clear the incoming one's handle on its way out.
    private var runningInterval: TimeInterval?
    private var pollGeneration = 0

    /// The stream, when the client can open one (`ScreenStream.swift`). While
    /// a socket is open, frames arrive on it and input leaves on it; polling
    /// is what happens when it cannot open or the deck says to poll.
    private let streaming: AgentScreenStreaming?
    private let decode: @Sendable (Data) async -> DecodedScreenImage?
    private var session: ScreenStreamSession?
    private var inputSeq = 0
    /// After a stream fails, poll this long before trying it again.
    private let streamRetry: TimeInterval
    private var streamRetryAt: Date = .distantPast

    public init(desk: String,
                displayName: String,
                client: AgentScreenClient,
                interval: TimeInterval = 2.0,
                takeoverInterval: TimeInterval = 0.25,
                clock: ScreenPollClock = TaskPollClock(),
                now: @escaping @Sendable () -> Date = { Date() },
                streamRetry: TimeInterval = 10,
                decode: @escaping @Sendable (Data) async -> DecodedScreenImage? = {
                    await ScreenFrameDecoder.decode($0)
                }) {
        self.desk = desk
        self.displayName = displayName
        self.client = client
        self.streaming = client as? AgentScreenStreaming
        self.decode = decode
        self.streamRetry = streamRetry
        self.thumbnailInterval = interval
        self.takeoverInterval = takeoverInterval
        self.clock = clock
        self.now = now
    }

    deinit { pollTask?.cancel() }

    public var presentation: AgentScreenPresentation {
        AgentScreenPresentation(desk: displayName, state: state)
    }

    /// The size a click has to be scaled back into, once the status is known.
    public func fit(inViewWidth width: Double, height: Double) -> ScreenFit? {
        guard let status else { return nil }
        return ScreenFit(displayWidth: status.width, displayHeight: status.height,
                         viewWidth: width, viewHeight: height)
    }

    // MARK: the gate

    public var isPolling: Bool { pollTask != nil }

    /// **One watcher is one thing looking at this screen.**
    ///
    /// Counted rather than a boolean, because two things look at once: the
    /// thumbnail in the inspector and, on top of it, the opened take-over. On
    /// screen one is in front of the other; in the view tree they are not
    /// nested, because a sheet is its own window and the panel underneath never
    /// disappears. With one flag between them the take-over closing switched
    /// off a thumbnail that was still on screen, and — having had its only
    /// `onAppear` already — it stayed frozen on that frame for the rest of the
    /// session.
    public func addWatcher(_ rate: WatchRate = .thumbnail) {
        switch rate {
        case .thumbnail: thumbnailWatchers += 1
        case .takeover: takeoverWatchers += 1
        }
        reconsider()
    }

    /// Never below none. A count driven negative by an unpaired disappear is a
    /// panel that can be on screen with the poll switched off and no way back.
    public func removeWatcher(_ rate: WatchRate = .thumbnail) {
        switch rate {
        case .thumbnail: thumbnailWatchers = max(0, thumbnailWatchers - 1)
        case .takeover: takeoverWatchers = max(0, takeoverWatchers - 1)
        }
        reconsider()
    }

    /// The wait this model is polling at right now: the fast one the moment
    /// anything has the machine open under his hands, the slow one otherwise.
    public var pollInterval: TimeInterval {
        takeoverWatchers > 0 ? takeoverInterval : thumbnailInterval
    }

    /// The other half of the gate. One JPEG a second is one `ffmpeg` grab
    /// inside the container, on the same machine as the Chromium being watched,
    /// and on a laptop — so the app going to the background stops it just as
    /// surely as closing the panel.
    public func setWindowActive(_ active: Bool) {
        guard active != isWindowActive else { return }
        isWindowActive = active
        reconsider()
    }

    private var shouldPoll: Bool {
        (thumbnailWatchers + takeoverWatchers) > 0 && isWindowActive
    }

    /// Starts, stops, and — the new one — **re-rates** the poll. Pressing Open
    /// over a thumbnail that is already polling has to speed it up there and
    /// then; a loop that only read the interval on its next tick would leave
    /// him a second of nothing at the moment he starts using the machine.
    private func reconsider() {
        guard shouldPoll else {
            pollGeneration += 1
            pollTask?.cancel()
            pollTask = nil
            runningInterval = nil
            return
        }
        let wanted = pollInterval
        if pollTask != nil, runningInterval == wanted { return }
        // A socket does not have a rate: frames come when the screen changes.
        // Re-rating must not tear it down and open another.
        // Only while the take-over is open: a thumbnail alone never holds a
        // socket (MEASURED: the inspector's thumbnail decoding ~10 frames a
        // second kept the idle app at 22-29% CPU), so the take-over closing
        // must end the stream and put the thumbnail back on its slow poll.
        if pollTask != nil, session != nil, takeoverWatchers > 0 {
            runningInterval = wanted
            return
        }

        pollGeneration += 1
        let generation = pollGeneration
        pollTask?.cancel()
        runningInterval = wanted
        pollTask = Task { [weak self] in
            await self?.runPoll(interval: wanted, generation: generation)
        }
    }

    /// Test seam: waits for the running loop to end. There is no timer to
    /// advance and no second to sleep through.
    public func pollLoopFinished() async {
        await pollTask?.value
    }

    private func runPoll(interval: TimeInterval, generation: Int) async {
        while !Task.isCancelled && shouldPoll && generation == pollGeneration {
            // The stream is for the take-over. A thumbnail is a glance and
            // polls; it never decodes a live feed nobody is looking at.
            if let streaming, takeoverWatchers > 0, status?.isRunning == true,
               now() >= streamRetryAt {
                await runStream(streaming, generation: generation)
                streamRetryAt = now().addingTimeInterval(streamRetry)
                continue
            }
            await pollOnce()
            do { try await clock.wait(runningInterval ?? interval) } catch { break }
        }
        // Only if this is still the loop in charge. A loop stood down for a
        // faster one must not clear the handle to its replacement.
        if generation == pollGeneration {
            pollTask = nil
            runningInterval = nil
        }
    }

    // MARK: the stream

    /// One socket, until it ends. Frames are decoded off the main thread and
    /// only then swapped in — the old picture stays up until the new one is
    /// ready, so there is never a blank between frames — and acknowledged
    /// after decoding, which is what makes the deck's backpressure real.
    private func runStream(_ streaming: AgentScreenStreaming, generation: Int) async {
        let opened: ScreenStreamSession
        do {
            opened = try await streaming.openScreenStream(agent: desk)
        } catch {
            return
        }
        guard !Task.isCancelled, generation == pollGeneration else {
            opened.close()
            return
        }
        session = opened
        defer {
            if session === opened { session = nil }
            opened.close()
        }
        await withTaskCancellationHandler {
            while !Task.isCancelled, generation == pollGeneration {
                let event: ScreenStreamEvent
                do { event = try await opened.next() } catch { return }
                switch event {
                case .hello:
                    continue
                case .frame(let seq, let age, let jpeg):
                    let decoded = await decode(jpeg)
                    guard !Task.isCancelled, generation == pollGeneration else { return }
                    let frame = AgentScreenFrame(jpeg: jpeg, serverAge: age,
                                                 display: status?.display ?? "",
                                                 receivedAt: now(), decoded: decoded)
                    lastFrame = frame
                    state = .forFrame(frame, staleAfter: status?.staleAfter ?? 30, now: now())
                    await opened.ack(seq)
                case .tick:
                    // Nothing changed: the picture on screen is the screen now.
                    if let last = lastFrame {
                        let fresh = AgentScreenFrame(jpeg: last.jpeg, serverAge: 0,
                                                     display: last.display, receivedAt: now(),
                                                     decoded: last.decoded)
                        lastFrame = fresh
                        state = .forFrame(fresh, staleAfter: status?.staleAfter ?? 30,
                                          now: now())
                    }
                case .inputResult(_, let refusal):
                    lastInputRefusal = refusal
                case .fallback(let reason):
                    if reason == "computer_not_running" { status = nil }
                    return
                }
            }
        } onCancel: {
            opened.close()
        }
    }

    // MARK: one tick

    /// One pass: keep the status honest, then take a picture if there is one to
    /// take.
    public func pollOnce() async {
        if status == nil || status?.isRunning == false {
            await refreshStatus()
        }
        guard let status, status.isRunning else { return }

        do {
            var frame = try await client.screenFrame(agent: desk)
            frame = frame.with(decoded: await decode(frame.jpeg))
            lastFrame = frame
            state = .forFrame(frame, staleAfter: status.staleAfter, now: now())
        } catch let refusal as ScreenRefusal {
            absorb(refusal, staleAfter: status.staleAfter)
        } catch {
            absorb(.transport(error.localizedDescription), staleAfter: status.staleAfter)
        }
    }

    /// A failed grab does not necessarily throw the picture away. One dropped
    /// frame out of a running feed is a blip and the last one is still true; a
    /// feed that has been failing long enough for the last frame to go stale is
    /// not a blip, and then the refusal is what he needs to read.
    private func absorb(_ refusal: ScreenRefusal, staleAfter: TimeInterval) {
        if case .noComputerYet = refusal {
            status = nil
            lastFrame = nil
            state = .noComputer
            return
        }
        if let last = lastFrame, let age = last.age(at: now()), age < staleAfter {
            state = .live(last)
        } else {
            lastFrame = nil
            state = .unavailable(refusal)
        }
    }

    public func refreshStatus() async {
        do {
            let fresh = try await client.screenStatus(agent: desk)
            status = fresh
            if !fresh.isRunning {
                lastFrame = nil
                if let mac = fresh.macRefusal {
                    state = .unavailable(mac)
                } else {
                    state = fresh.isWaking ? .waking : .noComputer
                }
            } else if lastFrame == nil {
                state = .connecting
            }
        } catch let refusal as ScreenRefusal {
            status = nil
            lastFrame = nil
            state = .unavailable(refusal)
        } catch {
            status = nil
            lastFrame = nil
            state = .unavailable(.transport(error.localizedDescription))
        }
    }

    // MARK: the take-over

    /// A click at a point in the panel's own coordinates. Scaled back into the
    /// display's space, and **not sent at all** when the point is outside the
    /// drawn picture.
    public func click(atViewX x: Double, y: Double, in fit: ScreenFit,
                      button: MouseButton = .left, count: Int = 1) async {
        guard let point = fit.displayPoint(atViewX: x, y: y) else { return }
        await send(.click(x: point.x, y: point.y,
                          button: button.rawValue, count: count))
    }

    /// The pointer, moved and not pressed — the gesture a hover menu needs.
    public func move(toViewX x: Double, y: Double, in fit: ScreenFit) async {
        guard let point = fit.displayPoint(atViewX: x, y: y) else { return }
        await send(.move(x: point.x, y: point.y))
    }

    /// **Both ends have to be on the picture.** The letterbox is not the
    /// agent's screen at either end of a drag, and the deck answers an
    /// off-screen coordinate with a 400 rather than clamping, on purpose — so a
    /// drag that starts or finishes on a bar is not sent at all.
    public func drag(fromViewX x: Double, y: Double,
                     toViewX toX: Double, y toY: Double,
                     in fit: ScreenFit, button: MouseButton = .left) async {
        guard let start = fit.displayPoint(atViewX: x, y: y),
              let end = fit.displayPoint(atViewX: toX, y: toY) else { return }
        await send(.drag(x: start.x, y: start.y, toX: end.x, toY: end.y,
                         button: button.rawValue))
    }

    /// The wheel, at the pointer. One axis reaches the deck: a diagonal flick
    /// on a trackpad reports both, the route takes one, and the dominant one is
    /// the one he meant.
    public func scroll(atViewX x: Double, y: Double,
                       wheelY: Double, wheelX: Double, in fit: ScreenFit) async {
        guard let point = fit.displayPoint(atViewX: x, y: y) else { return }
        var dy = ScreenScroll.notches(fromWheelDelta: wheelY)
        var dx = ScreenScroll.notches(fromWheelDelta: wheelX)
        guard dy != 0 || dx != 0 else { return }
        if dy != 0 && dx != 0 {
            if abs(wheelY) >= abs(wheelX) { dx = 0 } else { dy = 0 }
        }
        await send(.scroll(x: point.x, y: point.y, dy: dy, dx: dx))
    }

    /// One press of a real keyboard. `nil` back from `ScreenKeys` is a key this
    /// app has no legal keysym for, and it is dropped rather than guessed at.
    /// Returns whether anything was sent, so the view knows whether to let the
    /// event carry on to the rest of the responder chain.
    @discardableResult
    public func press(_ stroke: KeyStroke) async -> Bool {
        guard let input = ScreenKeys.input(for: stroke) else { return false }
        await send(input)
        return true
    }

    public func send(_ input: ScreenInput) async {
        if let session {
            inputSeq += 1
            do {
                try await session.send(input, id: inputSeq)
                lastInputRefusal = nil
                return
            } catch {
                // the socket is going; the gesture still goes, over HTTP
            }
        }
        do {
            try await client.sendScreenInput(agent: desk, input)
            lastInputRefusal = nil
        } catch let refusal as ScreenRefusal {
            lastInputRefusal = refusal
        } catch {
            lastInputRefusal = .transport(error.localizedDescription)
        }
    }
}

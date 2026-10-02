import Foundation

// MB1, the Mac's side of the wire (docs/mac-bridge.md). The server is strict
// about these shapes — see `server/mac_nodes.py` `record_events` — so every
// key is spelled here once and pinned by `MacWireTests`.

// MARK: - A JSON value (result payloads)

/// Any JSON value. Integers stay integers on the wire (`"size": 12`, not `12.0`).
public enum MacJSON: Equatable, Sendable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([MacJSON])
    case object([String: MacJSON])
}

extension MacJSON: Codable {
    public init(from decoder: Decoder) throws {
        let c = try decoder.singleValueContainer()
        if c.decodeNil() { self = .null }
        else if let b = try? c.decode(Bool.self) { self = .bool(b) }
        else if let i = try? c.decode(Int.self) { self = .int(i) }
        else if let d = try? c.decode(Double.self) { self = .double(d) }
        else if let s = try? c.decode(String.self) { self = .string(s) }
        else if let a = try? c.decode([MacJSON].self) { self = .array(a) }
        else { self = .object(try c.decode([String: MacJSON].self)) }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.singleValueContainer()
        switch self {
        case .null: try c.encodeNil()
        case .bool(let v): try c.encode(v)
        case .int(let v): try c.encode(v)
        case .double(let v): try c.encode(v)
        case .string(let v): try c.encode(v)
        case .array(let v): try c.encode(v)
        case .object(let v): try c.encode(v)
        }
    }
}

extension MacJSON: ExpressibleByNilLiteral, ExpressibleByBooleanLiteral, ExpressibleByIntegerLiteral,
    ExpressibleByFloatLiteral, ExpressibleByStringLiteral, ExpressibleByArrayLiteral, ExpressibleByDictionaryLiteral {
    public init(nilLiteral: ()) { self = .null }
    public init(booleanLiteral value: Bool) { self = .bool(value) }
    public init(integerLiteral value: Int) { self = .int(value) }
    public init(floatLiteral value: Double) { self = .double(value) }
    public init(stringLiteral value: String) { self = .string(value) }
    public init(arrayLiteral elements: MacJSON...) { self = .array(elements) }
    public init(dictionaryLiteral elements: (String, MacJSON)...) {
        self = .object(Dictionary(elements, uniquingKeysWith: { _, last in last }))
    }
}

// MARK: - POST /v1/nodes

public struct MacRegisterRequest: Encodable, Equatable, Sendable {
    public var machineId: String
    public var name: String
    public var os: String
    public var appVersion: String
    public var capabilities: [MacJobKind]
    /// `ask` · `full` · `paused` — never `off` (an Off Mac does not register).
    public var mode: MacMode

    enum CodingKeys: String, CodingKey {
        case name, os, capabilities, mode
        case machineId = "machine_id"
        case appVersion = "app_version"
    }

    public init(machineId: String, name: String, os: String, appVersion: String, capabilities: [MacJobKind], mode: MacMode) {
        self.machineId = machineId
        self.name = name
        self.os = os
        self.appVersion = appVersion
        self.capabilities = capabilities
        self.mode = mode
    }
}

public struct MacRegisterResponse: Decodable, Equatable, Sendable {
    public var nodeId: String
    /// Only on a pairing (201). Stored in the Keychain, never on disk.
    public var nodeSecret: String?
    public var name: String
    public var primary: Bool

    enum CodingKeys: String, CodingKey {
        case name, primary
        case nodeId = "node_id"
        case nodeSecret = "node_secret"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        nodeId = try c.decode(String.self, forKey: .nodeId)
        nodeSecret = try c.decodeIfPresent(String.self, forKey: .nodeSecret)
        name = try c.decodeIfPresent(String.self, forKey: .name) ?? ""
        primary = try c.decodeIfPresent(Bool.self, forKey: .primary) ?? false
    }

    public init(nodeId: String, nodeSecret: String?, name: String, primary: Bool) {
        self.nodeId = nodeId
        self.nodeSecret = nodeSecret
        self.name = name
        self.primary = primary
    }
}

// MARK: - POST /v1/nodes/{id}/poll

public struct MacWireGrant: Codable, Equatable, Sendable {
    public var state: MacGrant.State
    public var until: Double?

    public init(state: MacGrant.State, until: Double?) {
        self.state = state
        self.until = until
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(state, forKey: .state)
        try c.encode(until, forKey: .until)   // explicit null = no end
    }
}

public struct MacPollRequest: Encodable, Equatable, Sendable {
    public var wait: Double
    public var freeSlots: Int
    public var running: [String]
    public var mode: MacMode
    /// Informational (the desk's `status`); the Mac enforces.
    public var grants: [String: MacWireGrant]
    /// Mac control: the live grant (explicit null when none), the live view's
    /// pixel size, and the two permissions it needs. Mirrored by the deck.
    public var control: MacWireControl?
    public var screen: MacSpace?
    public var perms: MacPermissions?
    /// Every display, main first is not promised: `MacDisplays.ordered`.
    public var displays: [MacDisplayInfo]?

    enum CodingKeys: String, CodingKey {
        case wait, running, mode, grants, control, screen, perms, displays
        case freeSlots = "free_slots"
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(wait, forKey: .wait)
        try c.encode(freeSlots, forKey: .freeSlots)
        try c.encode(running, forKey: .running)
        try c.encode(mode, forKey: .mode)
        try c.encode(grants, forKey: .grants)
        try c.encodeIfPresent(control, forKey: .control)   // absent = no grant
        try c.encodeIfPresent(screen, forKey: .screen)
        try c.encodeIfPresent(perms, forKey: .perms)
        try c.encodeIfPresent(displays, forKey: .displays)
    }

    public init(wait: Double, freeSlots: Int, running: [String], mode: MacMode, grants: [String: MacWireGrant]) {
        self.wait = wait
        self.freeSlots = freeSlots
        self.running = running
        self.mode = mode
        self.grants = grants
    }
}

/// A job's `args`: one bag of optionals covering every kind (MB1 table).
public struct MacJobArgs: Codable, Equatable, Sendable {
    public var command: String?
    public var cwd: String?
    public var env: [String: String]?
    public var path: String?
    public var offset: Int?
    public var length: Int?
    public var encoding: String?
    public var content: String?
    public var mode: String?
    public var makeDirs: Bool?
    public var hidden: Bool?
    public var target: String?
    // `input` (Mac control): one gesture, coordinates in `space`.
    public var action: String?
    public var x: Int?
    public var y: Int?
    public var toX: Int?
    public var toY: Int?
    public var button: String?
    public var count: Int?
    public var dx: Int?
    public var dy: Int?
    public var text: String?
    public var key: String?
    public var space: MacSpace?
    /// `screenshot`: which display (nil = main).
    public var display: Int?

    enum CodingKeys: String, CodingKey {
        case command, cwd, env, path, offset, length, encoding, content, mode, hidden, target
        case action, x, y, button, count, dx, dy, text, key, space, display
        case makeDirs = "make_dirs"
        case toX = "to_x"
        case toY = "to_y"
    }

    public init(command: String? = nil, cwd: String? = nil, env: [String: String]? = nil, path: String? = nil,
                offset: Int? = nil, length: Int? = nil, encoding: String? = nil, content: String? = nil,
                mode: String? = nil, makeDirs: Bool? = nil, hidden: Bool? = nil, target: String? = nil,
                action: String? = nil, x: Int? = nil, y: Int? = nil, toX: Int? = nil, toY: Int? = nil,
                button: String? = nil, count: Int? = nil, dx: Int? = nil, dy: Int? = nil, text: String? = nil,
                key: String? = nil, space: MacSpace? = nil, display: Int? = nil) {
        self.display = display
        self.action = action
        self.x = x
        self.y = y
        self.toX = toX
        self.toY = toY
        self.button = button
        self.count = count
        self.dx = dx
        self.dy = dy
        self.text = text
        self.key = key
        self.space = space
        self.command = command
        self.cwd = cwd
        self.env = env
        self.path = path
        self.offset = offset
        self.length = length
        self.encoding = encoding
        self.content = content
        self.mode = mode
        self.makeDirs = makeDirs
        self.hidden = hidden
        self.target = target
    }
}

/// A job as delivered by a poll.
public struct MacWireJob: Codable, Equatable, Sendable {
    public var id: String
    public var desk: String
    /// Raw, so a kind this build does not know is refused rather than
    /// failing the whole poll.
    public var kind: String
    public var args: MacJobArgs
    public var timeoutS: Int
    public var background: Bool
    public var createdAt: Double

    public var jobKind: MacJobKind? { MacJobKind(rawValue: kind) }

    enum CodingKeys: String, CodingKey {
        case id, desk, kind, args, background
        case timeoutS = "timeout_s"
        case createdAt = "created_at"
    }

    public init(id: String, desk: String, kind: MacJobKind, args: MacJobArgs = MacJobArgs(), timeoutS: Int = 120,
                background: Bool = false, createdAt: Double = 0) {
        self.id = id
        self.desk = desk
        self.kind = kind.rawValue
        self.args = args
        self.timeoutS = timeoutS
        self.background = background
        self.createdAt = createdAt
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        desk = try c.decode(String.self, forKey: .desk)
        kind = try c.decode(String.self, forKey: .kind)
        args = (try? c.decodeIfPresent(MacJobArgs.self, forKey: .args)) ?? MacJobArgs()
        timeoutS = try c.decodeIfPresent(Int.self, forKey: .timeoutS) ?? 120
        background = try c.decodeIfPresent(Bool.self, forKey: .background) ?? false
        createdAt = try c.decodeIfPresent(Double.self, forKey: .createdAt) ?? 0
    }
}

public struct MacPollResponse: Decodable, Equatable, Sendable {
    public var jobs: [MacWireJob]
    public var cancel: [String]
    public var serverTs: Double
    /// Someone is looking at the live view: capture and push frames.
    public var watch: Bool
    /// Desks refused for `control_off`, waiting on his Allow.
    public var controlAsks: [String]
    /// Which displays are being watched; empty = the main one (an older deck).
    public var watchDisplays: [Int]

    enum CodingKeys: String, CodingKey {
        case jobs, cancel, watch
        case serverTs = "server_ts"
        case controlAsks = "control_asks"
        case watchDisplays = "watch_displays"
    }

    public init(jobs: [MacWireJob] = [], cancel: [String] = [], serverTs: Double = 0, watch: Bool = false,
                controlAsks: [String] = [], watchDisplays: [Int] = []) {
        self.watchDisplays = watchDisplays
        self.jobs = jobs
        self.cancel = cancel
        self.serverTs = serverTs
        self.watch = watch
        self.controlAsks = controlAsks
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        // One malformed job must not cost the others (or the cancel list).
        jobs = (try c.decodeIfPresent([Lossy<MacWireJob>].self, forKey: .jobs) ?? []).compactMap(\.value)
        cancel = try c.decodeIfPresent([String].self, forKey: .cancel) ?? []
        serverTs = try c.decodeIfPresent(Double.self, forKey: .serverTs) ?? 0
        watch = (try? c.decodeIfPresent(Bool.self, forKey: .watch)) ?? false
        controlAsks = (try? c.decodeIfPresent([String].self, forKey: .controlAsks)) ?? []
        watchDisplays = (try? c.decodeIfPresent([Int].self, forKey: .watchDisplays)) ?? []
    }

    private struct Lossy<T: Decodable>: Decodable {
        let value: T?
        init(from decoder: Decoder) throws { value = try? T(from: decoder) }
    }
}

// MARK: - POST /v1/nodes/{id}/jobs/{job_id}/events

public enum MacResultState: String, Codable, Sendable, CaseIterable {
    case done, failed, cancelled
    case timedOut = "timed_out"
    case refused
}

public struct MacStartedData: Codable, Equatable, Sendable {
    public var pid: Int32?
    public var cwd: String?
    public var sandboxed: Bool

    public init(pid: Int32?, cwd: String?, sandboxed: Bool) {
        self.pid = pid
        self.cwd = cwd
        self.sandboxed = sandboxed
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(pid, forKey: .pid)
        try c.encode(cwd, forKey: .cwd)
        try c.encode(sandboxed, forKey: .sandboxed)
    }
}

public struct MacJobResultData: Codable, Equatable, Sendable {
    public var state: MacResultState
    public var exit: Int32?
    /// `TERM` / `KILL` / nil.
    public var signal: String?
    public var reason: String?
    public var detail: String
    public var durationMs: Int
    public var payload: [String: MacJSON]

    enum CodingKeys: String, CodingKey {
        case state, exit, signal, reason, detail, payload
        case durationMs = "duration_ms"
    }

    public init(state: MacResultState, exit: Int32? = nil, signal: String? = nil, reason: String? = nil,
                detail: String = "", durationMs: Int = 0, payload: [String: MacJSON] = [:]) {
        self.state = state
        self.exit = exit
        self.signal = signal
        self.reason = reason
        self.detail = detail
        self.durationMs = durationMs
        self.payload = payload
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(state, forKey: .state)
        try c.encode(exit, forKey: .exit)
        try c.encode(signal, forKey: .signal)
        try c.encode(reason, forKey: .reason)
        try c.encode(detail, forKey: .detail)
        try c.encode(durationMs, forKey: .durationMs)
        try c.encode(payload, forKey: .payload)
    }
}

/// `{"seq", "type", "data"}`. `seq` is positive and increasing per job; the
/// server ignores one it has seen, so a retried batch is safe.
public struct MacJobEvent: Codable, Equatable, Sendable {
    public enum Body: Equatable, Sendable {
        case awaitingGrant
        case started(MacStartedData)
        case stdout(String)
        case stderr(String)
        case result(MacJobResultData)
    }

    public var seq: Int
    public var body: Body

    public init(seq: Int, body: Body) {
        self.seq = seq
        self.body = body
    }

    public var type: String {
        switch body {
        case .awaitingGrant: return "awaiting_grant"
        case .started: return "started"
        case .stdout: return "stdout"
        case .stderr: return "stderr"
        case .result: return "result"
        }
    }

    public var isResult: Bool { if case .result = body { return true } else { return false } }

    enum CodingKeys: String, CodingKey { case seq, type, data }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(seq, forKey: .seq)
        try c.encode(type, forKey: .type)
        switch body {
        case .awaitingGrant: break
        case .started(let d): try c.encode(d, forKey: .data)
        case .stdout(let s), .stderr(let s): try c.encode(s, forKey: .data)
        case .result(let r): try c.encode(r, forKey: .data)
        }
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        seq = try c.decode(Int.self, forKey: .seq)
        switch try c.decode(String.self, forKey: .type) {
        case "awaiting_grant": body = .awaitingGrant
        case "started": body = .started(try c.decode(MacStartedData.self, forKey: .data))
        case "stdout": body = .stdout(try c.decode(String.self, forKey: .data))
        case "stderr": body = .stderr(try c.decode(String.self, forKey: .data))
        case "result": body = .result(try c.decode(MacJobResultData.self, forKey: .data))
        case let other:
            throw DecodingError.dataCorruptedError(forKey: .type, in: c, debugDescription: "unknown event type \(other)")
        }
    }
}

public struct MacEventsRequest: Encodable, Equatable, Sendable {
    public var events: [MacJobEvent]
    public init(events: [MacJobEvent]) { self.events = events }
}

public struct MacEventsResponse: Decodable, Equatable, Sendable {
    public var ok: Bool
    /// The desk cancelled: kill now.
    public var cancel: Bool

    public init(ok: Bool = true, cancel: Bool = false) {
        self.ok = ok
        self.cancel = cancel
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        ok = try c.decodeIfPresent(Bool.self, forKey: .ok) ?? true
        cancel = try c.decodeIfPresent(Bool.self, forKey: .cancel) ?? false
    }

    enum CodingKeys: String, CodingKey { case ok, cancel }
}

// MARK: - POST /v1/nodes/{id}/grants

public struct MacGrantPost: Encodable, Equatable, Sendable {
    public var desk: String
    public var decision: MacGrantDecision
    public var until: Double?

    public init(desk: String, decision: MacGrantDecision, until: Double?) {
        self.desk = desk
        self.decision = decision
        self.until = until
    }

    enum CodingKeys: String, CodingKey { case desk, decision, until }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(desk, forKey: .desk)
        try c.encode(decision, forKey: .decision)
        try c.encode(until, forKey: .until)
    }
}

public struct MacGrantResponse: Decodable, Equatable, Sendable {
    public var ok: Bool
    public var told: Bool
}

/// Every refusal: `{"ok": false, "reason": "<slug>", "detail": "<sentence>"}`.
public struct MacWireRefusal: Decodable, Equatable, Sendable {
    public var reason: String
    public var detail: String

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        reason = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
        detail = try c.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }

    enum CodingKeys: String, CodingKey { case reason, detail }
}

// MARK: - Helpers

public enum MacWire {
    /// Characters (code points, as Python counts them) per stdout/stderr event.
    public static let chunkMax = 65_536
    /// Summary length, as the server's `summary_of`.
    public static let summaryMax = 500

    /// Split `text` into chunks the server accepts; never splits a code point.
    public static func chunks(_ text: String, max: Int = chunkMax) -> [String] {
        guard !text.isEmpty else { return [] }
        let scalars = text.unicodeScalars
        guard scalars.count > max else { return [text] }
        var out: [String] = []
        var i = scalars.startIndex
        while i != scalars.endIndex {
            let j = scalars.index(i, offsetBy: max, limitedBy: scalars.endIndex) ?? scalars.endIndex
            out.append(String(scalars[i..<j]))
            i = j
        }
        return out
    }

    /// Length of the longest prefix of `data` that does not end inside an
    /// incomplete UTF-8 sequence. A partial character stays for the next read.
    public static func completeUTF8Prefix(_ data: Data) -> Int {
        let n = data.count
        guard n > 0 else { return 0 }
        let bytes = [UInt8](data.suffix(3))
        // Walk back over at most 3 continuation bytes to the lead byte.
        var back = 0
        for b in bytes.reversed() {
            back += 1
            if b & 0xC0 == 0x80 { continue }                  // continuation
            let need = b & 0x80 == 0 ? 1 : b & 0xE0 == 0xC0 ? 2 : b & 0xF0 == 0xE0 ? 3 : b & 0xF8 == 0xF0 ? 4 : 1
            return need > back ? n - back : n
        }
        return n   // only continuation bytes: garbage, do not hold it back
    }

    /// The server's `_input_summary`, for the log and the banner.
    static func inputSummary(_ a: MacJobArgs) -> String {
        let at = "\(a.x ?? 0),\(a.y ?? 0)"
        switch a.action ?? "" {
        case "type":
            let t = a.text ?? ""
            let shown = t.count <= 40 ? t : String(t.prefix(39)) + "…"
            return "type '\(shown)' (\(t.count) chars)"
        case "key": return "key \(a.key ?? "")"
        case "click":
            var word = [1: "click", 2: "double-click", 3: "triple-click"][a.count ?? 1] ?? "click"
            if let b = a.button, b != "left" { word = "\(b)-\(word)" }
            return "\(word) \(at)"
        case "drag": return "drag \(at) -> \(a.toX ?? 0),\(a.toY ?? 0)"
        case "scroll": return "scroll \(at) " + (a.dy.map { "dy \($0)" } ?? "dx \(a.dx ?? 0)")
        case let other: return "\(other) \(at)"
        }
    }

    /// One line for the card, the log and the in-use bar — the server's
    /// `summary_of`: whitespace collapsed, at most 500 characters.
    public static func summary(kind: MacJobKind, args: MacJobArgs) -> String {
        let text: String
        switch kind {
        case .run: text = args.command ?? ""
        case .read, .write, .list: text = "\(kind.rawValue) \(args.path ?? "")"
        case .open: text = "open \(args.target ?? "")"
        case .screenshot: text = "screenshot"
        case .input: text = inputSummary(args)
        }
        return String(text.split(whereSeparator: \.isWhitespace).joined(separator: " ").prefix(summaryMax))
    }
}

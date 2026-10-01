import Foundation

/// What a desk is doing right now.
public enum AgentState: String, Codable, Sendable, CaseIterable {
    case needsYou = "NEEDS_YOU"
    case working = "WORKING"
    case done = "DONE"
    case shell = "SHELL"
    case idle = "IDLE"
    case dead = "DEAD"
    /// A roster desk with no process at it and nothing to resume.
    case offline = "OFFLINE"
    /// K3: no live session, but the last one resumes on the next message.
    /// Resting, not broken.
    case asleep = "ASLEEP"

    /// The contract is explicit: treat an unknown state as `IDLE`, so a newer
    /// deck cannot blank out a row.
    public init(wire: String) {
        self = AgentState(rawValue: wire) ?? .idle
    }

    public var label: String {
        switch self {
        case .needsYou: return "Needs you"
        case .working: return "Working"
        case .done: return "Done"
        case .shell: return "Shell"
        case .idle: return "Idle"
        case .dead: return "Dead"
        case .offline: return "Offline"
        case .asleep: return "Asleep"
        }
    }
}

/// **Who spoke last in this desk's conversation** — the field that makes
/// `state` readable.
///
/// `client-api.md` §3.0: `state` is the *session's* disposition, computed from
/// what the process is doing, and it knows nothing about the conversation. So
/// `IDLE` covers three situations a person would never call the same thing:
/// the desk answered and is waiting, he asked and it has not come back, and
/// nobody has ever spoken to it. One word for all three is not something he can
/// manage by exception on.
///
/// Four cases, not three, for the same reason `BlockedUpdate` has three: the
/// deck's `""` is a **real answer** — nobody has ever spoken here — while a row
/// from a deck that never shipped the field has told this client nothing at
/// all. Collapsing them would put a claim on screen that nothing made.
///
/// Read from the thread rather than the session, so it survives a restart: a
/// desk that answered and was then re-seated still reads `.agent` while its
/// `state` drops to `OFFLINE`.
public enum LastSpeaker: Hashable, Sendable {
    /// The desk. It came back to him.
    case agent
    /// Him. The ball is still with the desk.
    case owner
    /// `""` — nobody has ever spoken in this conversation.
    case nobody
    /// The key was absent, or carried a value this client does not know. Say
    /// nothing about it rather than guessing which of the three it was.
    case unknown

    /// An unknown value is `.unknown`, never folded into `.nobody`: a newer
    /// deck must not be able to make this app claim a desk was never spoken to.
    public init(wire: String?) {
        switch wire {
        case "agent": self = .agent
        case "owner": self = .owner
        case "": self = .nobody
        default: self = .unknown
        }
    }

    public var wire: String? {
        switch self {
        case .agent: return "agent"
        case .owner: return "owner"
        case .nobody: return ""
        case .unknown: return nil
        }
    }
}

/// K6: the character a desk was given (`avatar_look` on the wire), overriding
/// the one derived from its name. `color` is an index into the view layer's
/// palette.
public struct AvatarStyle: Hashable, Codable, Sendable {
    public var shape: String
    public var color: Int

    public init(shape: AvatarShape, color: Int) {
        self.shape = shape.rawValue
        self.color = color
    }

    public init(shape: String, color: Int) {
        self.shape = shape
        self.color = color
    }
}

/// K6: the voice a desk speaks in on a call. `id` is an AVSpeech identifier,
/// `""` for "pick one by name".
public struct DeskVoice: Hashable, Codable, Sendable {
    public var id: String
    public var rate: Double

    public init(id: String, rate: Double = 1.0) {
        self.id = id
        self.rate = rate
    }
}

/// A desk on the deck. The name is the identity — every route is
/// `/v1/agents/{name}` and every thread id is keyed on it — so it is not
/// editable and nothing else is allowed to be the key.
public struct Agent: Identifiable, Hashable, Codable, Sendable {
    public var name: String
    /// `label` on the wire: the chip beside the name.
    public var title: String
    /// `charter` on the wire: the long-form description in the settings panel.
    public var detail: String
    /// Client preference. Defaults to "Work" for anything never assigned one.
    public var section: String
    /// Opaque client string; the deck serves no images. `nil` means initials.
    public var avatar: String?
    /// K6's `{"shape","color"}` form of `avatar`; `nil` means derive the face.
    public var avatarStyle: AvatarStyle?
    /// K6: `nil` means pick a voice client-side.
    public var voice: DeskVoice?
    public var state: AgentState
    /// Client preference, for the favourites row.
    public var isPinned: Bool
    /// Client preference, stored by the deck. What it silences, and whether
    /// anything is being silenced at all, is `NotificationDelivery`'s to say
    /// and not this comment's — it said the opposite of the truth for as
    /// long as it took the pushes to ship.
    public var notificationsEnabled: Bool

    // Row state, carried on the same payload so the sidebar costs one request.
    public var threadID: String
    public var unread: Int
    public var lastActivityAt: Date?
    /// `last_activity_by` on the wire. **Not** derivable from `state` — see
    /// `LastSpeaker` and `client-api.md` §3.0.
    public var lastSpeaker: LastSpeaker
    public var preview: String

    // Org chart and session facts. Read-only here by contract.
    public var boss: String?
    public var reports: [String]
    public var project: String
    /// `cwd` on the wire: the folder this desk is configured to work in. For an
    /// agent hired by talking to it, the deck allocated this — it is not
    /// something the owner picked and not something he can change here. `""`
    /// when the payload did not carry one (the sidebar row's shape does not).
    public var workspace: String
    public var sessionID: String?
    public var isDesk: Bool
    /// Why this desk can't move, when the deck knows — either it never
    /// started (`state == .offline`) or it started and then froze on a
    /// permission dialog nothing relayed (`.dialogUnrelayed`, any state).
    /// See `Blocked`'s doc comment and `client-api.md` §3.1: do not gate
    /// showing this on `state`. `nil` covers both "not blocked" and "this
    /// deck hasn't shipped the field yet"; decoding is tolerant of either.
    public var blocked: Blocked?

    public var id: String { name }

    public init(
        name: String,
        title: String,
        detail: String = "",
        section: String = "Work",
        avatar: String? = nil,
        state: AgentState = .idle,
        isPinned: Bool = false,
        notificationsEnabled: Bool = true,
        threadID: String? = nil,
        unread: Int = 0,
        lastActivityAt: Date? = nil,
        lastSpeaker: LastSpeaker = .unknown,
        preview: String = "",
        workspace: String = "",
        boss: String? = nil,
        reports: [String] = [],
        project: String = "",
        sessionID: String? = nil,
        isDesk: Bool = true,
        blocked: Blocked? = nil
    ) {
        self.name = name
        self.title = title
        self.detail = detail
        self.section = section
        self.avatar = avatar
        self.state = state
        self.isPinned = isPinned
        self.notificationsEnabled = notificationsEnabled
        self.threadID = threadID ?? "direct:\(name)"
        self.unread = unread
        self.lastActivityAt = lastActivityAt
        self.lastSpeaker = lastSpeaker
        self.preview = preview
        self.workspace = workspace
        self.boss = boss
        self.reports = reports
        self.project = project
        self.sessionID = sessionID
        self.isDesk = isDesk
        self.blocked = blocked
    }

    /// The deck's names are lowercase on the wire and the contract says
    /// capitalisation is a client decision. Only the first letter of each word
    /// is touched, so "iOS scout" does not become "IOs Scout".
    public var displayName: String { Agent.displayName(forWireName: name) }

    /// The same capitalisation for a name with **no roster row behind it** — a
    /// desk named by a `peer:` id that has since renamed itself, or one this
    /// client has not fetched. §6.2's label has to be a name a person can read
    /// in those cases too; falling back to the raw id is what the relay exists
    /// to stop.
    public static func displayName(forWireName name: String) -> String {
        name.split(separator: " ")
            .map { $0.prefix(1).uppercased() + $0.dropFirst() }
            .joined(separator: " ")
    }

    /// "Working in ~/…", or nothing at all. `nil` rather than an empty string
    /// so a view cannot draw the label with no folder after it — the contract
    /// says show it, not guess it.
    public var workspaceLine: String? {
        guard !workspace.isEmpty else { return nil }
        return "Working in \(shortWorkspace)"
    }

    /// Home-relative, the way the Finder and the deck's own `cwd_short` write
    /// it: an allocated workspace path is otherwise mostly the home folder.
    public var shortWorkspace: String {
        let home = NSHomeDirectory()
        guard workspace == home || workspace.hasPrefix(home + "/") else { return workspace }
        return "~" + workspace.dropFirst(home.count)
    }

    /// The two initials shown when there is no avatar: "travel scout" -> "TS".
    public var initials: String {
        let words = name.split(separator: " ").prefix(2)
        let letters = words.compactMap { $0.first.map(String.init) }
        return letters.isEmpty ? "?" : letters.joined().uppercased()
    }

    /// A stable tint derived from the name, so a face keeps its colour between
    /// launches without the deck having to store one.
    public var avatarTintIndex: Int {
        abs(name.unicodeScalars.reduce(0) { ($0 &* 31 &+ Int($1.value)) % 100_003 })
    }

    /// The avatar string is opaque and the deck serves no images, so it is only
    /// drawable when it names something on this machine.
    public var localAvatarURL: URL? {
        guard let avatar, avatar.contains("/") else { return nil }
        let expanded = (avatar as NSString).expandingTildeInPath
        guard FileManager.default.fileExists(atPath: expanded) else { return nil }
        return URL(fileURLWithPath: expanded)
    }

    enum CodingKeys: String, CodingKey {
        case name, section, avatar, state, unread, preview, pinned, notifications
        case boss, reports, project, cwd, blocked, voice
        case label, charter, mission
        case threadID = "thread_id"
        case lastActivityAt = "last_activity_at"
        case lastSpeaker = "last_activity_by"
        case avatarLook = "avatar_look"
        case sessionID = "session_id"
        case desk
        case isDeskAlt = "is_desk"
    }

    /// Written by hand: the settings payload and the sidebar row are the same
    /// agent with different fields present, and either may omit the other's.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decode(String.self, forKey: .name)
        title = try container.decodeIfPresent(String.self, forKey: .label) ?? ""
        detail = try container.decodeIfPresent(String.self, forKey: .charter)
            ?? container.decodeIfPresent(String.self, forKey: .mission)
            ?? ""
        section = try container.decodeIfPresent(String.self, forKey: .section) ?? "Work"
        // K6 as shipped: the drawn character is `avatar_look`; `avatar` stays
        // the opaque string (often null) and never clears the character. An
        // object under `avatar` is read too, for a deck that echoes the PATCH.
        if let text = try? container.decodeIfPresent(String.self, forKey: .avatar) {
            avatar = text
        } else {
            avatar = nil
        }
        avatarStyle = (try? container.decodeIfPresent(AvatarStyle.self, forKey: .avatarLook))
            ?? (try? container.decodeIfPresent(AvatarStyle.self, forKey: .avatar))
        voice = try? container.decodeIfPresent(DeskVoice.self, forKey: .voice)
        state = AgentState(wire: try container.decodeIfPresent(String.self, forKey: .state) ?? "IDLE")
        isPinned = try container.decodeIfPresent(Bool.self, forKey: .pinned) ?? false
        notificationsEnabled = try container.decodeIfPresent(Bool.self, forKey: .notifications) ?? true
        threadID = try container.decodeIfPresent(String.self, forKey: .threadID) ?? "direct:\(name)"
        unread = try container.decodeIfPresent(Int.self, forKey: .unread) ?? 0
        // Epoch seconds as a float; `null` means "no time", never zero.
        lastActivityAt = try container.decodeIfPresent(Double.self, forKey: .lastActivityAt)
            .map(Date.init(timeIntervalSince1970:))
        // Absent is its own answer (`.unknown`), and deliberately not the same
        // fact as the deck's `""`. See `LastSpeaker`.
        lastSpeaker = LastSpeaker(
            wire: try container.decodeIfPresent(String.self, forKey: .lastSpeaker))
        preview = try container.decodeIfPresent(String.self, forKey: .preview) ?? ""
        workspace = try container.decodeIfPresent(String.self, forKey: .cwd) ?? ""
        boss = try container.decodeIfPresent(String.self, forKey: .boss)
        reports = try container.decodeIfPresent([String].self, forKey: .reports) ?? []
        project = try container.decodeIfPresent(String.self, forKey: .project) ?? ""
        sessionID = try container.decodeIfPresent(String.self, forKey: .sessionID)
        isDesk = try container.decodeIfPresent(Bool.self, forKey: .desk)
            ?? container.decodeIfPresent(Bool.self, forKey: .isDeskAlt)
            ?? true
        // Additive: a deck that has not shipped this yet, and `blocked: null`
        // on one that has, both decode to `nil` here.
        blocked = try container.decodeIfPresent(Blocked.self, forKey: .blocked)
    }

    /// Only used for local caching and previews. The wire body for a write is
    /// `AgentPatch`, deliberately — see below.
    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(name, forKey: .name)
        try container.encode(title, forKey: .label)
        try container.encode(detail, forKey: .charter)
        try container.encode(section, forKey: .section)
        try container.encodeIfPresent(avatar, forKey: .avatar)
        try container.encode(state.rawValue, forKey: .state)
        try container.encode(isPinned, forKey: .pinned)
        try container.encode(notificationsEnabled, forKey: .notifications)
        try container.encode(threadID, forKey: .threadID)
        try container.encode(unread, forKey: .unread)
        try container.encodeIfPresent(lastActivityAt?.timeIntervalSince1970, forKey: .lastActivityAt)
        try container.encodeIfPresent(lastSpeaker.wire, forKey: .lastSpeaker)
        try container.encode(preview, forKey: .preview)
        try container.encode(workspace, forKey: .cwd)
    }
}

/// The body of a `PATCH /v1/agents/{name}`.
///
/// It carries only the keys the settings panel owns. `name` is the identity and
/// is not editable; `reports_to`/`boss` is the org chart and sending it **at
/// all** — even unchanged — is a 409 by contract, so it is not representable.
public struct AgentPatch: Encodable, Sendable {
    public var label: String
    public var charter: String
    public var section: String
    public var notifications: Bool
    public var pinned: Bool
    public var avatar: String?

    public init(_ agent: Agent) {
        self.label = agent.title
        self.charter = agent.detail
        self.section = agent.section
        self.notifications = agent.notificationsEnabled
        self.pinned = agent.isPinned
        self.avatar = agent.avatar
    }

    enum CodingKeys: String, CodingKey {
        case label, charter, section, notifications, pinned, avatar
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(label, forKey: .label)
        try container.encode(charter, forKey: .charter)
        try container.encode(section, forKey: .section)
        try container.encode(notifications, forKey: .notifications)
        try container.encode(pinned, forKey: .pinned)
        // Explicit null is meaningful: it clears the avatar.
        try container.encode(avatar, forKey: .avatar)
    }

    public func encoded() throws -> Data {
        try DeckCoding.encoder.encode(self)
    }
}


/// The 201 body of `POST /v1/agents`: the settings payload of the new desk.
public struct CreatedAgentResponse: Decodable, Sendable {
    public var agent: Agent

    enum CodingKeys: String, CodingKey { case agent }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        agent = try container.decode(Agent.self, forKey: .agent)
    }
}

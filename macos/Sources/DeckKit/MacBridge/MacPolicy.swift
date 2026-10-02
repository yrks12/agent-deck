import Foundation

/// "Let agents use this Mac": `Off` · `Ask me` (default) · `Full access`, plus
/// `paused` from the menu bar.
public enum MacMode: String, Codable, Sendable, CaseIterable {
    case off, ask, full, paused
}

public enum MacJobKind: String, Codable, Sendable, CaseIterable {
    case run, read, write, list, open, screenshot, input
}

/// One desk's standing on this Mac.
public struct MacGrant: Codable, Equatable, Sendable {
    public enum State: String, Codable, Sendable { case hour, always, denied }
    public var state: State
    /// Epoch seconds; `nil` for `always`.
    public var until: Double?
    /// Fallback only (Seatbelt unavailable): the desk may run commands that
    /// are not confined. Never set while Seatbelt works.
    public var unconfined: Bool?

    public init(state: State, until: Double?, unconfined: Bool? = nil) {
        self.state = state
        self.until = until
        self.unconfined = unconfined
    }

    func live(at now: Double) -> Bool { until.map { now < $0 } ?? true }
}

/// What he tapped on the grant card (or Revoke in Settings).
public enum MacGrantDecision: String, Codable, Sendable {
    case hour, always, deny, revoke
}

/// `~/Library/Application Support/Agent Deck/mac-policy.json`.
public struct MacPolicyConfig: Codable, Equatable, Sendable {
    public var mode: MacMode
    public var folders: [String]
    public var neverTouch: [String]
    public var exceptions: [String]
    public var openEnabled: Bool
    public var screenshotEnabled: Bool
    public var grants: [String: MacGrant]

    enum CodingKeys: String, CodingKey {
        case mode, folders, exceptions, grants
        case neverTouch = "never_touch"
        case openEnabled = "open_enabled"
        case screenshotEnabled = "screenshot_enabled"
    }

    public init(mode: MacMode = .ask, folders: [String] = [], neverTouch: [String] = MacPolicyConfig.defaultNeverTouch,
                exceptions: [String] = [], openEnabled: Bool = true, screenshotEnabled: Bool = false,
                grants: [String: MacGrant] = [:]) {
        self.mode = mode
        self.folders = folders
        self.neverTouch = neverTouch
        self.exceptions = exceptions
        self.openEnabled = openEnabled
        self.screenshotEnabled = screenshotEnabled
        self.grants = grants
    }

    /// A stranger's first launch: Ask me, no folders, no grants.
    public static let defaults = MacPolicyConfig()

    /// The editable "Never touch" list ("Reset to defaults" restores it).
    public static let defaultNeverTouch: [String] = [
        "~/.ssh/**", "~/.gnupg/**", "~/Library/Keychains/**", "/Library/Keychains/**",
        "~/.aws/**", "~/.config/gcloud/**", "~/.kube/**", "~/.docker/config.json",
        "~/.netrc", "~/.npmrc", "~/.pypirc",
        "~/Library/Application Support/{Google/Chrome,Firefox,BraveSoftware,Microsoft Edge,Arc}/**",
        "~/Library/Safari/**", "~/Library/Cookies/**", "~/Library/Mail/**", "~/Library/Messages/**",
        "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
    ]

    /// Not in the editable list and not beaten by an exception in Ask mode: a
    /// desk must not be able to edit its own grant or the log of what it did.
    public static let protectedInAsk: [String] = [
        "~/Library/Application Support/Agent Deck/**",
        "~/Library/Preferences/\(DeckIdentity.bundleID).plist",
    ]
}

/// The one thing the bridge asks the policy: this job, now — what happens?
public struct MacJobRequest: Equatable, Sendable {
    public var id: String
    public var desk: String
    public var kind: MacJobKind
    /// `read`/`write`/`list`: the path as the desk sent it.
    public var path: String?
    /// `run`: working directory as sent (absolute or `~`), or nil.
    public var cwd: String?
    /// One line for the card and the log (`run sw_vers`, `read ~/x`).
    public var summary: String

    public init(id: String, desk: String, kind: MacJobKind, path: String? = nil, cwd: String? = nil, summary: String = "") {
        self.id = id
        self.desk = desk
        self.kind = kind
        self.path = path
        self.cwd = cwd
        self.summary = summary
    }
}

/// What the grant card shows.
public struct MacGrantRequest: Equatable, Sendable {
    public var jobId: String
    public var desk: String
    public var summary: String
    /// The folders a grant would let it change (for "It can … change files in:").
    public var folders: [String]
    /// Fallback only: this grant lets it run commands that are not confined.
    public var unconfined: Bool
}

public enum MacDecision: Equatable, Sendable {
    /// Go. `path` is the resolved target for file ops, `cwd` the resolved
    /// working directory for `run`; `profile` is nil when unconfined.
    case run(profile: MacSeatbeltProfile?, path: String?, cwd: String?)
    case ask(MacGrantRequest)
    case refuse(reason: String, detail: String)
}

/// **The Mac decides, not the server.** Pure: config + job + clock in,
/// decision out. The server cannot grant; a compromised server can only use
/// grants that already exist (security review).
public struct MacPolicy: Sendable {
    public var config: MacPolicyConfig
    public var paths: MacPaths
    /// Measured at start-up (`MacSeatbelt.isAvailable`). When false the plan's
    /// decided fallback applies: Ask-mode `run` needs an explicit per-desk
    /// "Allow commands (not confined)" grant; file tools stay scoped.
    public var seatbeltAvailable: Bool
    public var tempDirs: [String]
    /// "Let agents control this Mac" — in memory only, never in `config`.
    public var control: MacControlGrant?

    public static let hour: Double = 3600
    public static let denyHold: Double = 86_400

    public init(config: MacPolicyConfig, paths: MacPaths = MacPaths(), seatbeltAvailable: Bool = MacSeatbelt.isAvailable,
                tempDirs: [String] = MacSeatbelt.tempDirectories()) {
        self.config = config
        self.paths = paths
        self.seatbeltAvailable = seatbeltAvailable
        self.tempDirs = tempDirs
    }

    /// Chosen folders, resolved; unresolvable entries dropped.
    public var resolvedFolders: [String] { config.folders.compactMap { paths.resolve($0) } }

    /// The desk's live grant, or nil (none / expired).
    public func grant(for desk: String, at now: Double) -> MacGrant? {
        guard let g = config.grants[desk], g.live(at: now) else { return nil }
        return g
    }

    public func decide(_ job: MacJobRequest, now: Double) -> MacDecision {
        switch config.mode {
        case .off: return .refuse(reason: "mac_offline", detail: "Mac access is turned off on this Mac.")
        case .paused: return .refuse(reason: "mac_paused", detail: "Mac access is paused on this Mac. Do not retry until you are told it is back.")
        case .ask, .full: break
        }
        if job.kind == .open && !config.openEnabled {
            return .refuse(reason: "capability_off", detail: "Opening apps and links is turned off in Shaliach → Settings → Mac.")
        }
        // His own hands (the phone's live view) never need an agent's grant:
        // the grant is what lets AGENTS in (owner, 2026-10-01 21:00). But a
        // leaked deck token alone must not drive this Mac, so they need Full
        // access, decided here on the Mac and not trusted to the server.
        if job.kind == .input && job.desk == MacControlGrant.ownerDesk {
            return config.mode == .full
                ? .run(profile: nil, path: nil, cwd: nil)
                : .refuse(reason: "full_access_required", detail: MacControlCopy.fullAccessForPhone)
        }
        let controlled = control?.covers(job.desk, now: now) == true
        if job.kind == .input {
            return controlled ? .run(profile: nil, path: nil, cwd: nil)
                : .refuse(reason: "control_off", detail: MacControlCopy.offDetail)
        }
        // Full access can already run `screencapture`; the switch guards Ask me.
        if job.kind == .screenshot && !config.screenshotEnabled && !controlled && config.mode != .full {
            return .refuse(reason: "capability_off", detail: "Screenshots are turned off in Shaliach → Settings → Mac.")
        }
        // Under his control grant a desk may look: it cannot click what it cannot see.
        if job.kind == .screenshot && controlled { return .run(profile: nil, path: nil, cwd: nil) }

        let full = config.mode == .full
        var target: String?
        var cwd: String?
        switch job.kind {
        case .read, .write, .list:
            guard let raw = job.path, let resolved = paths.resolve(raw) else {
                return .refuse(reason: "bad_path", detail: "Give an absolute path or one starting with ~/.")
            }
            target = resolved
        case .run:
            if let raw = job.cwd {
                guard let resolved = paths.resolve(raw) else {
                    return .refuse(reason: "bad_path", detail: "cwd must be absolute or start with ~/.")
                }
                cwd = resolved
            }
        case .open, .screenshot, .input:
            break
        }

        // Full access: anywhere the user can reach, never asks, unconfined.
        if full { return .run(profile: nil, path: target, cwd: cwd) }

        for p in [target, cwd].compactMap({ $0 }) {
            if let refusal = blocked(p) { return refusal }
        }
        let folders = resolvedFolders
        if let p = target, !folders.contains(where: { MacPaths.isInside(p, folder: $0) }) {
            return .refuse(reason: "out_of_scope",
                           detail: "\(p) is outside the folders agents may use on this Mac. Ask the owner to add it in Shaliach → Settings → Mac.")
        }
        if let c = cwd, !(folders + tempDirs).contains(where: { MacPaths.isInside(c, folder: $0) }) {
            return .refuse(reason: "out_of_scope",
                           detail: "\(c) is outside the folders agents may use on this Mac. Ask the owner to add it in Shaliach → Settings → Mac.")
        }

        let needsUnconfined = job.kind == .run && !seatbeltAvailable
        if let g = config.grants[job.desk], g.live(at: now) {
            if g.state == .denied {
                return .refuse(reason: "denied", detail: "The owner said no to you using his Mac. Do not ask again today; finish without it.")
            }
            if !needsUnconfined || g.unconfined == true {
                let profile = job.kind == .run && seatbeltAvailable ? seatbeltProfile(folders: folders) : nil
                return .run(profile: profile, path: target, cwd: cwd ?? folders.first)
            }
        }
        return .ask(MacGrantRequest(jobId: job.id, desk: job.desk, summary: String(job.summary.prefix(500)),
                                    folders: folders, unconfined: needsUnconfined))
    }

    /// "Never touch" (plus the non-removable app dirs), unless an exception
    /// names it. The app's own dirs cannot be excepted.
    func blocked(_ path: String) -> MacDecision? {
        let detail = "\(path) is on this Mac's \"Never touch\" list. Do not try another way to reach it."
        if MacPolicyConfig.protectedInAsk.contains(where: { paths.matches($0, path) }) {
            return .refuse(reason: "blocked_path", detail: detail)
        }
        guard config.neverTouch.contains(where: { paths.matches($0, path) }) else { return nil }
        if config.exceptions.contains(where: { paths.matches($0, path) }) { return nil }
        return .refuse(reason: "blocked_path", detail: detail)
    }

    public func seatbeltProfile(folders: [String]) -> MacSeatbeltProfile {
        MacSeatbelt.profile(paths: paths, folders: folders,
                            neverTouch: config.neverTouch + MacPolicyConfig.protectedInAsk,
                            exceptions: config.exceptions, tempDirs: tempDirs)
    }

    /// Apply what he tapped. Hour = +3600 s, deny holds 24 h, always has no end.
    public mutating func apply(_ decision: MacGrantDecision, desk: String, now: Double, unconfined: Bool = false) {
        switch decision {
        case .hour: config.grants[desk] = MacGrant(state: .hour, until: now + Self.hour, unconfined: unconfined ? true : nil)
        case .always: config.grants[desk] = MacGrant(state: .always, until: nil, unconfined: unconfined ? true : nil)
        case .deny: config.grants[desk] = MacGrant(state: .denied, until: now + Self.denyHold)
        case .revoke: config.grants[desk] = nil
        }
    }
}

/// Loads and saves `mac-policy.json`. A missing or unreadable file is the safe
/// default (Ask me, nothing granted) — never Full access.
public struct MacPolicyStore: Sendable {
    public let url: URL

    public init(url: URL) { self.url = url }

    public init(paths: MacPaths = MacPaths()) {
        self.url = URL(fileURLWithPath: paths.supportDirectory + "/mac-policy.json")
    }

    public func load() -> MacPolicyConfig {
        guard let data = try? Data(contentsOf: url),
              let config = try? JSONDecoder().decode(MacPolicyConfig.self, from: data) else { return .defaults }
        return config
    }

    public func save(_ config: MacPolicyConfig) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        try encoder.encode(config).write(to: url, options: .atomic)
    }
}

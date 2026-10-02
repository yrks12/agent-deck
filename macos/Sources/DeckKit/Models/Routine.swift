import Foundation

/// A standing instruction: the same prompt, sent to one agent, on a schedule.
///
/// `nextRunAt` is **read-only here**. The deck owns the cron maths — it is the
/// side that survives a restart, a DST change and a Mac that was asleep — so
/// this client states a spec and a timezone and reads back whatever the deck
/// worked out.
public struct Routine: Identifiable, Hashable, Sendable, Decodable {
    public let id: String
    public let agentName: String
    public let prompt: String
    /// Five-field cron, exactly as the deck stores it.
    public let spec: String
    /// IANA identifier — "Europe/London". A schedule without one is ambiguous
    /// twice a year.
    public let timezone: String
    public let nextRunAt: Date?
    public let isEnabled: Bool
    /// The last three attempts, newest first. Enough to answer "is this thing
    /// working" — not an audit trail.
    public let runs: [RoutineRun]

    enum CodingKeys: String, CodingKey {
        case id, agent, prompt, trigger, enabled, runs
        case nextRunAt = "next_run_at"
    }

    struct Trigger: Decodable {
        let spec: String
        let tz: String
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        agentName = try container.decode(String.self, forKey: .agent)
        prompt = try container.decodeIfPresent(String.self, forKey: .prompt) ?? ""
        let trigger = try container.decode(Trigger.self, forKey: .trigger)
        spec = trigger.spec
        timezone = trigger.tz
        nextRunAt = try container.decodeIfPresent(Double.self, forKey: .nextRunAt)
            .map(Date.init(timeIntervalSince1970:))
        isEnabled = try container.decodeIfPresent(Bool.self, forKey: .enabled) ?? true
        runs = try container.decodeIfPresent([RoutineRun].self, forKey: .runs) ?? []
    }

    public var scheduleText: String { CronSchedule.describe(spec) }

    /// How the last attempt went. A routine that has been failing every
    /// morning must not read the same as one that has been working — and
    /// "queued" is waiting for the agent's next turn, not a failure.
    public var lastRunText: String {
        guard let last = runs.first else { return "Has not run yet" }
        let detail = last.detail.trimmingCharacters(in: .whitespacesAndNewlines)
        if last.ok {
            return detail.isEmpty ? "Last run went through" : "Last run \(detail)"
        }
        return detail.isEmpty ? "Last run failed" : "Last run failed — \(detail)"
    }

    /// Three different facts, three different sentences. A paused routine must
    /// never show the time it *would* have run — that reads as scheduled.
    public var nextRunText: String {
        guard isEnabled else { return "Paused" }
        guard let nextRunAt else { return "The deck has not worked out when this runs next" }
        return "Next run \(nextRunAt.formatted(date: .abbreviated, time: .shortened))"
    }
}

/// One attempt at running a routine.
public struct RoutineRun: Hashable, Sendable, Decodable {
    public let at: Date?
    public let ok: Bool
    public let detail: String

    enum CodingKeys: String, CodingKey { case ts, ok, detail }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        at = try container.decodeIfPresent(Double.self, forKey: .ts)
            .map(Date.init(timeIntervalSince1970:))
        ok = try container.decodeIfPresent(Bool.self, forKey: .ok) ?? false
        detail = try container.decodeIfPresent(String.self, forKey: .detail) ?? ""
    }
}

public struct RoutinesResponse: Decodable, Sendable {
    public var routines: [Routine]

    enum CodingKeys: String, CodingKey { case routines }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        routines = try container.decodeIfPresent([Routine].self, forKey: .routines) ?? []
    }
}

public struct RoutineResponse: Decodable, Sendable {
    public var routine: Routine

    enum CodingKeys: String, CodingKey { case routine }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        routine = try container.decode(Routine.self, forKey: .routine)
    }
}

/// The "+" in the routines panel.
public struct RoutineDraft: Equatable, Sendable {
    public var agentName: String
    public var prompt: String
    public var spec: String
    /// This Mac's zone, not a constant: a routine written in London and read
    /// in Lisbon must still mean 8am where it was written.
    public var timezone: String
    public var isEnabled: Bool

    public init(
        agentName: String,
        prompt: String = "",
        spec: String = "0 8 * * 1-5",
        timezone: String = TimeZone.current.identifier,
        isEnabled: Bool = true
    ) {
        self.agentName = agentName
        self.prompt = prompt
        self.spec = spec
        self.timezone = timezone
        self.isEnabled = isEnabled
    }

    public var problem: String? {
        if prompt.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "Say what this routine should ask the agent to do."
        }
        if spec.split(separator: " ").count != 5 {
            return "A schedule is five cron fields: minute, hour, day of month, month, day of week."
        }
        if timezone.isEmpty {
            return "A schedule needs a timezone, or it means a different hour twice a year."
        }
        return nil
    }

    public var isReady: Bool { problem == nil }

    /// `next_run_at` is deliberately absent. The deck computes it; a client
    /// that sent one would be overwriting the only copy that survives a
    /// restart.
    public func encodedBody() throws -> Data {
        try JSONSerialization.data(withJSONObject: [
            "agent": agentName,
            "prompt": prompt,
            // `kind` is stated: §13 stores any other kind and never schedules
            // it, so leaving it to a default is a routine that silently never
            // fires.
            "trigger": ["kind": "cron", "spec": spec, "tz": timezone],
            "enabled": isEnabled,
        ])
    }

    public var scheduleText: String { CronSchedule.describe(spec) }
}

/// Turns five cron fields into the sentence the panel shows.
///
/// It phrases only the shapes it is sure of. Anything else is shown as the
/// spec itself — a wrong sentence about when something fires is worse than a
/// spec the reader has to decode.
public enum CronSchedule {

    public static func describe(_ spec: String) -> String {
        let fields = spec.split(separator: " ").map(String.init)
        guard fields.count == 5 else { return custom(spec) }
        let (minute, hour, dayOfMonth, month, dayOfWeek) =
            (fields[0], fields[1], fields[2], fields[3], fields[4])
        // Anything keyed on a date is outside what this phrases.
        guard dayOfMonth == "*", month == "*" else { return custom(spec) }

        if let step = everyStep(minute), hour == "*", dayOfWeek == "*" {
            return "Every \(step) minutes"
        }
        if let step = everyStep(hour), minute == "0" {
            switch days(dayOfWeek) {
            case .everyDay: return "Every \(step) hours"
            case .weekdays: return "Every \(step) hours on weekdays"
            default: return custom(spec)
            }
        }
        guard let minuteValue = Int(minute), let hourValue = Int(hour),
              (0...59).contains(minuteValue), (0...23).contains(hourValue)
        else { return custom(spec) }

        let time = clock(hour: hourValue, minute: minuteValue)
        switch days(dayOfWeek) {
        case .everyDay: return "Every day at \(time)"
        case .weekdays: return "Weekdays at \(time)"
        case .weekends: return "Weekends at \(time)"
        case .single(let name): return "\(name)s at \(time)"
        case .unknown: return custom(spec)
        }
    }

    private static func custom(_ spec: String) -> String { "Custom schedule (\(spec))" }

    private enum Days: Equatable {
        case everyDay, weekdays, weekends, single(String), unknown
    }

    private static let names = [
        "Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday",
    ]

    private static func days(_ field: String) -> Days {
        switch field {
        case "*": return .everyDay
        case "1-5": return .weekdays
        case "0,6", "6,0", "0,6,", "6-7": return .weekends
        default:
            if let value = Int(field), names.indices.contains(value % 7) {
                return .single(names[value % 7])
            }
            return .unknown
        }
    }

    /// "*/3" -> 3.
    private static func everyStep(_ field: String) -> Int? {
        guard field.hasPrefix("*/"), let step = Int(field.dropFirst(2)), step > 1 else {
            return nil
        }
        return step
    }

    /// Written out here rather than through a formatter: the panel's schedule
    /// line is a fixed phrase and a locale-dependent one would make the string
    /// untestable and inconsistent with the words around it.
    private static func clock(hour: Int, minute: Int) -> String {
        let suffix = hour < 12 ? "AM" : "PM"
        let shown = hour % 12 == 0 ? 12 : hour % 12
        return String(format: "%d:%02d %@", shown, minute, suffix)
    }
}

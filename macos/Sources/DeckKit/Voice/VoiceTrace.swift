import Foundation
import os

/// **Every step of a voice turn, readable from Console.app.**
///
/// The first voice build logged nothing he could read: os_log redacts dynamic
/// strings as `<private>` unless they are marked public, so "the call did not
/// work" left no trace at all. Every line here is `privacy: .public` and names
/// the step, never what he said (his words stay out of the system log).
///
/// Read it live with:
///
///     log stream --predicate 'subsystem == "<bundle id>" AND category == "voice"'
///
/// The last lines are also kept in memory so a test can assert that a failure
/// left a trace, not just that it happened.
public enum VoiceTrace {
    public static let subsystem = DeckIdentity.bundleID
    public static let category = "voice"
    private static let logger = Logger(subsystem: subsystem, category: category)

    private static let lock = NSLock()
    nonisolated(unsafe) private static var buffer: [String] = []
    private static let keep = 200

    /// One step. `event` is a short fixed name (`call.start`), `detail` the
    /// facts that decide it (a state, a status, an error) — never his words.
    public static func note(_ event: String, _ detail: String = "") {
        let line = detail.isEmpty ? event : "\(event) \(detail)"
        logger.notice("\(line, privacy: .public)")
        lock.lock()
        buffer.append(line)
        if buffer.count > keep { buffer.removeFirst(buffer.count - keep) }
        lock.unlock()
    }

    /// Something went wrong: the same line at error level.
    public static func fail(_ event: String, _ detail: String) {
        let line = "\(event) \(detail)"
        logger.error("\(line, privacy: .public)")
        lock.lock()
        buffer.append(line)
        if buffer.count > keep { buffer.removeFirst(buffer.count - keep) }
        lock.unlock()
    }

    /// The most recent lines, oldest first.
    public static var recent: [String] {
        lock.lock(); defer { lock.unlock() }
        return buffer
    }

    /// Tests only: start from nothing.
    static func reset() {
        lock.lock(); buffer.removeAll(); lock.unlock()
    }
}

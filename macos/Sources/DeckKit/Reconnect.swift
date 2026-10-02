import Foundation

// MARK: - never hang on "Finding your deck"
//
// A phone's first request can stall: the VPN tunnel waking, a socket left
// from before a deck restart (deploys restart it often). Unbounded, that was
// 60 seconds of "Checking in with your desks" with no retry after. These are
// the rules that bound it; the app only calls them.

/// **How long to wait before trying the deck again.** Fast at first (a
/// restart is usually back in seconds), doubling, capped low so a deck that
/// comes back is found within ten seconds of it answering.
public struct ReconnectBackoff: Equatable, Sendable {
    public static let first: TimeInterval = 0.5
    public static let cap: TimeInterval = 10

    public private(set) var attempt = 0

    public init() {}

    /// The wait before the next try, counting this failure.
    public mutating func next() -> TimeInterval {
        let wait = min(Self.cap, Self.first * pow(2, Double(attempt)))
        attempt += 1
        return wait
    }

    public mutating func reset() { attempt = 0 }
}

public enum FirstLoad {
    /// Runs `load` until it succeeds: each try cut off after `timeout`, each
    /// failure reported and followed by the backoff's wait. Returns false only
    /// when the calling task is cancelled.
    public static func untilLoaded(
        timeout: TimeInterval,
        backoff: ReconnectBackoff,
        sleep: @escaping @Sendable (TimeInterval) async -> Void,
        onFailure: @escaping @Sendable (Error) async -> Void,
        load: @escaping @Sendable () async throws -> Void
    ) async -> Bool {
        var backoff = backoff
        while !Task.isCancelled {
            do {
                try await withTimeout(timeout, load)
                return true
            } catch {
                if Task.isCancelled { return false }
                await onFailure(error)
            }
            await sleep(backoff.next())
        }
        return false
    }

    /// Thrown when one try took longer than its timeout.
    public struct TimedOut: Error, Equatable {
        public let after: TimeInterval
    }

    public static func withTimeout(_ seconds: TimeInterval,
                            _ work: @escaping @Sendable () async throws -> Void) async throws {
        try await withThrowingTaskGroup(of: Void.self) { group in
            group.addTask { try await work() }
            group.addTask {
                try await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                throw TimedOut(after: seconds)
            }
            defer { group.cancelAll() }
            try await group.next()
        }
    }
}

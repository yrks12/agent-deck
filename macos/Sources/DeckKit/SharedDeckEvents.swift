import Foundation

/// **One `/v1/stream` per app, however many things are listening.**
///
/// The deck fans every event down one stream. The phone used to open one for
/// the roster and another for every thread he opened (and one for a call's
/// thread) — measured on the box as a new `/v1/stream` with every thread open.
/// This holds one upstream and hands each listener its own copy.
///
/// - The first listener opens the connection; the last one to leave closes it.
/// - When the connection drops, every listener's stream ends with that error,
///   so each resynchronises (a thread refetches since its cursor) and listens
///   again — and the first to come back reopens the one shared connection.
/// - A listener that joins late does not get the `hello` that opened it.
public actor SharedDeckEvents {
    private let open: @Sendable () -> AsyncThrowingStream<DeckEvent, Error>
    private var listeners: [UUID: AsyncThrowingStream<DeckEvent, Error>.Continuation] = [:]
    private var pump: Task<Void, Never>?
    private var generation = 0
    /// Connections opened over this hub's life, for tests and diagnostics.
    public private(set) var connectionsOpened = 0

    public init(open: @escaping @Sendable () -> AsyncThrowingStream<DeckEvent, Error>) {
        self.open = open
    }

    public nonisolated func events() -> AsyncThrowingStream<DeckEvent, Error> {
        AsyncThrowingStream { continuation in
            let key = UUID()
            Task { await self.add(key, continuation) }
            continuation.onTermination = { _ in
                Task { await self.remove(key) }
            }
        }
    }

    private func add(_ key: UUID, _ continuation: AsyncThrowingStream<DeckEvent, Error>.Continuation) {
        listeners[key] = continuation
        if pump == nil { start() }
    }

    private func remove(_ key: UUID) {
        guard listeners.removeValue(forKey: key) != nil else { return }
        if listeners.isEmpty {
            pump?.cancel()
            pump = nil
        }
    }

    /// Thrown to every listener when the connection is replaced on purpose.
    public struct Replaced: Error, Equatable { public init() {} }

    /// Close the connection and end every listener, so each resyncs and the
    /// first back opens a fresh one: Retry, or the app back from the
    /// background with a socket that may have died while it slept.
    public func reconnect() {
        guard pump != nil else { return }
        pump?.cancel()
        end(generation, error: Replaced())
    }

    private func start() {
        connectionsOpened += 1
        Diagnostics.count("http.sharedStreamOpened")
        generation += 1
        let mine = generation
        let upstream = open()
        pump = Task { [weak self] in
            do {
                for try await event in upstream {
                    await self?.broadcast(event)
                }
                await self?.end(mine, error: nil)
            } catch {
                await self?.end(mine, error: error)
            }
        }
    }

    private func broadcast(_ event: DeckEvent) {
        for continuation in listeners.values { continuation.yield(event) }
    }

    private func end(_ which: Int, error: Error?) {
        // A pump cancelled because everyone left has nothing to tell anyone.
        guard which == generation, pump != nil else { return }
        let ending = listeners
        listeners = [:]
        pump = nil
        for continuation in ending.values {
            if let error { continuation.finish(throwing: error) } else { continuation.finish() }
        }
    }
}

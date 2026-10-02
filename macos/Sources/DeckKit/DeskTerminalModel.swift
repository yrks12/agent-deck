import Foundation

/// Where the terminal is, in words a view can draw.
public enum TerminalStreamState: Equatable, Sendable {
    case connecting
    /// His shell, keys go to it.
    case live
    /// A mirror: drawn, never typed into.
    case readOnly
    /// The shell exited. Nothing reconnects by itself: a new session is his call.
    case ended(code: Int)
    case failed(TerminalStreamFailure)
}

/// The emulator, as the model sees it. SwiftTerm's view is one; a test
/// records. Bytes count as processed once `terminalFeed` has returned.
@MainActor
public protocol TerminalOutputSink: AnyObject {
    func terminalFeed(_ bytes: Data)
    /// A new session is starting: clear the screen it will draw over.
    func terminalReset()
}

/// **The live terminal's model. It does not touch `DeckStore`.**
///
/// Same rule as `AgentComputerModel`: it publishes only to the view that owns
/// it. It owns one socket at a time and:
/// * hands output to the emulator, buffering until one is attached;
/// * acks what the emulator processed — every `TerminalStreamWire.ackEvery`
///   bytes and at any idle moment — so the deck's backpressure is honest;
/// * sends input, resizes and acks in one ordered queue (a keystroke must not
///   overtake the one before it);
/// * reconnects after a drop with `ReconnectBackoff`, and stops on an exit or a
///   refusal, saying which;
/// * switches window by reconnecting with the other `window=`.
@MainActor
public final class DeskTerminalModel: ObservableObject {
    public let desk: String

    @Published public private(set) var state: TerminalStreamState = .connecting
    @Published public private(set) var window: TerminalWindow
    @Published public private(set) var windows: [TerminalWindow] = TerminalWindow.allCases
    /// Between pressing the other window and its hello.
    @Published public private(set) var isSwitching = false

    /// The stream is refused in a way the one-command drawer still answers.
    public var fallsBackToOneShot: Bool {
        if case .failed(let failure) = state { return failure.fallsBackToOneShot }
        return false
    }

    public var acceptsInput: Bool { state == .live }

    // Ack accounting, per socket.
    public private(set) var processedBytes = 0
    public private(set) var ackedBytes = 0
    public var pendingBytes: Int { pending.reduce(0) { $0 + $1.count } }

    private let client: DeskTerminalStreaming
    private let sleep: @Sendable (TimeInterval) async -> Void
    private let idleAckDelay: TimeInterval?
    private var backoff = ReconnectBackoff()

    private var cols: Int
    private var rows: Int
    private weak var sink: TerminalOutputSink?
    private var pending: [Data] = []

    private var runTask: Task<Void, Never>?
    private var generation = 0
    private var outbox: AsyncStream<Outgoing>.Continuation?
    private var idleAckTask: Task<Void, Never>?

    private enum Outgoing { case input(Data), text(String) }
    private enum Ending { case exit(Int), failed(TerminalStreamFailure), dropped, stopped }

    public init(desk: String, client: DeskTerminalStreaming,
                window: TerminalWindow = .deck, cols: Int = 80, rows: Int = 24,
                sleep: @escaping @Sendable (TimeInterval) async -> Void = { seconds in
                    try? await Task.sleep(nanoseconds: UInt64(max(0, seconds) * 1_000_000_000))
                },
                idleAckDelay: TimeInterval? = 0.05) {
        self.desk = desk
        self.client = client
        self.window = window
        let grid = TerminalStreamWire.clamp(cols: cols, rows: rows)
        self.cols = grid.cols
        self.rows = grid.rows
        self.sleep = sleep
        self.idleAckDelay = idleAckDelay
    }

    deinit {
        runTask?.cancel()
        idleAckTask?.cancel()
    }

    // MARK: lifecycle

    /// Opens the socket, unless one is already being run.
    public func start() {
        guard runTask == nil else { return }
        launch()
    }

    public func stop() {
        generation += 1
        runTask?.cancel()
        runTask = nil
        idleAckTask?.cancel()
        outbox?.finish()
        outbox = nil
    }

    /// After an exit or a failure: try again, same window.
    public func retry() {
        stop()
        backoff.reset()
        state = .connecting
        launch()
    }

    public func switchWindow(to target: TerminalWindow) {
        guard target != window || runTask == nil else { return }
        window = target
        isSwitching = true
        retry()
    }

    /// Test seam: waits for the running loop to end.
    public func runFinished() async {
        await runTask?.value
    }

    private func launch() {
        generation += 1
        let mine = generation
        runTask = Task { [weak self] in
            await self?.run(generation: mine)
        }
    }

    // MARK: the emulator

    public func attach(_ sink: TerminalOutputSink) {
        self.sink = sink
        let buffered = pending
        pending = []
        for chunk in buffered { feed(chunk, to: sink) }
    }

    public func detach(_ sink: TerminalOutputSink) {
        if self.sink === sink { self.sink = nil }
    }

    // MARK: what he does

    public func send(input: Data) {
        guard acceptsInput, !input.isEmpty else { return }
        outbox?.yield(.input(input))
    }

    public func resize(cols newCols: Int, rows newRows: Int) {
        let grid = TerminalStreamWire.clamp(cols: newCols, rows: newRows)
        guard grid.cols != cols || grid.rows != rows else { return }
        cols = grid.cols
        rows = grid.rows
        outbox?.yield(.text(TerminalStreamWire.resize(cols: cols, rows: rows)))
    }

    // MARK: the loop

    private func run(generation mine: Int) async {
        while !Task.isCancelled, mine == generation {
            if state != .connecting { state = .connecting }
            let session: TerminalStreamSession
            do {
                session = try await client.openTerminalStream(agent: desk, window: window,
                                                              cols: cols, rows: rows)
            } catch {
                if let failure = Self.failure(for: error) { return finish(.failed(failure), mine) }
                await sleep(backoff.next())
                continue
            }
            guard !Task.isCancelled, mine == generation else { session.close(); return }

            let ending = await pump(session, generation: mine)
            session.close()
            guard mine == generation else { return }
            switch ending {
            case .dropped:
                state = .connecting
                await sleep(backoff.next())
            case .stopped:
                return
            case .exit, .failed:
                return finish(ending, mine)
            }
        }
    }

    private func finish(_ ending: Ending, _ mine: Int) {
        guard mine == generation else { return }
        isSwitching = false
        switch ending {
        case .exit(let code): state = .ended(code: code)
        case .failed(let failure): state = .failed(failure)
        case .dropped, .stopped: break
        }
    }

    private func pump(_ session: TerminalStreamSession, generation mine: Int) async -> Ending {
        processedBytes = 0
        ackedBytes = 0
        pending = []
        let (stream, continuation) = AsyncStream<Outgoing>.makeStream()
        outbox = continuation
        let sender = Task {
            for await message in stream {
                switch message {
                case .input(let bytes): try? await session.send(input: bytes)
                case .text(let text): try? await session.send(text: text)
                }
            }
        }
        defer {
            continuation.finish()
            sender.cancel()
            if mine == generation {
                outbox = nil
                idleAckTask?.cancel()
            }
        }

        return await withTaskCancellationHandler {
            while true {
                let event: TerminalStreamEvent
                do { event = try await session.next() } catch {
                    if Task.isCancelled || mine != generation { return .stopped }
                    if let failure = Self.failure(for: error) { return .failed(failure) }
                    return .dropped
                }
                guard !Task.isCancelled, mine == generation else { return .stopped }
                switch event {
                case .hello(let hello):
                    windows = hello.windows
                    window = hello.window
                    isSwitching = false
                    state = hello.readOnly ? .readOnly : .live
                    backoff.reset()
                    sink?.terminalReset()
                    if hello.cols != cols || hello.rows != rows {
                        continuation.yield(.text(TerminalStreamWire.resize(cols: cols, rows: rows)))
                    }
                case .output(let bytes):
                    if let sink { feed(bytes, to: sink) } else { pending.append(bytes) }
                case .exit(let code):
                    return .exit(code)
                case .error(let reason, let detail):
                    return .failed(.fromError(reason: reason, detail: detail))
                }
            }
        } onCancel: {
            session.close()
        }
    }

    private static func failure(for error: Error) -> TerminalStreamFailure? {
        if let closed = error as? TerminalStreamClosed {
            return closed.handshake
                ? .fromHandshake(status: closed.code - 4000)
                : .fromClose(code: closed.code)
        }
        if let refusal = error as? ScreenRefusal {
            switch refusal {
            case .missingToken, .unauthorized: return .unauthorized
            default: return nil
            }
        }
        return nil
    }

    // MARK: acks

    private func feed(_ bytes: Data, to sink: TerminalOutputSink) {
        sink.terminalFeed(bytes)
        processedBytes += bytes.count
        if processedBytes - ackedBytes >= TerminalStreamWire.ackEvery {
            ack()
        } else {
            scheduleIdleAck()
        }
    }

    private func ack() {
        guard processedBytes > ackedBytes, let outbox else { return }
        ackedBytes = processedBytes
        outbox.yield(.text(TerminalStreamWire.ack(bytes: ackedBytes)))
    }

    /// Output stopped arriving: ack the tail, so a quiet terminal never sits on
    /// unacked bytes the deck is counting against it.
    private func scheduleIdleAck() {
        guard let delay = idleAckDelay else { return }
        idleAckTask?.cancel()
        idleAckTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(max(0, delay) * 1_000_000_000))
            guard !Task.isCancelled else { return }
            self?.ack()
        }
    }
}

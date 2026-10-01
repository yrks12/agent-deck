import Foundation
import ImageIO

/// Knobs for the loop. The defaults are MB1/MB5's numbers; tests shrink the
/// clocks so nothing waits on a real 2 s keepalive or a 60 s grant card.
public struct MacBridgeSettings: Sendable {
    public var machineId: String
    public var name: String
    public var os: String
    public var appVersion: String

    /// Seconds the server may hold a poll (it clamps to 0…25).
    public var pollWait: Double = 25
    /// A live job reports at least this often; 45 s of silence is `lost`.
    public var keepalive: TimeInterval = 2
    /// How often a job's outbox is looked at.
    public var tick: TimeInterval = 0.25
    /// How long a job waits for the owner's tap before ending `refused/awaiting_grant`.
    public var grantWait: TimeInterval = 60
    public var maxConcurrent = 4
    public var perDesk = 2
    public var perMinute = 120
    public var backoffBase: Double = 1
    public var backoffCap: Double = 30
    /// Quit / Pause: how long to wait for killed jobs to settle and report.
    public var stopGrace: TimeInterval = 5
    /// How long a finished job's result is retried while the deck is unreachable.
    public var resultRetry: TimeInterval = 60
    /// Base64 screenshot payload cap (MB1: ≤ 2 MiB).
    public var screenshotMax = 2 << 20

    public init(machineId: String, name: String, os: String, appVersion: String) {
        self.machineId = machineId
        self.name = name
        self.os = os
        self.appVersion = appVersion
    }
}

/// **The Mac bridge: dial out, claim jobs, run them under the Mac's own policy, report.**
///
/// register → `poll(wait: 25)` forever on the client's own session, independent
/// of `DeckStore` and of the window. Failures back off 1, 2, 4 … 30 s. `Off` and
/// `Paused` do not poll (Pause tells the deck once, with a `paused` heartbeat,
/// so desks see *paused* rather than *offline*). Quit (`stop()`) kills every
/// job and waits for them to settle: nothing executes while the app is not
/// running.
///
/// The Mac decides, not the server: every job goes through `MacPolicy`. An
/// `ask` posts `awaiting_grant`, puts up one card per desk and waits up to
/// 60 s for the tap; after that the job ends `refused/awaiting_grant` and a
/// late tap is sent to the deck (`POST /grants`) so the desk is told to retry.
///
/// Commands, `open` and screenshots run through the injected
/// `MacJobExecuting`; file ops run in-process (`MacFileOps`). Every live job
/// posts its events at least every `keepalive` seconds, and a `cancel: true`
/// on any reply — or the deck listing it in a poll's `cancel` — kills it.
public actor MacBridge {

    // MARK: dependencies

    private let client: any MacNodeClienting
    private let executor: any MacJobExecuting
    private var policy: MacPolicy
    private let policyStore: MacPolicyStore?
    private let activity: MacActivityLog?
    private let settings: MacBridgeSettings
    private let now: @Sendable () -> Date
    private let sleeper: @Sendable (TimeInterval) async throws -> Void
    private let onState: @Sendable (MacBridgeState) -> Void

    // MARK: state

    private var state: MacBridgeState
    private var nodeId: String?
    private var needsRegister = true
    private var stopped = false
    private var pausedTold = false
    private var loopTask: Task<Void, Never>?
    private var pollTask: Task<MacPollResponse, Error>?
    private var parked: CheckedContinuation<Void, Never>?
    private var live: [String: LiveJob] = [:]
    private var reporters: [String: Task<Void, Never>] = [:]
    private var waiters: [String: (desk: String, resume: CheckedContinuation<GrantOutcome, Never>)] = [:]
    private var accepted: [Date] = []

    private struct LiveJob {
        let job: MacWireJob
        let outbox: MacJobOutbox
        var run: (any MacJobRun)?
        var task: Task<Void, Never>?
        var cancelled = false
        /// False for a job refused on arrival (over a limit): it is only
        /// being reported, and must not take a slot from the next one.
        var holdsSlot = true
    }

    private var busy: [LiveJob] { live.values.filter(\.holdsSlot) }

    private enum GrantOutcome: Sendable { case granted, denied, timedOut, cancelled }

    public init(client: any MacNodeClienting, executor: any MacJobExecuting, policy: MacPolicy,
                policyStore: MacPolicyStore? = nil, activity: MacActivityLog? = nil, settings: MacBridgeSettings,
                now: @escaping @Sendable () -> Date = { Date() },
                sleep: @escaping @Sendable (TimeInterval) async throws -> Void = { try await Task.sleep(nanoseconds: UInt64($0 * 1e9)) },
                onState: @escaping @Sendable (MacBridgeState) -> Void = { _ in }) {
        self.client = client
        self.executor = executor
        self.policy = policy
        self.policyStore = policyStore
        self.activity = activity
        self.settings = settings
        self.now = now
        self.sleeper = sleep
        self.onState = onState
        state = MacBridgeState(mode: policy.config.mode,
                               connection: MacBridgeState.initialConnection(mode: policy.config.mode, deck: client.deckURL),
                               recent: activity?.recent() ?? [])
    }

    // MARK: public surface

    /// What DeckUI draws.
    public var snapshot: MacBridgeState { state }

    /// The policy as it stands (Settings reads it).
    public var policyConfig: MacPolicyConfig { policy.config }

    /// 1, 2, 4 … `cap` seconds.
    public static func backoff(attempt: Int, base: Double = 1, cap: Double = 30) -> Double {
        let n = max(1, min(attempt, 32))
        return min(cap, base * pow(2, Double(n - 1)))
    }

    public func start() {
        guard loopTask == nil, !stopped else { return }
        publish()
        loopTask = Task { await self.loop() }
    }

    /// Quit: kill every job, wait (bounded) for them to settle and report, stop.
    public func stop() async {
        guard !stopped else { return }
        stopped = true
        loopTask?.cancel()
        pollTask?.cancel()
        wake()
        await killAllAndSettle()
        for r in reporters.values { r.cancel() }
        state.connection = .off
        publish()
    }

    /// Off / Ask me / Full access / Paused. Off and Paused kill every job now.
    public func setMode(_ mode: MacMode) async {
        var config = policy.config
        config.mode = mode
        await setPolicy(config)
    }

    /// Settings changed anything (folders, Never touch, toggles, mode).
    public func setPolicy(_ config: MacPolicyConfig) async {
        let old = policy.config
        policy.config = config
        try? policyStore?.save(config)
        state.mode = config.mode
        if old.openEnabled != config.openEnabled || old.screenshotEnabled != config.screenshotEnabled {
            needsRegister = true   // the deck must stop offering what this Mac refuses
        }
        if config.mode == .off || config.mode == .paused {
            if old.mode != config.mode { pausedTold = false }
            pollTask?.cancel()
            state.connection = config.mode == .off ? .off : .paused
            publish()
            await killAllAndSettle()
        }
        wake()
        publish()
    }

    /// What he tapped on the grant card (or Revoke in Settings). A tap while
    /// the job still waits runs (or refuses) it; a tap after it gave up — or
    /// any revoke — is sent to the deck, which tells the desk.
    public func answerGrant(desk: String, decision: MacGrantDecision, unconfined: Bool = false) async {
        let t = now().timeIntervalSince1970
        policy.apply(decision, desk: desk, now: t, unconfined: unconfined)
        try? policyStore?.save(policy.config)
        let card = state.pendingGrants.first { $0.desk == desk }
        state.answered(desk: desk)
        let granted = decision == .hour || decision == .always
        log(MacActivityRow(ts: t, jobId: card?.jobId ?? "", desk: desk, kind: "grant",
                           summary: "\(decision.rawValue)\(card.map { ": " + $0.summary } ?? "")",
                           decision: granted ? .granted : .denied))
        let waiting = waiters.filter { $0.value.desk == desk }.map(\.key)
        for id in waiting { resumeWaiter(id, granted ? .granted : .denied) }
        publish()
        guard waiting.isEmpty || decision == .revoke, let node = nodeId else { return }
        let post = MacGrantPost(desk: desk, decision: decision, until: policy.config.grants[desk]?.until)
        _ = try? await client.postGrant(nodeId: node, post)
    }

    /// Stop in the in-use bar: that desk's jobs, now.
    public func stopDesk(_ desk: String) {
        for (id, entry) in live where entry.job.desk == desk { cancelJob(id) }
    }

    public func stopJob(_ id: String) { cancelJob(id) }

    // MARK: the loop

    private func loop() async {
        var failures = 0
        while !stopped && !Task.isCancelled {
            guard MacTransport.allowed(client.deckURL) else {
                setConnection(.refused(MacTransport.refusal))
                await park()
                continue
            }
            let mode = policy.config.mode
            if mode == .off {
                setConnection(.off)
                await park()
                continue
            }
            if mode == .paused {
                setConnection(.paused)
                if !pausedTold, let node = nodeId {
                    pausedTold = true
                    _ = try? await client.poll(nodeId: node, MacPollRequest(wait: 0, freeSlots: 0, running: [], mode: .paused,
                                                                          grants: wireGrants()))
                }
                if policy.config.mode == .paused && !stopped { await park() }
                continue
            }
            pausedTold = false
            if nodeId == nil || needsRegister {
                if nodeId == nil { setConnection(.connecting) }
                do {
                    let r = try await client.register(registerBody(mode))
                    nodeId = r.nodeId
                    needsRegister = false
                    failures = 0
                } catch {
                    if stopped || Task.isCancelled { break }
                    failures += 1
                    await backoff(failures)
                    continue
                }
            }
            guard let node = nodeId else { continue }
            let body = MacPollRequest(wait: settings.pollWait, freeSlots: max(0, settings.maxConcurrent - busy.count),
                                      running: live.keys.sorted(), mode: mode, grants: wireGrants())
            let client = self.client
            let task = Task { try await client.poll(nodeId: node, body) }
            pollTask = task
            do {
                let r = try await task.value
                pollTask = nil
                failures = 0
                setConnection(.online)
                for id in r.cancel { cancelJob(id) }
                for job in r.jobs { accept(job) }
            } catch {
                pollTask = nil
                if stopped || Task.isCancelled { break }
                if policy.config.mode != mode { continue }   // paused / off mid-poll
                if (error as? MacNodeError)?.isUnknownNode == true { nodeId = nil }
                failures += 1
                setConnection(.connecting)
                await backoff(failures)
            }
        }
    }

    private func backoff(_ attempt: Int) async {
        try? await sleeper(Self.backoff(attempt: attempt, base: settings.backoffBase, cap: settings.backoffCap))
    }

    private func park() async {
        await withCheckedContinuation { parked = $0 }
    }

    private func wake() {
        parked?.resume()
        parked = nil
    }

    private func setConnection(_ c: MacConnection) {
        guard state.connection != c else { return }
        state.connection = c
        publish()
    }

    private func publish() { onState(state) }

    private func registerBody(_ mode: MacMode) -> MacRegisterRequest {
        let caps = MacJobKind.allCases.filter {
            ($0 != .open || policy.config.openEnabled) && ($0 != .screenshot || policy.config.screenshotEnabled)
        }
        return MacRegisterRequest(machineId: settings.machineId, name: settings.name, os: settings.os,
                                  appVersion: settings.appVersion, capabilities: caps, mode: mode)
    }

    private func wireGrants() -> [String: MacWireGrant] {
        let t = now().timeIntervalSince1970
        var out: [String: MacWireGrant] = [:]
        for desk in policy.config.grants.keys {
            if let g = policy.grant(for: desk, at: t) { out[desk] = MacWireGrant(state: g.state, until: g.until) }
        }
        return out
    }

    // MARK: jobs

    private func accept(_ job: MacWireJob) {
        guard live[job.id] == nil, !stopped else { return }
        let t = now()
        accepted.removeAll { t.timeIntervalSince($0) >= 60 }
        var refusal: (String, String)?
        if busy.count >= settings.maxConcurrent {
            refusal = ("node_busy", "This Mac is already running \(settings.maxConcurrent) jobs. Wait for one to finish.")
        } else if busy.filter({ $0.job.desk == job.desk }).count >= settings.perDesk {
            refusal = ("node_busy", "You already have \(settings.perDesk) jobs running on this Mac. Wait for one to finish.")
        } else if accepted.count >= settings.perMinute {
            refusal = ("rate_limited", "At most \(settings.perMinute) jobs a minute on this Mac; wait and batch the work.")
        }
        accepted.append(t)
        let outbox = MacJobOutbox()
        live[job.id] = LiveJob(job: job, outbox: outbox, holdsSlot: refusal == nil)
        reporters[job.id] = Task { await self.report(job.id, outbox) }
        let task = Task.detached { [self] in
            if let (reason, detail) = refusal {
                await self.finish(job, outbox, summary: self.summary(job), result: MacJobResultData(state: .refused, reason: reason, detail: detail))
            } else {
                await self.perform(job, outbox)
            }
        }
        live[job.id]?.task = task
    }

    private func cancelJob(_ id: String) {
        guard var entry = live[id] else { return }
        entry.cancelled = true
        live[id] = entry
        entry.run?.cancel()
        resumeWaiter(id, .cancelled)
    }

    private func killAllAndSettle() async {
        let tasks = live.values.compactMap(\.task)
        for id in Array(live.keys) { cancelJob(id) }
        for id in Array(waiters.keys) { resumeWaiter(id, .cancelled) }
        let reports = Array(reporters.values)
        guard !tasks.isEmpty || !reports.isEmpty else { return }
        let grace = settings.stopGrace
        await withTaskGroup(of: Void.self) { group in
            group.addTask {
                for t in tasks { await t.value }
                for r in reports { await r.value }
            }
            group.addTask { try? await Task.sleep(nanoseconds: UInt64(grace * 1e9)) }
            await group.next()
            group.cancelAll()
        }
    }

    /// Attach a started run; false when the job was cancelled meanwhile.
    private func attach(_ id: String, _ run: any MacJobRun) -> Bool {
        guard var entry = live[id], !entry.cancelled else { return false }
        entry.run = run
        live[id] = entry
        return true
    }

    private func isCancelled(_ id: String) -> Bool { live[id]?.cancelled ?? true }

    private func decide(_ request: MacJobRequest) -> MacDecision {
        policy.decide(request, now: now().timeIntervalSince1970)
    }

    private func context() -> (home: String, folders: [String]) { (policy.paths.home, policy.resolvedFolders) }

    private func markRunning(_ job: MacWireJob, _ summary: String) {
        state.started(MacRunningJob(id: job.id, desk: job.desk, summary: summary, startedAt: now()))
        publish()
    }

    private func waitForGrant(_ request: MacGrantRequest) async -> GrantOutcome {
        if isCancelled(request.jobId) { return .cancelled }
        state.asked(request)
        log(MacActivityRow(ts: now().timeIntervalSince1970, jobId: request.jobId, desk: request.desk,
                           kind: live[request.jobId]?.job.kind ?? "", summary: request.summary, decision: .asked))
        publish()
        let wait = settings.grantWait
        let id = request.jobId
        return await withCheckedContinuation { c in
            waiters[id] = (request.desk, c)
            Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
                await self?.resumeWaiter(id, .timedOut)
            }
        }
    }

    private func resumeWaiter(_ jobId: String, _ outcome: GrantOutcome) {
        waiters.removeValue(forKey: jobId)?.resume.resume(returning: outcome)
    }

    private func log(_ row: MacActivityRow) {
        activity?.append(row)
        state.logged(row)
    }

    private func finished(_ job: MacWireJob, row: MacActivityRow) {
        live[job.id] = nil
        activity?.append(row)
        state.finished(jobId: job.id, row: row)
        publish()
    }

    nonisolated private func summary(_ job: MacWireJob) -> String {
        job.jobKind.map { MacWire.summary(kind: $0, args: job.args) } ?? job.kind
    }

    /// Queue the result and record the row.
    nonisolated private func finish(_ job: MacWireJob, _ outbox: MacJobOutbox, summary: String, result: MacJobResultData,
                                    sandboxed: Bool? = nil, outBytes: Int? = nil) async {
        outbox.add(.result(result))
        let row = MacActivityRow(ts: now().timeIntervalSince1970, jobId: job.id, desk: job.desk, kind: job.kind,
                                 summary: summary, decision: result.state == .refused ? .refused : .ran,
                                 state: result.state.rawValue, reason: result.reason, exit: result.exit,
                                 durationMs: result.durationMs, outBytes: outBytes, sandboxed: sandboxed)
        await finished(job, row: row)
    }

    // MARK: one job, off the actor

    nonisolated private func perform(_ job: MacWireJob, _ outbox: MacJobOutbox) async {
        let summary = summary(job)
        guard let kind = job.jobKind else {
            return await finish(job, outbox, summary: summary, result: MacJobResultData(
                state: .refused, reason: "bad_input", detail: "This Mac does not know the job kind \"\(job.kind)\"; update Agent Deck on the Mac."))
        }
        let request = MacJobRequest(id: job.id, desk: job.desk, kind: kind, path: job.args.path, cwd: job.args.cwd, summary: summary)
        var decision = await decide(request)
        if case .ask(let card) = decision {
            outbox.add(.awaitingGrant)
            switch await waitForGrant(card) {
            case .granted:
                decision = await decide(request)
                if case .ask = decision {
                    decision = .refuse(reason: "awaiting_grant", detail: "The owner's answer did not cover this; ask again.")
                }
            case .denied:
                decision = .refuse(reason: "denied", detail: "The owner said no to you using his Mac. Do not ask again today; finish without it.")
            case .timedOut:
                decision = .refuse(reason: "awaiting_grant",
                                   detail: "The owner has not answered the request to use his Mac yet. The card stays on his screen; you will be told when he answers — do not retry before that.")
            case .cancelled:
                return await finish(job, outbox, summary: summary, result: MacJobResultData(state: .cancelled, detail: "Cancelled before it started."))
            }
        }
        guard case .run(let profile, let path, let cwd) = decision else {
            if case .refuse(let reason, let detail) = decision {
                return await finish(job, outbox, summary: summary, result: MacJobResultData(state: .refused, reason: reason, detail: detail))
            }
            return await finish(job, outbox, summary: summary, result: MacJobResultData(state: .refused, reason: "awaiting_grant"))
        }
        if await isCancelled(job.id) {
            return await finish(job, outbox, summary: summary, result: MacJobResultData(state: .cancelled, detail: "Cancelled before it started."))
        }
        let (home, folders) = await context()
        let timeout = TimeInterval(min(600, max(1, job.timeoutS)))

        switch kind {
        case .read, .write, .list:
            await fileOp(job, kind, path ?? "", home: home, outbox, summary)
        case .run:
            let spec = MacRunSpec(command: job.args.command ?? "", cwd: cwd ?? folders.first, env: job.args.env ?? [:],
                                  desk: job.desk, jobId: job.id, timeout: timeout, sandboxProfile: profile?.text,
                                  folders: folders)
            await execute(job, spec, outbox, summary)
        case .open:
            switch Self.openCommand(job.args.target ?? "") {
            case .failure(let refusal):
                await finish(job, outbox, summary: summary, result: MacJobResultData(state: .refused, reason: refusal.reason, detail: refusal.detail))
            case .success(let command):
                var spec = MacRunSpec(command: command, desk: job.desk, jobId: job.id, timeout: min(timeout, 60), shell: "/bin/sh")
                spec.cwd = nil
                await execute(job, spec, outbox, summary)
            }
        case .screenshot:
            let file = NSTemporaryDirectory() + "agent-deck-shot-\(job.id).jpg"
            let spec = MacRunSpec(command: "/usr/sbin/screencapture -x -t jpg " + Self.quote(file), desk: job.desk,
                                  jobId: job.id, timeout: min(timeout, 60), shell: "/bin/sh")
            await execute(job, spec, outbox, summary, screenshot: file)
        }
    }

    nonisolated private func execute(_ job: MacWireJob, _ spec: MacRunSpec, _ outbox: MacJobOutbox, _ summary: String,
                                     screenshot: String? = nil) async {
        let activity = self.activity
        let run = executor.start(spec) { stream, data in
            outbox.output(stream, data)
            if screenshot == nil { activity?.recordOutput(jobId: spec.jobId, data) }
        }
        if !(await attach(job.id, run)) { run.cancel() }
        if run.pid > 0 {
            outbox.add(.started(MacStartedData(pid: run.pid, cwd: run.cwd, sandboxed: run.sandboxed)))
        }
        await markRunning(job, summary)
        let r: MacRunResult = await withCheckedContinuation { c in run.onFinish { c.resume(returning: $0) } }

        var result = MacJobResultData(state: Self.resultState(r.state), exit: r.exit, signal: r.signal, reason: r.reason,
                                      detail: r.detail, durationMs: r.durationMs)
        if let file = screenshot {
            defer { try? FileManager.default.removeItem(atPath: file) }
            if result.state == .done {
                if let payload = FileManager.default.contents(atPath: file).flatMap({ Self.jpegPayload($0, maxBase64: settings.screenshotMax) }) {
                    result.payload = payload
                } else {
                    result.state = .failed
                    result.reason = "tcc_denied"
                    result.detail = "macOS did not let Agent Deck record the screen. The owner must allow it in System Settings → Privacy & Security → Screen Recording → Agent Deck, then you can retry."
                }
            }
        }
        await finish(job, outbox, summary: summary, result: result, sandboxed: r.sandboxed,
                     outBytes: r.stdoutBytes + r.stderrBytes)
    }

    nonisolated private func fileOp(_ job: MacWireJob, _ kind: MacJobKind, _ path: String, home: String,
                                    _ outbox: MacJobOutbox, _ summary: String) async {
        outbox.add(.started(MacStartedData(pid: nil, cwd: nil, sandboxed: false)))
        await markRunning(job, summary)
        let began = Date()
        let a = job.args
        var payload: [String: MacJSON] = [:]
        var failure: MacOpError?
        switch kind {
        case .read:
            switch MacFileOps.read(path, offset: a.offset ?? 0, length: a.length ?? MacFileOps.textPageMax,
                                   encoding: a.encoding ?? "text", home: home) {
            case .success(let r):
                if let t = r.text { payload["text"] = .string(t) }
                if let b = r.base64 { payload["base64"] = .string(b) }
                payload["size"] = .int(r.size)
                payload["offset"] = .int(r.offset)
                payload["returned"] = .int(r.returned)
                payload["truncated"] = .bool(r.truncated)
            case .failure(let e): failure = e
            }
        case .write:
            switch MacFileOps.write(path, content: a.content ?? "", encoding: a.encoding ?? "text", mode: a.mode ?? "overwrite",
                                    makeDirs: a.makeDirs ?? false, home: home) {
            case .success(let r): payload = ["bytes": .int(r.bytes), "path": .string(r.path)]
            case .failure(let e): failure = e
            }
        default:
            switch MacFileOps.list(path, hidden: a.hidden ?? false, home: home) {
            case .success(let r):
                payload = ["path": .string(r.path), "truncated": .bool(r.truncated),
                           "entries": .array(r.entries.map {
                               ["name": .string($0.name), "kind": .string($0.kind.rawValue), "size": .int($0.size),
                                "modified": .double($0.modified)]
                           })]
            case .failure(let e): failure = e
            }
        }
        let ms = Int(Date().timeIntervalSince(began) * 1000)
        let result = failure.map { MacJobResultData(state: .failed, reason: $0.reason, detail: $0.detail, durationMs: ms) }
            ?? MacJobResultData(state: .done, durationMs: ms, payload: payload)
        await finish(job, outbox, summary: summary, result: result, sandboxed: false)
    }

    // MARK: reporting

    /// One job's reporter: sends its outbox in order, a keepalive when
    /// nothing else went for `keepalive` seconds, and kills the job when the
    /// deck says cancel or no longer has it.
    private func report(_ id: String, _ outbox: MacJobOutbox) async {
        var lastPost = ProcessInfo.processInfo.systemUptime - settings.keepalive
        var closedAt: TimeInterval?
        while !Task.isCancelled {
            if outbox.done { break }
            let clock = ProcessInfo.processInfo.systemUptime
            if outbox.closed, closedAt == nil { closedAt = clock }
            if let c = closedAt, clock - c > settings.resultRetry { break }
            let batch = outbox.batch()
            if let node = nodeId, !batch.isEmpty || clock - lastPost >= settings.keepalive {
                do {
                    let r = try await client.postEvents(nodeId: node, jobId: id, batch)
                    if let last = batch.last { outbox.ack(through: last.seq) }
                    lastPost = ProcessInfo.processInfo.systemUptime
                    if r.cancel { cancelJob(id) }
                } catch let e as MacNodeError where e.jobIsGone {
                    cancelJob(id)   // the deck settled or forgot it: nothing here may keep running
                    break
                } catch {
                    // Deck unreachable: keep the events (same seqs) and retry.
                }
                continue   // look again at once: more may be queued
            }
            try? await Task.sleep(nanoseconds: UInt64(settings.tick * 1e9))
        }
        reporters[id] = nil
    }

    // MARK: helpers

    nonisolated static func resultState(_ s: MacRunResult.State) -> MacResultState {
        switch s {
        case .done: return .done
        case .failed: return .failed
        case .cancelled: return .cancelled
        case .timedOut: return .timedOut
        }
    }

    /// POSIX single-quote.
    nonisolated static func quote(_ s: String) -> String { "'" + s.replacingOccurrences(of: "'", with: #"'\''"#) + "'" }

    /// `open` a web/mail link, a path, or an app by name — never an option,
    /// never another URL scheme (`file:`, custom app schemes).
    nonisolated static func openCommand(_ target: String) -> Result<String, MacOpError> {
        let t = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, !t.hasPrefix("-"), !t.contains("\0"), !t.contains("\n") else {
            return .failure(MacOpError("bad_input", "Give a link (http, https, mailto), a path, or an app name."))
        }
        if t.hasPrefix("/") || t.hasPrefix("~") {
            let path = t.hasPrefix("~") ? NSHomeDirectory() + t.dropFirst() : t
            return .success("/usr/bin/open " + quote(path))
        }
        if let colon = t.firstIndex(of: ":"), t[..<colon].allSatisfy({ $0.isLetter || $0.isNumber || "+.-".contains($0) }) {
            let scheme = t[..<colon].lowercased()
            guard ["http", "https", "mailto"].contains(scheme) else {
                return .failure(MacOpError("bad_input", "Only http, https and mailto links can be opened on this Mac."))
            }
            return .success("/usr/bin/open " + quote(t))
        }
        return .success("/usr/bin/open -a " + quote(t))
    }

    /// `{mime, base64, width, height}`, re-encoded smaller until it fits.
    nonisolated static func jpegPayload(_ data: Data, maxBase64: Int) -> [String: MacJSON]? {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else { return nil }
        var bytes = data
        var side = 2560
        while (bytes.count + 2) / 3 * 4 > maxBase64 {
            guard side >= 320 else { return nil }
            let options: [CFString: Any] = [kCGImageSourceCreateThumbnailFromImageAlways: true,
                                            kCGImageSourceThumbnailMaxPixelSize: side,
                                            kCGImageSourceCreateThumbnailWithTransform: true]
            guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return nil }
            let out = NSMutableData()
            guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil) else { return nil }
            CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.6] as CFDictionary)
            guard CGImageDestinationFinalize(dest) else { return nil }
            bytes = out as Data
            side = side * 2 / 3
        }
        guard let final = CGImageSourceCreateWithData(bytes as CFData, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(final, 0, nil) as? [CFString: Any],
              let w = props[kCGImagePropertyPixelWidth] as? Int, let h = props[kCGImagePropertyPixelHeight] as? Int
        else { return nil }
        return ["mime": "image/jpeg", "base64": .string(bytes.base64EncodedString()), "width": .int(w), "height": .int(h)]
    }
}

/// **One job's events, in order, with their `seq` fixed once.**
///
/// Output arrives on the executor's threads; it is held as bytes (so a
/// character split across two reads is not mangled) and turned into
/// `stdout`/`stderr` events — at most 65 536 code points each — only after
/// `started`, and all of it before `result`. Events stay here until the deck
/// acknowledges them, so a failed POST is retried with the same `seq`s.
final class MacJobOutbox: @unchecked Sendable {
    private let lock = NSLock()
    private var nextSeq = 1
    private var pending: [MacJobEvent] = []
    private var raw: [MacStream: Data] = [.stdout: Data(), .stderr: Data()]
    private var outputOpen = false
    private var isClosed = false

    static let batchMax = 16

    func add(_ body: MacJobEvent.Body) {
        lock.withLock {
            guard !isClosed else { return }
            if case .result = body {
                flushRaw(final: true)
                isClosed = true
            }
            append(body)
            if case .started = body { outputOpen = true }
        }
    }

    func output(_ stream: MacStream, _ data: Data) {
        lock.withLock {
            guard !isClosed, !data.isEmpty else { return }
            raw[stream, default: Data()].append(data)
        }
    }

    /// The oldest unacknowledged events, output converted first.
    func batch() -> [MacJobEvent] {
        lock.withLock {
            if outputOpen && !isClosed { flushRaw(final: false) }
            return Array(pending.prefix(Self.batchMax))
        }
    }

    func ack(through seq: Int) { lock.withLock { pending.removeAll { $0.seq <= seq } } }

    var closed: Bool { lock.withLock { isClosed } }
    var done: Bool { lock.withLock { isClosed && pending.isEmpty } }

    private func append(_ body: MacJobEvent.Body) {
        pending.append(MacJobEvent(seq: nextSeq, body: body))
        nextSeq += 1
    }

    private func flushRaw(final: Bool) {
        for stream in [MacStream.stdout, .stderr] {
            guard let data = raw[stream], !data.isEmpty else { continue }
            let n = final ? data.count : MacWire.completeUTF8Prefix(data)
            guard n > 0 else { continue }
            let text = String(decoding: data.prefix(n), as: UTF8.self)
            raw[stream] = Data(data.dropFirst(n))
            for chunk in MacWire.chunks(text) { append(stream == .stdout ? .stdout(chunk) : .stderr(chunk)) }
        }
    }
}

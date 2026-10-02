import Foundation

/// A canned deck, shaped exactly like the real one: `direct:<name>` and
/// `peer:<a>|<b>` thread ids, epoch timestamps, opaque sortable cursors, the
/// owner as `DeckOwner.name`, and server-formatted preview lines.
///
/// It exists so the app launches and is fully explorable with no deck running,
/// and so the views have every shape they must survive: two sections, pinned
/// favourites, unread rows, relayed previews in both directions, a read-only
/// peer thread, and a message carrying a link, a path and an image.
///
/// Swapping this for `HTTPDeckClient` changes one line at the app's root.
public struct FixtureDeckClient: DeckClient {
    private let now: Date
    /// Answered approvals, so a card tapped in the fixture app stays gone.
    /// Reference type on purpose: the client itself is a value.
    private let answered = AnsweredApprovals()
    private let deletedRoutines = AnsweredApprovals()
    /// Handed-over steps he has answered, so a card tapped in the fixture app
    /// stays gone rather than being re-raised on the next poll.
    private let resolvedHandoffs = AnsweredApprovals()

    /// Draw a made-up page for the agent's screen instead of refusing a frame.
    /// For documentation screenshots only; off unless asked.
    private let demoScreenPage: Bool

    public init(now: Date = Date(), demoScreenPage: Bool = false) {
        self.now = now
        self.demoScreenPage = demoScreenPage
    }

    private func ago(_ minutes: Double) -> Date { now.addingTimeInterval(-minutes * 60) }

    // MARK: roster

    public var agents: [Agent] {
        [
            Agent(name: "chief", title: "The Builder",
                  detail: "Runs the board. The only desk you talk to directly.",
                  summary: "Chief of staff: you talk to the chief, the chief runs the team.",
                  section: "Work", state: .working, isPinned: true,
                  unread: 2, lastActivityAt: ago(3),
                  preview: "Messaged Hemingway: wave 7 is merged, two branches left",
                  reports: ["hemingway", "seeker"], project: "acme-os", sessionID: "sid-chief"),
            Agent(name: "hemingway", title: "Designer",
                  detail: "Turns rough notes into something a person would read.",
                  summary: "Turns rough notes into something a person would read.",
                  section: "Work", state: .idle, isPinned: true,
                  unread: 0, lastActivityAt: ago(12),
                  preview: "the second draft reads much better",
                  boss: "chief", project: "acme-os", sessionID: "sid-hem"),
            Agent(name: "seeker", title: "Researcher",
                  detail: "Digs up sources and checks the ones you already have.",
                  summary: "Digs up sources and checks the ones you already have.",
                  section: "Work", state: .needsYou, isPinned: true,
                  unread: 5, lastActivityAt: ago(26),
                  preview: "Message from Seeker: three papers, one of them contradicts you",
                  boss: "chief", project: "acme"),
            Agent(name: "ledger", title: "Email",
                  detail: "Watches the inbox and drafts the replies you keep putting off.",
                  summary: "Watches the inbox and drafts the replies you keep putting off.",
                  section: "Work", state: .offline,
                  unread: 0, lastActivityAt: ago(140),
                  preview: "You: nine replies drafted, none sent yet",
                  boss: "chief"),
            Agent(name: "travel scout", title: "Travel Scout",
                  detail: "Prices routes and holds the options until you decide.",
                  summary: "Prices routes and holds the options until you decide.",
                  section: "Personal", state: .done, notificationsEnabled: false,
                  unread: 1, lastActivityAt: ago(55),
                  preview: "Lisbon in October is half the price of Rome"),
            Agent(name: "larder", title: "Email",
                  detail: "Household mail, receipts and renewals.",
                  summary: "Household mail, receipts and renewals.",
                  section: "Personal", state: .offline,
                  unread: 0, lastActivityAt: ago(600),
                  preview: "insurance renews on the 14th"),
        ]
    }

    /// Only the peer threads need stating: a direct thread per agent is
    /// synthesised from the roster row, exactly as the HTTP client does.
    ///
    /// Two of them, because the desk sweeps two reports in one breath and the
    /// relay has to be seen doing that rather than doing it once.
    public var peerThreads: [ThreadSummary] {
        [
            ThreadSummary(
                id: "peer:chief|hemingway", kind: .peer, title: "chief ⇄ hemingway",
                agentName: "chief", participants: ["chief", "hemingway"],
                isReadOnly: true, unreadCount: 0, messageCount: 2,
                lastActivity: ago(16), lastCursor: "0000000035-p2",
                preview: ThreadPreview(text: "second draft: out. last line: cut. 186 words.",
                                       relay: .messaged("hemingway"))
            ),
            ThreadSummary(
                id: "peer:chief|seeker", kind: .peer, title: "chief ⇄ seeker",
                agentName: "chief", participants: ["chief", "seeker"],
                isReadOnly: true, unreadCount: 0, messageCount: 2,
                lastActivity: ago(17), lastCursor: "0000000034-s2",
                preview: ThreadPreview(text: "3 new sources, 1 contradiction, no retractions",
                                       relay: .received("seeker"))
            ),
        ]
    }

    public func roster() async throws -> RosterPayload {
        var payload = AgentsResponse.roster(from: agents)
        payload.threads.append(contentsOf: peerThreads)
        return payload
    }

    public func threads() async throws -> [ThreadSummary] {
        try await roster().threads
    }

    public func agent(named name: String) async throws -> Agent {
        guard let found = agents.first(where: { $0.name == name }) else {
            throw DeckError.unknownAgent
        }
        return found
    }

    public func updateAgent(_ agent: Agent) async throws -> Agent { agent }

    /// Answers the way the deck would, and writes nothing. The canned roster
    /// is deliberately immutable: the owner hires his own desks, and a fixture that
    /// grew one would be a fixture that had done something on his behalf.
    public func createAgent(_ draft: AgentDraft) async throws -> Agent {
        if let problem = draft.problem { throw DeckError.transport(problem) }
        guard !agents.contains(where: { $0.name == draft.name }) else {
            throw DeckError.createRefused(
                .nameTaken, detail: "a desk named '\(draft.name)' already exists"
            )
        }
        return Agent(
            name: draft.name, title: draft.title, detail: draft.detail,
            state: .offline, preview: "No messages yet",
            boss: draft.boss, project: draft.directory
        )
    }

    /// A provisional desk, the way the real route will answer: a placeholder
    /// name and title that the agent replaces once it has been told what it is
    /// for. Nothing here is the owner's to type.
    public func startInterview(_ draft: InterviewDraft) async throws -> InterviewOutcome {
        if let problem = draft.problem { throw DeckError.transport(problem) }
        let suffix = String(UUID().uuidString.prefix(4)).lowercased()
        let name = "agent-\(suffix)"
        return InterviewOutcome(agent: Agent(
            name: name,
            title: "Working out what to be",
            detail: draft.roleHint,
            state: .offline,
            threadID: "direct:\(name)",
            preview: "Tell me what you want me for."
        ))
    }

    // MARK: threads

    public func messages(threadID: String, since: String?, limit: Int) async throws -> MessagePage {
        let all = Self.transcripts(threadID: threadID, now: now)
        let peer = peerThreads.first { $0.id == threadID }
        let isKnown = peer != nil || threadID.hasPrefix("direct:")
        guard isKnown else { throw DeckError.unknownThread }

        // `since` is exclusive, exactly as the contract says.
        let page = since.map { cursor in all.filter { $0.cursor > cursor } } ?? all
        let participants = peer?.participants
            ?? [DeckOwner.name, String(threadID.dropFirst("direct:".count))]

        return MessagePage(
            threadID: threadID,
            kind: peer == nil ? .direct : .peer,
            messages: page,
            isReadOnly: peer != nil,
            participants: participants,
            nextSince: all.last?.cursor
        )
    }

    public func messages(threadID: String, before: String, limit: Int) async throws -> MessagePage {
        let older = Self.transcripts(threadID: threadID, now: now).filter { $0.cursor < before }
        let page = Array(older.suffix(max(1, limit)))
        return MessagePage(
            threadID: threadID,
            kind: peerThreads.contains { $0.id == threadID } ? .peer : .direct,
            messages: page,
            isReadOnly: peerThreads.contains { $0.id == threadID },
            participants: [],
            hasMoreBefore: older.count > page.count,
            nextBefore: page.first?.cursor
        )
    }

    public func send(threadID: String, text: String, replyTo: String?) async throws -> Message {
        let stamp = Date()
        return Message(
            id: UUID().uuidString,
            cursor: String(format: "%018.0f-local", stamp.timeIntervalSince1970 * 1_000_000),
            threadID: threadID,
            author: DeckOwner.name,
            role: .owner,
            sentAt: stamp,
            text: text
        )
    }

    public func markRead(agent: String, upTo: String?) async throws {}

    // MARK: routines

    /// Two schedules in the two shapes the panel has to phrase, plus a paused
    /// one so the disabled row is reviewable.
    public func routines() async throws -> [Routine] {
        let json = """
        {"routines": [
          {"id": "rt_morning", "agent": "chief", "prompt": "What moved yesterday, in five lines.",
           "trigger": {"kind": "cron", "spec": "0 8 * * 1-5", "tz": "\(TimeZone.current.identifier)"},
           "next_run_at": \(now.addingTimeInterval(3600 * 14).timeIntervalSince1970), "enabled": true,
           "runs": [{"ts": \(now.addingTimeInterval(-3600 * 10).timeIntervalSince1970), "ok": true, "detail": "delivered"}]},
          {"id": "rt_sweep", "agent": "chief", "prompt": "Check the queue for anything stuck.",
           "trigger": {"kind": "cron", "spec": "0 */3 * * 1-5", "tz": "\(TimeZone.current.identifier)"},
           "next_run_at": \(now.addingTimeInterval(3600 * 2).timeIntervalSince1970), "enabled": true,
           "runs": [{"ts": \(now.addingTimeInterval(-3600).timeIntervalSince1970), "ok": false, "detail": "no session"}]},
          {"id": "rt_prices", "agent": "travel scout", "prompt": "Re-check the Lisbon fares.",
           "trigger": {"kind": "cron", "spec": "0 10 * * 0,6", "tz": "\(TimeZone.current.identifier)"},
           "next_run_at": null, "enabled": false, "runs": []}
        ]}
        """
        var live = try DeckCoding.decoder.decode(RoutinesResponse.self, from: Data(json.utf8)).routines
        live.removeAll { deletedRoutines.contains($0.id) }
        return live
    }

    /// Answers in the deck's shape — including a `next_run_at` the client did
    /// not send — and writes nothing to disk.
    public func createRoutine(_ draft: RoutineDraft) async throws -> Routine {
        if let problem = draft.problem { throw DeckError.transport(problem) }
        return try Self.decodeRoutine(
            id: "rt_\(UUID().uuidString.prefix(6))", draft: draft,
            nextRun: now.addingTimeInterval(3600), enabled: draft.isEnabled
        )
    }

    public func setRoutine(id: String, enabled: Bool) async throws -> Routine {
        guard let existing = try await routines().first(where: { $0.id == id }) else {
            throw DeckError.http(404, reason: "unknown_routine")
        }
        return try Self.decodeRoutine(
            id: existing.id,
            draft: RoutineDraft(agentName: existing.agentName, prompt: existing.prompt,
                                spec: existing.spec, timezone: existing.timezone),
            nextRun: enabled ? now.addingTimeInterval(3600) : nil,
            enabled: enabled
        )
    }

    public func deleteRoutine(id: String) async throws {
        deletedRoutines.add(id)
    }

    private static func decodeRoutine(
        id: String, draft: RoutineDraft, nextRun: Date?, enabled: Bool
    ) throws -> Routine {
        let next = nextRun.map { String($0.timeIntervalSince1970) } ?? "null"
        let json = """
        {"id": "\(id)", "agent": "\(draft.agentName)", "prompt": "\(draft.prompt)",
         "trigger": {"kind": "cron", "spec": "\(draft.spec)", "tz": "\(draft.timezone)"},
         "next_run_at": \(next), "enabled": \(enabled), "runs": []}
        """
        return try DeckCoding.decoder.decode(Routine.self, from: Data(json.utf8))
    }

    // MARK: approvals

    /// Two waiting tool calls in §12's shape: one where a standing rule is on
    /// offer, and one where `always` is unavailable because the call is
    /// irreversible. Both shapes have to exist here or the card's hardest
    /// state is never seen without a live deck.
    public func approvals() async throws -> ApprovalsPage {
        let json = """
        {"approvals": [
          {"id": "apr_pr", "ts": \(now.addingTimeInterval(-240).timeIntervalSince1970),
           "agent": "chief", "tool": "Bash",
           "subject": "gh pr list --limit 20 --json number,title,author",
           "cwd": "/Users/you/Projects/acme", "cwd_short": "~/Projects/acme",
           "status": "pending",
           "options": [
             {"reply": "once", "available": true, "rule": null, "summary": "just this time"},
             {"reply": "always", "available": true,
              "summary": "allow `gh pr*` in ~/Projects/acme, never ask again",
              "rule": {"id": "ask-apr_pr", "kind": "always_allow", "tool": "Bash",
                       "pattern": "gh pr*", "cwd": "/Users/you/Projects/acme",
                       "note": "from chief on WhatsApp"}},
             {"reply": "never", "available": true,
              "summary": "refuse `gh pr*` in ~/Projects/acme from now on",
              "rule": {"id": "ask-apr_pr", "kind": "deny", "tool": "Bash",
                       "pattern": "gh pr*", "cwd": "/Users/you/Projects/acme",
                       "note": "from chief on WhatsApp"}}
           ]},
          {"id": "apr_write", "ts": \(now.addingTimeInterval(-90).timeIntervalSince1970),
           "agent": "hemingway", "tool": "Write",
           "subject": "Write ~/Projects/acme/README.md (2.1 KB)",
           "cwd": "/Users/you/Projects/acme", "cwd_short": "~/Projects/acme",
           "status": "pending",
           "options": [
             {"reply": "once", "available": true, "rule": null, "summary": "just this time"},
             {"reply": "always", "available": false, "rule": null,
              "summary": "not available here (credential, payment or irreversible)"},
             {"reply": "never", "available": true,
              "summary": "refuse `Write(README.md)` in ~/Projects/acme from now on",
              "rule": {"id": "ask-apr_write", "kind": "deny", "tool": "Write",
                       "pattern": "Write(README.md)", "cwd": "/Users/you/Projects/acme",
                       "note": "from hemingway on WhatsApp"}}
           ]}
        ]}
        """
        var page = try DeckCoding.decoder.decode(ApprovalsPage.self, from: Data(json.utf8))
        page.approvals.removeAll { answered.contains($0.id) }
        return page
    }

    @discardableResult
    public func decideApproval(
        id: String, option: ApprovalOption
    ) async throws -> ApprovalDecision {
        answered.add(id)
        // The fixture answers the way the deck does: `once` writes no rule,
        // the other two write the one that was on the button, and a refusal
        // restarts nobody (§12.1).
        return ApprovalDecision(
            answered: option.reply.rawValue,
            rule: option.rule,
            resumed: option.reply != .never
        )
    }

    // MARK: secure handoffs

    /// **The other half of the tray, and the half this fixture used to leave
    /// out entirely.**
    ///
    /// `DeckClient` defaults `handoffs()` to an empty page so a client built
    /// against an older deck degrades instead of crashing. This file inherited
    /// that default and nothing failed — so under `DECK_FIXTURE=1`, and on
    /// every demo build, the tray drew approvals and no handoffs, and every
    /// review of "what does the app look like" was of an app missing a feature
    /// it has.
    ///
    /// Two, because the two shapes read differently and both have to be
    /// reviewable with no deck running: a **login** where the desk is mid-job
    /// and the clock is running, and a **2FA code** where the ask nothing can
    /// attribute arrives with a raw session id in `agent` (`desk_known: false`)
    /// — which is what the live deck publishes today.
    ///
    /// Nothing here is a credential. `where` is a public sign-in page, and
    /// `evidence` is the desk's own words.
    public func handoffs() async throws -> HandoffsPage {
        let json = """
        {"handoffs": [
          {"id": "hnd_gh", "ts": \(ago(11).timeIntervalSince1970),
           "agent": "seeker", "asked_by": "seeker", "desk_known": true,
           "kind": "login",
           "needs": "Sign in to GitHub in the browser on this Mac, then come back and press I'm done.",
           "state": "Nine of the twelve source PRs are already fetched; the tenth returned 401 and the run is paused there. Nothing has been written and nothing is being charged.",
           "where": "https://github.com/login",
           "evidence": "gh: To get started with GitHub CLI, please run: gh auth login",
           "status": "waiting",
           "options": [
             {"reply": "done", "available": true,
              "summary": "I signed in — go back and check the step really worked"},
             {"reply": "skipped", "available": true,
              "summary": "This will not happen — drop that path and report what you could not finish"}
           ]},
          {"id": "hnd_2fa", "ts": \(ago(4).timeIntervalSince1970),
           "agent": "sid-4c19", "asked_by": "sid-4c19", "desk_known": false,
           "kind": "2fa",
           "needs": "Read the six-digit code from your phone and type it into the window that is waiting for it.",
           "state": "The booking is held but not paid. The hold expires in about twenty minutes, and after that the fare has to be searched again.",
           "where": "the Safari window on this Mac",
           "evidence": "Enter the code we sent to the number ending 41",
           "status": "waiting",
           "options": [
             {"reply": "done", "available": true,
              "summary": "I typed the code — go back and check it was accepted"},
             {"reply": "skipped", "available": true,
              "summary": "Let the hold lapse — abandon the booking and say what was left undone"}
           ]}
        ]}
        """
        var page = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data(json.utf8))
        page.handoffs.removeAll { resolvedHandoffs.contains($0.id) }
        return page
    }

    /// Answers the way the deck does and writes nothing. `resumed` is `true`
    /// for both verbs because both are a real instruction back into the
    /// session — `done` orders a re-check, `skipped` orders abandonment — which
    /// is the fact an approval cannot promise and this card is built around.
    @discardableResult
    public func resolveHandoff(
        id: String, outcome: HandoffOutcome
    ) async throws -> HandoffResolution {
        resolvedHandoffs.add(id)
        return HandoffResolution(outcome: "resolved", resumed: true)
    }

    public func events() -> AsyncThrowingStream<DeckEvent, Error> {
        AsyncThrowingStream { continuation in
            continuation.yield(.heartbeat)
            // A fixture deck has nothing further to say; the stream stays open
            // so the indicator reads Connected rather than Reconnecting.
        }
    }

    // MARK: canned transcripts

    /// Written as **one** run of records with one sequence, because that is
    /// what the deck serves: the same message — same id, same cursor, same
    /// `peer:` `thread_id` — comes back from the desk's thread, from the other
    /// desk's thread and from the pair's own thread (§6.2). A second copy per
    /// view is how a fixture teaches a client to double-count.
    private static func transcripts(threadID: String, now: Date) -> [Message] {
        func at(_ minutes: Double) -> Date { now.addingTimeInterval(-minutes * 60) }

        func message(
            _ id: String, _ order: Int, _ author: String, _ minutes: Double, _ text: String,
            attachments: [Attachment] = [], thread: String? = nil
        ) -> Message {
            Message(
                id: id,
                cursor: String(format: "%010d-%@", order, id),
                threadID: thread ?? threadID,
                author: author,
                role: author == DeckOwner.name ? .owner
                    : ["routine", "deck", "engineer"].contains(author) ? .system : .agent,
                sentAt: at(minutes),
                text: text,
                attachments: attachments
            )
        }

        let hemingwayPair = "peer:chief|hemingway"
        let seekerPair = "peer:chief|seeker"

        // The status sweep, in the shape the owner actually reads: he asks,
        // the desk says what it is checking, two dispatches go out, two sets
        // of counts come back, and the desk summarises. Each of these four
        // keeps its `peer:` id in every view it appears in.
        let toHemingway = message(
            "p1", 32, "chief", 18,
            "Launch note: is the second draft out, and did the last line go? One word each.",
            thread: hemingwayPair
        )
        let toSeeker = message(
            "s1", 33, "chief", 18,
            "Quick status: any new source, any contradiction, any retraction since this morning? One line.",
            thread: seekerPair
        )
        let fromSeeker = message(
            "s2", 34, "seeker", 17,
            "Since 07:20: 3 new sources, 1 contradiction (Halpern 2021), no retractions.",
            thread: seekerPair
        )
        let fromHemingway = message(
            "p2", 35, "hemingway", 16,
            "second draft: out. last line: cut. word count 186.",
            thread: hemingwayPair
        )

        switch threadID {
        case "direct:hemingway":
            return [
                message("h1", 10, DeckOwner.name, 40, "Have a look at the opening paragraph."),
                message("h2", 11, "hemingway", 38,
                        "Read it. The second sentence is doing two jobs — I split it."),
                message("h3", 12, "hemingway", 37,
                        "Rewrite is in ~/Projects/deck-app/README.md if you want the diff.",
                        attachments: [Attachment(kind: .file, value: "~/Projects/deck-app/README.md")]),
                message("h4", 13, "hemingway", 30,
                        "Reference I was working from: https://example.com/style-guide",
                        attachments: [
                            Attachment(kind: .link, value: "https://example.com/style-guide"),
                            Attachment(kind: .image, value: "/tmp/draft-spread.png"),
                        ]),
                message("h5", 14, DeckOwner.name, 12, "Much better. Ship it."),
                // Both ends inline the same pair, so from this side the arrows
                // point the other way, off the very same two records.
                toHemingway, fromHemingway,
            ]
        case hemingwayPair:
            return [toHemingway, fromHemingway]
        case seekerPair:
            return [toSeeker, fromSeeker]
        case "direct:chief":
            return [
                // K1 system lines: a routine fire and the deck's own notice.
                // Grey centred captions, never his bubbles.
                message("c0", 28, "routine", 24, "Morning briefing\nWhat moved yesterday, in five lines."),
                message("c0d", 29, "deck", 22, "[Agent Deck] Hired: seeker now reports to you."),
                message("c1", 30, DeckOwner.name, 20, "any updates?"),
                message("c2", 31, "chief", 19, "Checking the launch note and the sources."),
                toHemingway, toSeeker, fromSeeker, fromHemingway,
                message("c3", 36, "chief", 15,
                        "Draft is out with the last line cut. Seeker found one contradiction worth reading."),
                // K4: a question asked as buttons.
                {
                    var asked = message("c4", 37, "chief", 14, "Which headline goes on the Acme homepage?")
                    asked.kind = .decision
                    asked.decision = Decision(
                        id: "dec_fixture01", prompt: "Which headline goes on the Acme homepage?",
                        help: "Both drafts are in ~/Projects/acme/copy/headlines.md",
                        options: [
                            DecisionOption(label: "Homes that sell themselves", value: "Go with A: Homes that sell themselves", style: .primary),
                            DecisionOption(label: "Your home, fully booked", value: "Go with B: Your home, fully booked"),
                            DecisionOption(label: "Hold both", value: "Hold both headlines for now", style: .danger),
                        ],
                        allowCustom: true)
                    return asked
                }(),
            ]
        default:
            let name = threadID.hasPrefix("direct:")
                ? String(threadID.dropFirst("direct:".count)) : threadID
            return [message("\(name)-1", 1, name, 60, "Nothing new here yet.")]
        }
    }
}


// MARK: - K4 decisions and K6 calls

/// Accepted and dropped, as the deck would answer. A decision card tapped in
/// the fixture app flips locally; nothing here pretends to be a desk acting on it.
extension FixtureDeckClient: DecisionClient, CallClient {
    public func answerDecision(id: String, value: String) async throws {}

    public func startCall(agent: String) async throws -> CallStart {
        guard agents.contains(where: { $0.name == agent }) else { throw DeckError.unknownAgent }
        return CallStart(callID: "call_fixture", threadID: "direct:\(agent)",
                         voice: DeskVoice(id: "", rate: 1.0))
    }

    public func sendVoice(threadID: String, text: String, callID: String) async throws -> Message {
        var line = try await send(threadID: threadID, text: text)
        line.channel = .voice
        return line
    }

    public func endCall(id: String) async throws {}
}

// MARK: - "<Agent>'s screen" (§15)

/// A canned computer, so the screen panel can be built and reviewed with no
/// deck, no Docker and no container.
///
/// **No JPEG ships here.** A captured frame is a photograph of a signed-in
/// browser — his mail, his admin consoles, whatever the agent had open — and
/// one of those does not go into a repository, a fixture or a test. So the
/// fixture answers the *status* honestly and refuses the frame with the deck's
/// own `no_frame`, which is a state the panel already draws in words. The
/// picture is a thing you only ever see against a real machine.
extension FixtureDeckClient: AgentScreenClient {

    /// `chief` has a machine; everybody else does not. Both are states the
    /// panel must draw and neither is an error.
    public func screenStatus(agent: String) async throws -> AgentScreenStatus {
        guard agents.contains(where: { $0.name == agent }) else {
            throw ScreenRefusal.unknownAgent("no desk named '\(agent)'")
        }
        return AgentScreenStatus(
            desk: agent,
            isRunning: agent == "chief",
            image: "agent-deck/desk-computer:1",
            container: "deck-desk-\(agent)",
            display: ":99",
            width: 1280,
            height: 800,
            staleAfter: 30,
            generatedAt: Date())
    }

    public func screenFrame(agent: String) async throws -> AgentScreenFrame {
        let status = try await screenStatus(agent: agent)
        guard status.isRunning else {
            throw ScreenRefusal.noComputerYet("deck-desk-\(agent) is not up")
        }
        if demoScreenPage {
            return AgentScreenFrame(
                jpeg: FixtureScreenPage.jpeg(width: status.width, height: status.height),
                serverAge: 0.2, display: status.display, receivedAt: Date())
        }
        throw ScreenRefusal.noFrame(
            "this is the fixture — no picture of a real browser is kept in this app")
    }

    /// Accepted and dropped. The gesture is still checked against the display
    /// the status call reported, so a client bug in the transform fails here
    /// too rather than only against a live machine.
    public func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        let status = try await screenStatus(agent: agent)
        guard status.isRunning else {
            throw ScreenRefusal.noComputerYet("deck-desk-\(agent) is not up")
        }
        for point in Self.pointsIn(input) where
            !(0..<status.width).contains(point.x) || !(0..<status.height).contains(point.y) {
            // The deck's own words, measured on the box.
            throw ScreenRefusal.badInput(
                "click (\(point.x),\(point.y)) is outside the "
                + "\(status.width)x\(status.height) screen")
        }
    }

    /// Every coordinate a gesture carries — **including the far end of a drag**,
    /// which is a second chance for the transform to be wrong and the one the
    /// single-point check could not see.
    private static func pointsIn(_ input: ScreenInput) -> [DisplayPoint] {
        switch input {
        case .click(let x, let y, _, _), .move(let x, let y),
             .scroll(let x, let y, _, _):
            return [DisplayPoint(x: x, y: y)]
        case .drag(let x, let y, let toX, let toY, _):
            return [DisplayPoint(x: x, y: y), DisplayPoint(x: toX, y: toY)]
        case .type, .key:
            return []
        }
    }
}

/// A tiny locked set. `FixtureDeckClient` is a value type by design — the app
/// swaps it for `HTTPDeckClient` at one line — but an answered approval has to
/// stay answered across the copies SwiftUI makes.
final class AnsweredApprovals: @unchecked Sendable {
    private let lock = NSLock()
    private var ids: Set<String> = []

    func add(_ id: String) {
        lock.lock(); defer { lock.unlock() }
        ids.insert(id)
    }

    func contains(_ id: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return ids.contains(id)
    }
}

// MARK: - the usage meter

extension FixtureDeckClient: ClaudeUsageSource {
    /// A meter in the shape the deck sends: the 5-hour window first, the
    /// weekly one after, on the plan object's name.
    public func usage() async throws -> ClaudeUsage? {
        ClaudeUsage(available: true, plan: "Max 20×", windows: [
            ClaudeUsage.Window(key: "weekly_all", label: "Weekly", percent: 72,
                               resetsAt: now.addingTimeInterval(3 * 86_400)),
            ClaudeUsage.Window(key: "session", label: "5-hour window", percent: 38,
                               resetsAt: now.addingTimeInterval(2 * 3_600)),
        ])
    }
}

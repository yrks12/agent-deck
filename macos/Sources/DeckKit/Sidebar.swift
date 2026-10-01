import Foundation

/// What a row's face has to say before he opens it.
///
/// Two marks, not seven. He manages by exception, and a roster where every
/// desk is flagged is a roster where none is: `.waitingForYou` is orange and
/// means *nothing moves until he acts*, `.working` is green and means *this
/// one is busy, leave it*, and everything else draws nothing at all.
public enum RowAttention: String, Hashable, Sendable, CaseIterable {
    /// Named `quiet`, not `none`: `RowAttention.none` collides with
    /// `Optional.none` at every call site, and the first test written against
    /// it silently compared the attention to `nil` and passed the wrong thing.
    case quiet
    case working
    case waitingForYou

    /// Read to the screen reader, because a colour is not a status. `nil` for
    /// `.none` — there is nothing to say about a desk that is simply idle.
    public var spokenWord: String? {
        switch self {
        case .quiet: return nil
        case .working: return "working"
        case .waitingForYou: return "waiting for you"
        }
    }
}

public struct SidebarRow: Identifiable, Hashable, Sendable {
    public var agent: Agent
    /// The thread the row opens: the agent's most recent one.
    public var threadID: String?
    /// Every thread this agent has, newest first — including read-only peer
    /// threads, which the thread view lets you switch to.
    public var threads: [ThreadSummary]
    public var preview: ThreadPreview
    public var timestamp: Date?
    public var unreadCount: Int

    // Everything below is *derived here, once*, because a row is built once per
    // roster change and drawn every time anything on the deck publishes. Each
    // of these was being worked out inside the row's `body` instead: an FNV-1a
    // hash over the name (three times a pass), two trips through ICU, and a
    // `stat` on this Mac's filesystem. Measured at 60 hashes and 40 date
    // formats for a single draw of twenty rows — see `SidebarBodyWorkTests`.

    /// The face: shape, tint and initials, keyed on the one field that cannot
    /// be edited.
    public var look: AvatarLook
    /// "now" / "4m" / "14:32" / "Yesterday" / "3 Feb", as the row shows it.
    /// Fixed at the moment the roster was built — "4m" is not a live clock and
    /// never was, it just used to be recomputed for no benefit.
    ///
    /// **Never empty.** The stamp used to appear only on rows whose agent had a
    /// thread, so the right-hand column was ragged and a quiet desk read as a
    /// rendering failure. A desk nobody has ever heard from gets a mark, not a
    /// gap.
    public var timestampLabel: String
    /// Green, orange or nothing — the one thing he can read across the whole
    /// roster at a glance.
    public var attention: RowAttention
    /// The second line. A stuck desk says what it is waiting for; every other
    /// desk says the last thing that happened in its conversation.
    public var previewText: String
    /// The faces on a row whose newest thread has more than one desk in it.
    /// Empty for an ordinary 1:1, which draws its single avatar as before.
    public var participantLooks: [AvatarLook]
    /// The ones that did not fit — drawn as "+2", never dropped.
    public var extraParticipantCount: Int
    /// The full form, for VoiceOver. "4m" is no use read aloud.
    public var spokenTimestampLabel: String?
    /// The deck serves no images, so an avatar is drawable only when its
    /// opaque string names a file this Mac can reach — which is a filesystem
    /// call, and not one to make while laying out a list.
    public var localAvatarURL: URL?

    public var id: String { agent.name }
    public var isUnread: Bool { unreadCount > 0 }

    /// The derived fields default to being derived, so a row built by hand is
    /// never inconsistent with one built by `SidebarSnapshot.build`.
    public init(
        agent: Agent,
        threadID: String?,
        threads: [ThreadSummary],
        preview: ThreadPreview,
        timestamp: Date?,
        unreadCount: Int,
        now: Date = Date()
    ) {
        self.agent = agent
        self.threadID = threadID
        self.threads = threads
        self.preview = preview
        self.timestamp = timestamp
        self.unreadCount = unreadCount
        self.look = agent.look
        self.timestampLabel = timestamp.map { SidebarTimestamp.short($0, now: now) }
            ?? SidebarTimestamp.never
        self.spokenTimestampLabel = timestamp.map { SidebarTimestamp.spoken($0) }
        self.localAvatarURL = agent.localAvatarURL
        self.attention = RowAttention(for: agent)
        self.previewText = SidebarRow.previewText(for: agent, preview: preview)

        // A row draws the desks in its newest conversation, not the owner: he
        // is in every direct thread and counting him would make every 1:1 look
        // like a crowd.
        let others = (threads.first?.participants ?? []).filter { $0 != DeckOwner.name }
        if others.count > 1 {
            self.participantLooks = others.prefix(2).map(AvatarLook.forName)
            self.extraParticipantCount = max(0, others.count - 2)
        } else {
            self.participantLooks = []
            self.extraParticipantCount = 0
        }
    }

    /// A stuck desk's own preview is usually its last message, which is true
    /// and useless: it reads exactly like a desk that is fine. The deck's
    /// `what` is the short, actionable half of `blocked` and is what goes here;
    /// the full sentence stays in the panel. `what` is optional on the wire, so
    /// a blocked desk with nothing said about it still says it is blocked
    /// rather than falling back to the message that hid the problem.
    static func previewText(for agent: Agent, preview: ThreadPreview) -> String {
        guard let blocked = agent.blocked else { return preview.line }
        let what = blocked.what.trimmingCharacters(in: .whitespacesAndNewlines)
        return "Waiting for you: " + (what.isEmpty ? Blocked.badgeText : what)
    }
}

/// The dot at the right edge of a row.
public enum RowDot: Hashable, Sendable {
    case none
    /// Something to read.
    case unread
    /// Nothing moves until he acts. Orange, and shown even when read.
    case waiting
}

extension SidebarRow {
    public var dot: RowDot {
        if attention == .waitingForYou { return .waiting }
        return isUnread ? .unread : .none
    }
}

extension RowAttention {
    /// **Never gated on `state`.** `client-api.md` §3.1 is explicit that
    /// `blocked` also arrives on a desk that is seated and running
    /// (`dialog_unrelayed`), and a client that only looks at `state` drops the
    /// exact stall he most needs to see.
    public init(for agent: Agent) {
        if agent.blocked != nil || agent.state == .needsYou {
            self = .waitingForYou
        } else if agent.state == .working {
            self = .working
        } else {
            // DONE, IDLE, SHELL, DEAD and OFFLINE: nothing is moving and
            // nothing is stuck. The unread badge already says whether there is
            // something to read, and the face already dims for OFFLINE.
            self = .quiet
        }
    }
}

/// How a row says when it last heard from a desk.
///
/// In DeckKit rather than in the view's `Theme`, because `Date.formatted` is a
/// trip through ICU and a row is drawn far more often than the roster changes.
/// Deciding it here means it is decided once per change instead.
public enum SidebarTimestamp {

    /// **How long ago comes before which day.**
    ///
    /// This used to ask `isDateInToday` first, so a desk that had spoken
    /// sixteen minutes ago read "Yesterday" for the whole first hour after
    /// midnight — the exact stamp he would take for a stalled desk, on every
    /// desk on the board, every night. FOUND at 00:15 on 2026-09-07 by
    /// `SidebarRowSpeaksTests`, which had been green all day.
    ///
    /// The calendar-day question is still asked, and still answers "Yesterday"
    /// for something that really was yesterday — it is asked second, after the
    /// clock has had its say.
    public static func short(_ date: Date, now: Date = Date()) -> String {
        let calendar = Calendar.current
        let minutes = Int(now.timeIntervalSince(date) / 60)
        if minutes >= 0 && minutes < 60 {
            return minutes < 1 ? "now" : "\(minutes)m"
        }
        if calendar.isDate(date, inSameDayAs: now) {
            return date.formatted(date: .omitted, time: .shortened)
        }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(date, inSameDayAs: yesterday) {
            return "Yesterday"
        }
        return date.formatted(.dateTime.day().month(.abbreviated))
    }

    public static func spoken(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }

    /// For a desk the deck has never heard from. A mark rather than a gap:
    /// an empty cell in the stamp column reads as a row that failed to draw.
    public static let never = "—"
}

public struct SidebarSection: Identifiable, Hashable, Sendable {
    public var name: String
    public var rows: [SidebarRow]
    /// Unread sitting on rows that were cut off below the fold.
    public var overflowUnreadCount: Int

    public var id: String { name }
    public var hasOverflow: Bool { overflowUnreadCount > 0 }
    public var overflowLabel: String { "+ \(overflowUnreadCount) more unreads" }

    public init(name: String, rows: [SidebarRow], overflowUnreadCount: Int) {
        self.name = name
        self.rows = rows
        self.overflowUnreadCount = overflowUnreadCount
    }
}

/// The entire sidebar, derived from one roster payload.
///
/// `build` takes no client and makes no call. That is the guarantee: sorting,
/// unread counts, previews and timestamps all come out of a single pass over
/// what the deck already sent, so a roster of five hundred desks costs the same
/// one request as a roster of five.
public struct SidebarSnapshot: Hashable, Sendable {
    public var pinned: [SidebarRow]
    public var sections: [SidebarSection]
    public var totalUnread: Int
    /// The one desk he talks to, in its own block above the roster rather than
    /// mixed into the list of the desks it hires. `nil` when the org chart on
    /// this roster does not name one — see `chiefName`.
    public var chief: SidebarRow?

    public init(
        pinned: [SidebarRow],
        sections: [SidebarSection],
        totalUnread: Int,
        chief: SidebarRow? = nil
    ) {
        self.pinned = pinned
        self.sections = sections
        self.totalUnread = totalUnread
        self.chief = chief
    }

    public static let empty = SidebarSnapshot(pinned: [], sections: [], totalUnread: 0)

    /// Every desk on this sidebar, the chief included. Pinning it above the
    /// roster took it out of `sections`, and anything that *looks a desk up* —
    /// selection, search — has to keep finding it or the one desk he talks to
    /// becomes the one desk he cannot click.
    public var allRows: [SidebarRow] {
        (chief.map { [$0] } ?? []) + sections.flatMap(\.rows)
    }

    /// **Who he actually talks to — asked of the roster, never guessed.**
    ///
    /// The chief is the desk that reports to nobody but him *and* has desks
    /// under it. Both halves matter: without the first it is any desk, and
    /// without the second a lone new hire with no boss recorded would be
    /// promoted into the seat.
    ///
    /// Two desks answering to that is an org chart this client cannot read, and
    /// picking one would put a stranger in the seat he talks to. It returns
    /// `nil` and every desk stays in the list, which is the honest failure.
    public static func chiefName(among agents: [Agent]) -> String? {
        let candidates = agents.filter { agent in
            let boss = agent.boss?.trimmingCharacters(in: .whitespacesAndNewlines)
            guard boss == nil || boss!.isEmpty || boss == DeckOwner.name else { return false }
            return !agent.reports.isEmpty || agents.contains { $0.boss == agent.name }
        }
        return candidates.count == 1 ? candidates[0].name : nil
    }

    public static func build(
        from payload: RosterPayload,
        visibleRowsPerSection: Int = .max,
        now: Date = Date()
    ) -> SidebarSnapshot {
        // Pass one: bucket threads by the agent whose row they belong on.
        var byAgent: [String: [ThreadSummary]] = [:]
        byAgent.reserveCapacity(payload.agents.count)
        for thread in payload.threads {
            byAgent[thread.agentName, default: []].append(thread)
        }

        // Pass two: one row per agent, everything folded in as we go.
        var rowsBySection: [String: [SidebarRow]] = [:]
        var sectionOrder = payload.sectionOrder
        var pinned: [SidebarRow] = []
        var totalUnread = 0
        let chiefName = chiefName(among: payload.agents)
        var chief: SidebarRow?

        for agent in payload.agents {
            let threads = (byAgent[agent.name] ?? []).sorted {
                ($0.lastActivity ?? .distantPast) > ($1.lastActivity ?? .distantPast)
            }
            let newest = threads.first
            // Unread is per *agent* on this deck — one read cursor covers its
            // 1:1 and its peer threads together — so this is a max, not a sum.
            let unread = threads.map(\.unreadCount).max() ?? 0
            totalUnread += unread

            let row = SidebarRow(
                agent: agent,
                threadID: newest?.id,
                threads: threads,
                preview: newest?.preview ?? ThreadPreview(text: "No messages yet"),
                // A desk with no thread yet still has a last-seen time on the
                // roster. Using it is what puts a stamp on every row.
                timestamp: newest?.lastActivity ?? agent.lastActivityAt,
                unreadCount: unread,
                now: now
            )
            if agent.name == chiefName {
                // Above the roster, and *not* also in it: he would see the same
                // desk twice and one of them would scroll away.
                chief = row
                continue
            }
            if agent.isPinned { pinned.append(row) }
            if rowsBySection[agent.section] == nil {
                rowsBySection[agent.section] = []
                if !sectionOrder.contains(agent.section) { sectionOrder.append(agent.section) }
            }
            rowsBySection[agent.section]?.append(row)
        }

        // Pass three: order each section and roll up what falls below the fold.
        let sections: [SidebarSection] = sectionOrder.compactMap { name in
            guard var rows = rowsBySection[name] else { return nil }
            // Newest first; an agent with no traffic sorts last rather than
            // vanishing, and ties break on name so the order never jitters.
            rows.sort { left, right in
                switch (left.timestamp, right.timestamp) {
                case (let l?, let r?) where l != r: return l > r
                case (nil, .some): return false
                case (.some, nil): return true
                default: return left.agent.name < right.agent.name
                }
            }
            guard rows.count > visibleRowsPerSection else {
                return SidebarSection(name: name, rows: rows, overflowUnreadCount: 0)
            }
            let visible = Array(rows.prefix(visibleRowsPerSection))
            let hidden = rows.dropFirst(visibleRowsPerSection)
            return SidebarSection(
                name: name,
                rows: visible,
                overflowUnreadCount: hidden.reduce(0) { $0 + $1.unreadCount }
            )
        }

        return SidebarSnapshot(
            pinned: pinned, sections: sections, totalUnread: totalUnread, chief: chief)
    }
}

extension RosterPayload: Equatable {
    /// Field-wise, because "is this the roster we are already holding?" is the
    /// question every live frame has to answer before it is allowed to cost
    /// anything. Every field is already `Hashable`; nothing here is derived.
    public static func == (left: RosterPayload, right: RosterPayload) -> Bool {
        left.agents == right.agents
            && left.threads == right.threads
            && left.sectionOrder == right.sectionOrder
    }
}

/// Holds the roster and rebuilds the sidebar from it. One `roster()` call per
/// refresh — never a follow-up per agent.
public actor RosterModel {
    private let client: DeckClient
    private var visibleRowsPerSection: Int

    public private(set) var snapshot: SidebarSnapshot = .empty
    public private(set) var payload = RosterPayload(agents: [], threads: [], sectionOrder: [])
    public private(set) var lastError: DeckError?

    public init(client: DeckClient, visibleRowsPerSection: Int = .max) {
        self.client = client
        self.visibleRowsPerSection = visibleRowsPerSection
    }

    public func load() async throws {
        commit(try await client.roster())
    }

    /// Applies a live unread count without going back to the network. The deck
    /// keeps one read cursor per agent, so every one of its threads moves.
    ///
    /// This used to raise a `touched` flag on the *assignment* rather than on
    /// the assignment changing anything, so it never once said no: a frame
    /// restating the count already on the badge rebuilt the whole sidebar.
    /// `commit` is what says no now.
    public func applyUnread(agent: String, count: Int) {
        var next = payload
        for position in next.threads.indices where next.threads[position].agentName == agent {
            next.threads[position].unreadCount = count
        }
        if let position = next.agents.firstIndex(where: { $0.name == agent }) {
            next.agents[position].unread = count
        }
        commit(next)
    }

    /// Folds a live `agent_state` frame into the roster with no refetch — the
    /// point of the stream (§8). `blocked` defaults to `.unspecified` so every
    /// call site that predates this field (and any frame class that never
    /// carries it) behaves exactly as before: state moves, `blocked` is left
    /// untouched, never silently cleared.
    ///
    /// `.cleared` (an explicit `blocked: null` on the wire) *does* clear
    /// whatever this desk already had — that is the recovery signal, and a
    /// badge that only ever appears and never retracts is the same lie in the
    /// other direction.
    public func applyAgentState(name: String, state: AgentState, blocked: BlockedUpdate = .unspecified) {
        guard let position = payload.agents.firstIndex(where: { $0.name == name }) else { return }
        var next = payload
        next.agents[position].state = state
        switch blocked {
        case .unspecified:
            break
        case .cleared:
            next.agents[position].blocked = nil
        case .value(let info):
            next.agents[position].blocked = info
        }
        commit(next)
    }

    /// **Who spoke last, off the newest message rather than off a frame that
    /// cannot carry it.**
    ///
    /// `client-api.md` §3.0 is explicit that `last_activity_by` is *not* on the
    /// `agent_state` frame — that frame is `{type, ts, name, state, blocked}` —
    /// and just as explicit about what to do instead: "re-read
    /// `GET /v1/agents`, or take it from the newest message in the thread you
    /// already have open". This is the second one, and it is the cheap one.
    ///
    /// Without it the worst reading available survives on screen: a desk that
    /// has *just answered him* keeps saying "you spoke last and it has not come
    /// back yet", because the answer arrived on a frame the roster ignored and
    /// the state change arrived on one that carries no such field.
    /// `false` when nothing moved, so a caller on the message path can skip a
    /// republish rather than rebuilding the sidebar once per line of a
    /// conversation. Every line of a desk answering him would otherwise be a
    /// full pass over the roster for a value that did not change.
    @discardableResult
    public func applyLastSpeaker(agent: String, _ speaker: LastSpeaker) -> Bool {
        guard speaker != .unknown,
              let position = payload.agents.firstIndex(where: { $0.name == agent }),
              payload.agents[position].lastSpeaker != speaker
        else { return false }
        var next = payload
        next.agents[position].lastSpeaker = speaker
        commit(next)
        return true
    }

    public func replace(with payload: RosterPayload) {
        commit(payload)
    }

    public func threads(for agent: String) -> [ThreadSummary] {
        payload.threads.filter { $0.agentName == agent }
    }

    /// Puts a newly hired desk on the roster without a refetch: the 201 carries
    /// exactly one element of the `agents` array, and its direct thread is
    /// derived the same way every other row's is.
    public func insert(_ agent: Agent) {
        guard !payload.agents.contains(where: { $0.name == agent.name }) else {
            applyAgentUpdate(agent)
            return
        }
        var next = payload
        next.agents.append(agent)
        next.threads.append(
            ThreadSummary(
                id: agent.threadID,
                kind: .direct,
                title: agent.name,
                agentName: agent.name,
                participants: [DeckOwner.name, agent.name],
                isReadOnly: false,
                unreadCount: agent.unread,
                lastActivity: agent.lastActivityAt,
                preview: ThreadPreview(text: agent.preview)
            )
        )
        if !next.sectionOrder.contains(agent.section) {
            next.sectionOrder.append(agent.section)
        }
        commit(next)
    }

    /// A desk that renamed itself. The same row, in the same position, under a
    /// new key — not a second row beside the placeholder.
    ///
    /// Every thread id is keyed on the name (§6), so the direct thread is
    /// re-keyed with it and its unread count, preview and timestamp are carried
    /// across: none of that changed, only what the desk is called.
    public func rename(_ oldName: String, to agent: Agent) {
        guard let position = payload.agents.firstIndex(where: { $0.name == oldName }) else {
            return
        }
        var next = payload
        next.agents[position] = agent
        next.threads = next.threads.map { thread in
            guard thread.agentName == oldName, thread.kind == .direct else { return thread }
            return ThreadSummary(
                id: agent.threadID,
                kind: .direct,
                title: thread.title,
                agentName: agent.name,
                participants: [DeckOwner.name, agent.name],
                isReadOnly: thread.isReadOnly,
                unreadCount: thread.unreadCount,
                lastActivity: thread.lastActivity,
                preview: thread.preview
            )
        }
        if !next.sectionOrder.contains(agent.section) {
            next.sectionOrder.append(agent.section)
        }
        commit(next)
    }

    public func applyAgentUpdate(_ agent: Agent) {
        guard let position = payload.agents.firstIndex(where: { $0.name == agent.name }) else {
            return
        }
        var next = payload
        next.agents[position] = agent
        commit(next)
    }

    public func markRead(agent: String, upTo: String? = nil) async {
        try? await client.markRead(agent: agent, upTo: upTo)
        applyUnread(agent: agent, count: 0)
    }

    public func agent(named name: String) -> Agent? {
        payload.agents.first { $0.name == name }
    }

    public func agentsByName() -> [String: Agent] {
        Dictionary(payload.agents.map { ($0.name, $0) }, uniquingKeysWith: { first, _ in first })
    }

    /// **The only place the roster is allowed to move.**
    ///
    /// The deck's stream repeats itself: `agent_state` restating the state a
    /// desk is already in, an unread count restating what is already on the
    /// badge, a `hello` after every reconnect carrying the roster already held,
    /// a refetch that found nothing. Every one of those cost a full rebuild and
    /// a fresh snapshot pushed at a `List` — the view tree reconstructed for
    /// news that was not news, which is what `_swift_getGenericMetadata` at the
    /// top of a live sample means. Comparing the payload is linear; rebuilding
    /// it is not, and the rebuild is followed by SwiftUI re-measuring the list.
    ///
    /// Sweeping the whole class matters more than the frames that were caught
    /// first: every mutator on this actor now goes through here, so a new one
    /// cannot reintroduce the fault by forgetting a guard of its own.
    private func commit(_ next: RosterPayload) {
        guard next != payload else { return }
        payload = next
        rebuild()
    }

    /// One full pass over the roster: every thread bucketed, every row
    /// allocated, every section sorted. Counted because it is the sidebar's
    /// unit of wasted work — see `SidebarChurnTests`. Free unless
    /// `DECK_DIAGNOSE=1`.
    private func rebuild() {
        Diagnostics.count("sidebar.rebuild")
        snapshot = SidebarSnapshot.build(
            from: payload, visibleRowsPerSection: visibleRowsPerSection
        )
    }
}

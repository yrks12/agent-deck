import Foundation

/// **What is happening right now, said on the conversation itself.**
///
/// Measured live, 2026-09-03: he sent a message and the screen did not move.
/// Nothing told him the desk had taken it, was thinking, had stopped to ask him
/// something, or had finished. He had to go and look somewhere else to find
/// out, and when he could not, he concluded the app was broken.
///
/// Every fact this needs was already in the app and none of it was on that
/// screen: `Agent.state` and `Agent.blocked` off `GET /v1/agents` and the live
/// `agent_state` frame, the cards from `GET /v1/approvals`, and the stream's
/// own `ConnectionState`. This is the one place that turns them into a sentence,
/// so the thread pane and the sidebar cannot end up describing the same desk
/// two different ways.
///
/// It follows the `Blocked` / `FailurePresentation` shape deliberately: a short
/// headline to read at a glance, a sentence underneath when there is something
/// to *do*, and — where the fault is one `Blocked` already writes prose for —
/// that exact prose rather than a second wording of it.
public struct DeskStatus: Equatable, Sendable {

    /// Six conditions, because six is how many he has to be able to tell apart.
    /// Not a severity scale: `.offline` is not "worse" than `.waitingOnYou`,
    /// it is a different thing to do something about.
    public enum Tone: Equatable, Sendable {
        /// Something is running. Includes his own send, in flight.
        case working
        /// Stopped, and only he can move it: a dialog, an approval, NEEDS_YOU.
        case waitingOnYou
        /// Nothing is running and nothing is wrong.
        case quiet
        /// No session at this desk.
        case offline
        /// **This app** is not attached to the deck. Whatever is on screen may
        /// already be out of date.
        case disconnected
    }

    public var tone: Tone
    /// Two or three words. The thing he reads without stopping.
    public var headline: String
    /// The sentence under it, when there is something to do or to understand.
    /// `nil` when the headline is the whole truth — an invented second line
    /// would just be noise to read past next time.
    public var detail: String?
    /// An SF Symbol. **Always a still picture, never a spinner.**
    ///
    /// This was `String?`, where `nil` meant "the honest drawing is motion, so
    /// animate". It shipped, and the app went to 99.3% CPU and 2.4 GB on the
    /// owner's Mac inside two minutes. An indeterminate progress indicator is
    /// an AppKit animation, and this strip is on screen *permanently* — so it
    /// drove the window's display cycle at the refresh rate, and because the
    /// strip sits in the stack that sizes the transcript's `ScrollView`, every
    /// one of those frames re-measured every row of the conversation.
    ///
    /// A word he can read is the signal. The motion was never the signal, and
    /// it cost him the product. See `LayoutSettlesTests`.
    public var symbol: String
    /// **Whether the conversation draws this at all.** A quiet desk says
    /// nothing (the "Idle — its session is up" footer is gone) and a working
    /// one is the character under its last line. What stays is what he acts
    /// on, what is broken, and a desk that is resting.
    public var showsOnConversation: Bool

    public init(
        tone: Tone, headline: String, detail: String? = nil, symbol: String,
        showsOnConversation: Bool? = nil
    ) {
        self.tone = tone
        self.headline = headline
        self.detail = detail
        self.symbol = symbol
        self.showsOnConversation = showsOnConversation
            ?? [.waitingOnYou, .offline, .disconnected].contains(tone)
    }

    /// One sentence for VoiceOver — the same rule `Message.spokenLabel` and
    /// `FailureView` follow, so a headline is never heard stranded from the
    /// line that explains it.
    public var spokenLabel: String {
        guard let detail, !detail.isEmpty else { return headline }
        return "\(headline). \(detail)"
    }

    /// `nil` means **say nothing**: a healthy stream on a thread with no desk
    /// behind it — a `peer:` transcript — has no state to report, and printing
    /// one would be putting a claim on screen that nothing made.
    ///
    /// Order matters and is the whole design:
    ///
    /// 1. **The connection first.** A state the deck sent before the socket
    ///    dropped is not news, and drawing it as current is exactly the lie
    ///    that made him think a working system was broken.
    /// 2. **His own send.** The gap between pressing Return and the desk waking
    ///    up is where he decided nothing was happening.
    /// 3. **A question waiting on him**, from either place the deck raises one:
    ///    an approval card, or `blocked` on the desk itself.
    /// 4. **Whatever the desk says it is doing.**
    public static func make(
        agent: Agent?,
        connection: ConnectionState,
        approvals: [ApprovalCard],
        isSending: Bool
    ) -> DeskStatus? {
        if let interrupted = disconnected(connection) { return interrupted }

        if isSending {
            return DeskStatus(tone: .working, headline: "Sending…",
                              symbol: "paperplane.fill")
        }

        if let first = approvals.first {
            return DeskStatus(
                tone: .waitingOnYou,
                headline: "Waiting on you",
                detail: approvals.count == 1
                    ? first.title
                    : "\(approvals.count) requests are waiting for an answer.",
                symbol: "hand.raised.fill"
            )
        }

        guard let agent else { return nil }

        // Deliberately not gated on `state` — see `Blocked`'s doc comment and
        // §3.1. A desk can be frozen on a dialog while its state still reads
        // WORKING, and that is the one stall most worth surfacing.
        if let blocked = agent.blocked {
            return DeskStatus(
                tone: .waitingOnYou,
                headline: "Stopped — needs you",
                detail: blocked.sentence,
                symbol: "exclamationmark.triangle.fill"
            )
        }

        return forState(agent.state, lastSpeaker: agent.lastSpeaker)
    }

    /// **The three things `IDLE` means**, §3.0's table in the app's own words.
    ///
    /// Every one of them is `.quiet`: none is a fault and none needs him at the
    /// keyboard. What differs is what he would do next, which is the only
    /// reason to draw three sentences instead of one — go and read the answer,
    /// leave it alone, or say something to it.
    private static func idle(_ lastSpeaker: LastSpeaker) -> DeskStatus {
        switch lastSpeaker {
        case .agent:
            // §3.0, verbatim: "It answered you and is waiting. Say that, not
            // 'nothing is running'."
            return DeskStatus(
                tone: .quiet,
                headline: "Answered you",
                detail: "It came back and is waiting. There is something to read.",
                symbol: "text.bubble"
            )
        case .owner:
            return DeskStatus(
                tone: .quiet,
                headline: "Idle",
                detail: "You spoke last and it has not come back yet.",
                symbol: "moon.zzz"
            )
        case .nobody:
            return DeskStatus(
                tone: .quiet,
                headline: "Never started",
                detail: "Its session is up and nobody has ever spoken to it.",
                symbol: "moon.zzz"
            )
        case .unknown:
            // The deck said nothing about who spoke last — an older one, or a
            // row that never carried the field. Claim nothing beyond the state.
            return DeskStatus(
                tone: .quiet,
                headline: "Idle",
                detail: "Its session is up and nothing is running.",
                symbol: "moon.zzz"
            )
        }
    }

    private static func disconnected(_ connection: ConnectionState) -> DeskStatus? {
        switch connection {
        case .live:
            return nil
        case .connecting:
            return DeskStatus(
                tone: .disconnected,
                headline: "Connecting…",
                detail: "Reaching the deck. Nothing new can arrive until it answers.",
                symbol: "antenna.radiowaves.left.and.right"
            )
        case .reconnecting:
            return DeskStatus(
                tone: .disconnected,
                headline: "Reconnecting…",
                detail: "The deck's live stream dropped. What you can see may already be out of date.",
                symbol: "antenna.radiowaves.left.and.right.slash"
            )
        case .idle:
            return DeskStatus(
                tone: .disconnected,
                headline: "Not connected",
                detail: "This window is not attached to the deck, so nothing here is live.",
                symbol: "bolt.horizontal.circle"
            )
        }
    }

    /// One sentence per state the wire can send. Every one of them is distinct,
    /// because two states that read the same are two states he cannot act on
    /// differently — `DeskStatusTests` sweeps `AgentState.allCases` for exactly
    /// that.
    ///
    /// `IDLE` is the one that needs a second field, and `client-api.md` §3.0 is
    /// a whole section about why: `state` is the **session's** disposition,
    /// computed from what the process is doing, and it knows nothing about the
    /// conversation. So one `IDLE` covers a desk that has answered him, a desk
    /// still working through what he asked, and a desk nobody has ever spoken
    /// to — and he manages by exception, which he cannot do on a word that
    /// means three things. See `IdleIsThreeDifferentThingsTests`.
    private static func forState(
        _ state: AgentState, lastSpeaker: LastSpeaker = .unknown
    ) -> DeskStatus {
        switch state {
        case .working:
            return DeskStatus(tone: .working, headline: "Working…",
                              symbol: "gearshape.2.fill")
        case .needsYou:
            return DeskStatus(
                tone: .waitingOnYou,
                headline: "Waiting on you",
                detail: "It has stopped and is asking for you.",
                symbol: "hand.raised.fill"
            )
        case .done:
            return DeskStatus(
                tone: .quiet,
                headline: "Done",
                detail: "It finished what it was doing and has gone quiet.",
                symbol: "checkmark.circle"
            )
        case .idle:
            return idle(lastSpeaker)
        case .shell:
            return DeskStatus(
                tone: .quiet,
                headline: "At a shell",
                detail: "Its window is at a shell prompt, so it is not reading messages.",
                symbol: "terminal"
            )
        case .dead:
            return DeskStatus(
                tone: .offline,
                headline: "Session ended",
                detail: "Its session stopped. Start it again to talk to it.",
                symbol: "xmark.circle"
            )
        case .offline:
            return DeskStatus(
                tone: .offline,
                headline: "Offline",
                detail: "There is no session at this desk right now.",
                symbol: "powerplug"
            )
        case .asleep:
            // K3: the next message wakes it. Never "nothing will read this" —
            // that was false, and it frightened him.
            return DeskStatus(
                tone: .quiet,
                headline: "Asleep — it wakes when you message it.",
                symbol: "moon.zzz",
                showsOnConversation: true
            )
        }
    }
}

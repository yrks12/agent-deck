import Foundation

/// **Whether anything actually delivers a notification, and the one sentence
/// the app is allowed to say about it.**
///
/// The inspector used to carry this, spelled out in the view:
///
/// > Saved on the deck, but nothing delivers notifications yet — there is no
/// > push service.
///
/// True the day it was written. It stops being true the moment a push service
/// lands, and then the app is telling him — under a switch called *"Get
/// notified when this Bot finishes or needs input"* — that nothing will reach
/// him, while something does. That is not a stale string: it is the app being
/// wrong about the one question he asked, which is whether he finds out when an
/// agent is stuck.
///
/// So the sentence is derived from a fact rather than typed next to a switch.
/// **`channels` is the fact.** Empty means nothing on this build delivers
/// anything, and every sentence here says so. Give it a channel and every
/// sentence changes with it — there is nowhere else to edit, and no second copy
/// to forget.
public struct NotificationDelivery: Hashable, Sendable {

    /// What can really deliver one, named the way he would say it — "your
    /// phone", not "APNs". A channel appears here only when something in this
    /// app or on the deck actually sends to it; a channel that is planned,
    /// half-wired or configured-but-unsent is not one.
    public let channels: [String]

    public init(channels: [String]) {
        self.channels = channels
    }

    /// **What this build can deliver, today.**
    ///
    /// It used to be empty, and honestly so: the deck recorded the per-agent
    /// flag and nothing on the machine read it. That is no longer true. The
    /// deck now pushes per desk — a desk that **starts** a job, **says**
    /// something mid-run, goes **blocked** or **finishes** buzzes his phone
    /// over WhatsApp; the volume is capped by a per-desk and a global gap; and
    /// the switch in this panel is what silences one, dropped at offer time so
    /// unmuting releases the next thing that desk says rather than an hour of
    /// backlog.
    ///
    /// **This is a fact about the deck, asserted at build time, and that is the
    /// weak part of it.** The app can be pointed at any deck with `DECK_URL`,
    /// and a deck without that push would make this sentence wrong again. The
    /// honest fix is for `GET /v1/agents` (or a capability route) to say what
    /// it delivers, and for this value to be read off it. Until then there is
    /// one place to change, which is the whole reason this type exists.
    public static let current = NotificationDelivery(channels: ["WhatsApp on your phone"])

    public var deliversAnything: Bool { !channels.isEmpty }

    /// The line drawn under the switch. It is a caveat while nothing delivers
    /// and a statement of where things go once something does — never a
    /// promise, in either case.
    public var caveat: String {
        guard deliversAnything else {
            return "Saved on the deck, but nothing delivers notifications yet — there is no push service."
        }
        return "Delivered to \(list) when this desk starts a job, reports, "
            + "gets blocked or finishes. Off, and it tells you none of them."
    }

    /// "your phone", "your phone and Slack", "your phone, Slack and email" —
    /// the English list, because this is read as a sentence.
    private var list: String {
        switch channels.count {
        case 0: return ""
        case 1: return channels[0]
        default:
            return channels.dropLast().joined(separator: ", ") + " and " + channels[channels.count - 1]
        }
    }
}

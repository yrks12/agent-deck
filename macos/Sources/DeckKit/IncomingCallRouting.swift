import Foundation

/// **How often the phone asks the deck what is new** (`GET /v1/owner/alerts`).
/// A desk's ring lasts 30 s and the phone has no push of its own, so while he
/// is in the app it asks every 3 s; kept alive behind the lock screen (a call
/// holds the audio) it keeps the old 12 s pace.
public enum OwnerAlertCadence {
    public static let active: TimeInterval = 3
    public static let background: TimeInterval = 12

    public static func interval(active: Bool) -> TimeInterval { active ? Self.active : background }
}

/// **The phone's incoming call: which ring is up, and where a tap goes.**
/// PURE. Wraps `IncomingCallPresenter` (the oldest ring he has not handled)
/// with the one ring he tapped a notification for, which wins while it rings.
public struct IncomingCallState: Equatable, Sendable {
    /// What a tap on a ring's notification or ntfy link opens.
    public enum Opened: Equatable, Sendable {
        case ringing(IncomingRing)
        /// It ran out (or the deck is out of reach): the desk's thread, with one line.
        case missed(threadID: String, agent: String, notice: String)
    }

    public private(set) var showing: IncomingRing?
    private var presenter = IncomingCallPresenter()
    private var focus: String?

    public init() {}

    @discardableResult
    public mutating func update(_ rings: [IncomingRing], now: Date) -> IncomingRing? {
        presenter.update(rings, now: now)
        showing = presenter.showing
        guard let focus else { return showing }
        var probe = presenter
        if let tapped = probe.update(rings.filter { $0.id == focus }, now: now) {
            showing = tapped
        } else {
            self.focus = nil
        }
        return showing
    }

    /// A tapped ring, after a fresh page. `ringing` nil: the page could not be
    /// fetched. Nil for a tap that is not a ring (the thread opens, as before).
    public mutating func open(_ route: AlertRoute, ringing: [IncomingRing]?, now: Date) -> Opened? {
        guard let id = route.ringID else { return nil }
        let name = Agent.displayName(forWireName: route.agent)
        guard let ringing else {
            return .missed(threadID: route.threadID, agent: route.agent,
                           notice: "Couldn't reach the deck — \(name)'s reason will be in the thread")
        }
        focus = id
        if let ring = update(ringing, now: now), ring.id == id { return .ringing(ring) }
        return .missed(threadID: route.threadID, agent: route.agent,
                       notice: "Missed — \(name)'s reason is in the thread")
    }

    /// He tapped Answer or Decline here.
    public mutating func handle(_ id: String) {
        presenter.handle(id)
        if focus == id { focus = nil }
        if showing?.id == id { showing = presenter.showing }
    }
}

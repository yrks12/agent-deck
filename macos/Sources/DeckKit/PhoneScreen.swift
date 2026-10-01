import Foundation

// MARK: - "<Agent>'s computer", on a phone
//
// The Mac's take-over already speaks the three screen routes: the poll
// (`AgentComputerModel`), the letterbox (`ScreenFit`), the wire body
// (`ScreenInput`) and the keysyms (`ScreenKeys`). The phone reuses every one
// of them. What it adds is only what a finger needs and a mouse never did,
// kept here — away from UIKit — so each rule is a pure function with a test.

/// **Pinch zoom, and the way back out of it.**
///
/// A 1280x800 desktop fitted to a phone held upright is about 400pt wide, so
/// he zooms. The view draws the fitted picture, then scales it about the
/// view's centre and pans it by `offset`:
///
///     drawn = centre + (fitted - centre) * scale + offset
///
/// A tap lands in view coordinates and has to go back through that before
/// `ScreenFit` turns it into a display pixel — otherwise at 2x a tap on a
/// button sends a click to somewhere else entirely.
///
/// The centre of the view is the centre of the fitted picture (`ScreenFit`
/// centres it), so the fit alone is enough to invert the zoom.
public struct ScreenZoom: Equatable, Sendable {
    /// 4x: a 1280-pixel display on a ~400pt phone is then drawn at better than
    /// one pixel per point, which is as far as zooming shows anything new.
    public static let maxScale: Double = 4
    public static let fitted = ScreenZoom(scale: 1, offsetX: 0, offsetY: 0)

    public var scale: Double
    public var offsetX: Double
    public var offsetY: Double

    public init(scale: Double, offsetX: Double, offsetY: Double) {
        self.scale = scale
        self.offsetX = offsetX
        self.offsetY = offsetY
    }

    public var isZoomed: Bool { scale > 1.001 }

    /// Settled: scale within `1...maxScale`, and panned no further than keeps
    /// picture under every edge that had picture. Below 1x there is nothing to
    /// pan, so the result is exactly `fitted`.
    public func clamped(to fit: ScreenFit, viewWidth: Double, viewHeight: Double) -> ScreenZoom {
        let s = min(Self.maxScale, max(1, scale.isFinite ? scale : 1))
        guard s > 1 else { return .fitted }
        let spareX = max(0, (fit.drawnWidth * s - viewWidth) / 2)
        let spareY = max(0, (fit.drawnHeight * s - viewHeight) / 2)
        return ScreenZoom(scale: s,
                          offsetX: min(spareX, max(-spareX, offsetX.isFinite ? offsetX : 0)),
                          offsetY: min(spareY, max(-spareY, offsetY.isFinite ? offsetY : 0)))
    }

    /// The display pixel under a point in the view, or nil when that point is
    /// not on the picture (the letterbox, or past a zoomed edge). Nil is sent
    /// nowhere: the deck refuses off-screen rather than clamping, and so does
    /// this.
    public func displayPoint(atViewX x: Double, y: Double, in fit: ScreenFit) -> DisplayPoint? {
        guard scale > 0, scale.isFinite else { return nil }
        let cx = fit.drawnX + fit.drawnWidth / 2
        let cy = fit.drawnY + fit.drawnHeight / 2
        let fx = cx + (x - cx - offsetX) / scale
        let fy = cy + (y - cy - offsetY) / scale
        return fit.displayPoint(atViewX: fx, y: fy)
    }
}

/// **Two fingers dragged are the wheel.**
///
/// `screen/input` takes a scroll in whole notches, one axis per call, `1...10`,
/// with `dy < 0` meaning down and `dx > 0` meaning right. A drag reports a
/// running translation, so this keeps count of the notches already sent and
/// answers only with the ones that are new. Content follows the fingers, as
/// everywhere else on a phone: fingers up is the page going down.
public struct ScreenScrollDrag: Equatable, Sendable {
    /// Points of finger travel per notch. One notch is three lines in most
    /// X apps; 24pt keeps a short drag worth a line or two and a long one a page.
    public static let pointsPerNotch: Double = 24

    private var sentY = 0
    private var sentX = 0

    public init() {}

    /// The scroll to send now for a drag that has travelled this far in total,
    /// or nil when it has not yet travelled another whole notch.
    public mutating func input(at point: DisplayPoint,
                               translationX: Double, translationY: Double) -> ScreenInput? {
        guard translationX.isFinite, translationY.isFinite else { return nil }
        let wantY = Int(translationY / Self.pointsPerNotch)
        let wantX = Int(-translationX / Self.pointsPerNotch)
        var dy = wantY - sentY
        var dx = wantX - sentX
        guard dy != 0 || dx != 0 else { return nil }
        // One axis per call: the route refuses both. The other axis stays
        // owed and goes on the next movement.
        if dy != 0 && dx != 0 {
            if abs(translationY) >= abs(translationX) { dx = 0 } else { dy = 0 }
        }
        let cap = ScreenScroll.maxNotches
        dy = min(cap, max(-cap, dy))
        dx = min(cap, max(-cap, dx))
        sentY += dy
        sentX += dx
        return .scroll(x: point.x, y: point.y, dy: dy, dx: dx)
    }
}

/// **His keyboard, for the agent's machine.** Text is sent verbatim — he
/// types passwords through this — and the named keys come out of `ScreenKeys`
/// (the Mac's table, which the deck's keysym regex is tested against), never
/// out of a string literal in a view.
public enum ScreenTyping {
    public static let returnKey = named(36)
    public static let backspace = named(51)
    public static let tab = named(48)
    public static let escape = named(53)

    /// What pressing Send (or Return on the phone's keyboard) puts on the wire,
    /// in order. Empty text with Return is still a Return.
    public static func inputs(forSubmitted text: String, pressReturn: Bool) -> [ScreenInput] {
        var out: [ScreenInput] = []
        if !text.isEmpty { out.append(.type(text)) }
        if pressReturn { out.append(returnKey) }
        return out
    }

    private static func named(_ keyCode: UInt16) -> ScreenInput {
        ScreenKeys.input(for: KeyStroke(keyCode: keyCode)) ?? .key(ScreenKeys.namedKeys[keyCode] ?? "Return")
    }
}

/// **The phone's screen view, as a watcher.** One `AgentComputerModel`, driven
/// by what the view knows: it appeared, it went away, the app went to or came
/// back from the background, and his hands are on it. Idempotent, because
/// SwiftUI may say "appeared" twice and a watcher counted twice is a poll that
/// never stops.
///
/// **The rate follows his hands.** Idle, 2 frames a second — each frame is an
/// `ffmpeg` grab in the agent's container. While he is touching it, 6, so he
/// sees what his tap did; `idleAfter` seconds after the last touch it drops
/// back.
@MainActor
public final class ScreenViewing {
    public static let idleFrameInterval: TimeInterval = 0.5
    public static let activeFrameInterval: TimeInterval = 1.0 / 6.0
    public static let idleAfter: TimeInterval = 3

    public let model: AgentComputerModel
    private let now: () -> Date
    private var isShown = false
    private var isBoosted = false
    private var lastInteraction: Date?

    public init(model: AgentComputerModel, now: @escaping () -> Date = { Date() }) {
        self.model = model
        self.now = now
    }

    /// The model the phone uses: idle rate as a glance, fast while driving.
    public static func phoneModel(desk: String, displayName: String, client: AgentScreenClient,
                                  clock: ScreenPollClock = TaskPollClock()) -> AgentComputerModel {
        AgentComputerModel(desk: desk, displayName: displayName, client: client,
                           interval: idleFrameInterval, takeoverInterval: activeFrameInterval,
                           clock: clock)
    }

    public func appeared(sceneActive: Bool) {
        model.setWindowActive(sceneActive)
        guard !isShown else { return }
        isShown = true
        model.addWatcher(.thumbnail)
    }

    public func disappeared() {
        guard isShown else { return }
        isShown = false
        model.removeWatcher(.thumbnail)
        unboost()
    }

    public func scene(active: Bool) {
        model.setWindowActive(active)
    }

    /// He touched it: fast frames now, for a while.
    public func interacted() {
        guard isShown else { return }
        lastInteraction = now()
        if !isBoosted {
            isBoosted = true
            model.addWatcher(.takeover)
        }
    }

    /// Called on a timer by the view: back to the idle rate once he has let
    /// go for `idleAfter` seconds.
    public func settle() {
        guard isBoosted, let last = lastInteraction,
              now().timeIntervalSince(last) >= Self.idleAfter else { return }
        unboost()
    }

    private func unboost() {
        guard isBoosted else { return }
        isBoosted = false
        model.removeWatcher(.takeover)
    }
}

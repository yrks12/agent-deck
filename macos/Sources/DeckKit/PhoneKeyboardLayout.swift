import Foundation

// MARK: - the agent's screen with the keyboard up
//
// The owner's screenshot: typing a Google password on Atlas's screen, the
// picture was laid out for the whole phone while the key bar and the iOS
// keyboard covered the bottom half — the field he had tapped was under them,
// and the top of the picture was dead black. These are the rules that fix it,
// kept pure so each is a tested function and the view only measures and draws.

/// **What is left to draw in.** The stage stops at the key bar's top edge
/// (top-aligned, so nothing is laid out under the keyboard), and inside it the
/// band he can actually see runs from below the toolbar to that edge.
public struct ScreenVisibleArea: Equatable, Sendable {
    public let width: Double
    /// The stage's height: the view's height less what covers its bottom.
    public let stageHeight: Double
    /// Where the band he can see starts (below the toolbar) and ends.
    public let bandTop: Double
    public let bandBottom: Double
    /// Something (the key bar, the keyboard) covers the bottom.
    public let isCovered: Bool

    public var bandMidY: Double { (bandTop + bandBottom) / 2 }
}

public enum ScreenStageLayout {
    /// The area left once `coveredTop` points (status bar and toolbar) and
    /// `coveredBottom` points (key bar plus keyboard) are taken. Negative
    /// covers are no cover; a cover taller than the view leaves nothing.
    public static func visible(viewWidth: Double, viewHeight: Double,
                               coveredTop: Double, coveredBottom: Double) -> ScreenVisibleArea {
        let height = max(0, viewHeight.isFinite ? viewHeight : 0)
        let bottom = min(height, max(0, coveredBottom.isFinite ? coveredBottom : 0))
        let stage = height - bottom
        let top = min(stage, max(0, coveredTop.isFinite ? coveredTop : 0))
        return ScreenVisibleArea(width: max(0, viewWidth), stageHeight: stage,
                                 bandTop: top, bandBottom: stage, isCovered: bottom > 0.5)
    }

    /// The status line's row under the picture, when there is a line to show.
    public static let statusHeight: Double = 28

    /// **The stage laid out round the controls, never under them.** The owner,
    /// of controls floating over the desk's picture: "the UI blocks stuff".
    /// The stage starts at the toolbar's bottom edge (`top`) and ends above
    /// the status row and the key bar; inside it nothing covers anything, so
    /// the band he can see is the whole stage.
    public static func uncovered(viewWidth: Double, viewHeight: Double, coveredTop: Double,
                                 coveredBottom: Double, statusHeight: Double)
        -> (top: Double, area: ScreenVisibleArea) {
        let height = max(0, viewHeight.isFinite ? viewHeight : 0)
        let top = min(height, max(0, coveredTop.isFinite ? coveredTop : 0))
        let bottom = min(height - top, max(0, coveredBottom.isFinite ? coveredBottom : 0))
        let status = min(height - top - bottom, max(0, statusHeight))
        let stage = height - top - bottom - status
        return (top, ScreenVisibleArea(width: max(0, viewWidth), stageHeight: stage,
                                       bandTop: 0, bandBottom: stage, isCovered: bottom > 0.5))
    }
}

extension ScreenZoom {
    /// Zoomed to at least the readable size (or more, if he was closer), and
    /// panned so a display point — the field he tapped — sits at the middle of
    /// the view across and at `centreY` down. Clamped like every other zoom, so
    /// a field in a corner comes as near the middle as the picture allows and
    /// never leaves black where the desktop should be.
    public func focusing(displayX x: Double, y: Double, in fit: ScreenFit,
                         viewWidth: Double, viewHeight: Double, centreY: Double) -> ScreenZoom {
        let readable = ScreenZoom.opening(fit: fit, viewWidth: viewWidth, viewHeight: viewHeight).scale
        let s = min(Self.maxScale(for: fit), max(readable, scale.isFinite ? scale : 1))
        let fx = fit.drawnX + (x / Double(fit.displayWidth)) * fit.drawnWidth
        let fy = fit.drawnY + (y / Double(fit.displayHeight)) * fit.drawnHeight
        let cx = fit.drawnX + fit.drawnWidth / 2
        let cy = fit.drawnY + fit.drawnHeight / 2
        // drawn = c + (f - c) * s + offset  →  offset = target - c - (f - c) * s
        return ScreenZoom(scale: s,
                          offsetX: viewWidth / 2 - cx - (fx - cx) * s,
                          offsetY: centreY - cy - (fy - cy) * s)
            .clamped(to: fit, viewWidth: viewWidth, viewHeight: viewHeight)
    }
}

/// **Keyboard up: the field he tapped, centred. Keyboard down: his zoom back.**
///
/// The zoom he had is kept with the stage size it was for, from the first
/// time the keyboard covers the screen until it goes. Moves of the keyboard in
/// between (the QuickType bar, the key bar appearing) centre again but never
/// overwrite what to go back to. Turned while typing, the kept zoom is for
/// the other shape, so closing opens readable instead.
public struct ScreenKeyboardZoom: Equatable, Sendable {
    private var saved: ScreenZoom?
    private var savedWidth = 0.0
    private var savedHeight = 0.0

    public init() {}

    public var isUp: Bool { saved != nil }

    /// The zoom for the stage while the keyboard covers part of it. `current`
    /// is what is on screen now, drawn at `currentWidth` x `currentHeight`.
    public mutating func shown(current: ScreenZoom, currentWidth: Double, currentHeight: Double,
                               focus: DisplayPoint?, fit: ScreenFit, area: ScreenVisibleArea) -> ScreenZoom {
        if saved == nil {
            saved = current
            savedWidth = currentWidth
            savedHeight = currentHeight
        }
        guard let focus else {
            return .opening(fit: fit, viewWidth: area.width, viewHeight: area.stageHeight)
        }
        return current.focusing(displayX: Double(focus.x), y: Double(focus.y), in: fit,
                                viewWidth: area.width, viewHeight: area.stageHeight, centreY: area.bandMidY)
    }

    /// The zoom to go back to now the keyboard has gone, for a stage of this size.
    public mutating func hidden(fit: ScreenFit, viewWidth: Double, viewHeight: Double) -> ScreenZoom {
        hiddenIfUp(fit: fit, viewWidth: viewWidth, viewHeight: viewHeight)
            ?? .opening(fit: fit, viewWidth: viewWidth, viewHeight: viewHeight)
    }

    /// As `hidden`, or nil when the keyboard was never up (nothing to restore).
    public mutating func hiddenIfUp(fit: ScreenFit, viewWidth: Double, viewHeight: Double) -> ScreenZoom? {
        guard let zoom = saved else { return nil }
        saved = nil
        let sameShape = abs(savedWidth - viewWidth) < 1 && abs(savedHeight - viewHeight) < 1
        return sameShape ? zoom.clamped(to: fit, viewWidth: viewWidth, viewHeight: viewHeight)
                         : .opening(fit: fit, viewWidth: viewWidth, viewHeight: viewHeight)
    }
}

extension ScreenControl {
    /// For a toolbar too narrow for `buttonTitle`: "Hand back to Atlas" is
    /// what a phone cuts to "H…". The view falls back to this, then to the icon.
    public var shortButtonTitle: String { driver == .agent ? "Take over" : "Hand back" }

    /// The pill's icon, which is all of it on the narrowest toolbar.
    public var symbol: String { driver == .owner ? "hand.raised.slash" : "hand.raised.fill" }
}

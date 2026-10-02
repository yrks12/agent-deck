import Foundation
import CoreGraphics

/// **"On desktop, make the view bigger so we can see what the agents do."**
///
/// Which halves of the agent's computer are on screen. Both = the desktop and
/// its terminal side by side (or stacked, in a tall area).
public enum ComputerView: String, CaseIterable, Sendable {
    case screen
    case terminal
    case both

    public var title: String {
        switch self {
        case .screen: return "Screen"
        case .terminal: return "Terminal"
        case .both: return "Both"
        }
    }

    public var symbol: String {
        switch self {
        case .screen: return "display"
        case .terminal: return "terminal"
        case .both: return "rectangle.split.2x1"
        }
    }
}

/// The computer's layout as numbers: the take-over sheet, the Expand window,
/// and Watch mode (the computer in the main window, the chat a side panel).
/// The display's size is always an input — the desk's desktop arrives in the
/// screen's hello/status (1280x800 then, 1600x1000 now) and is never assumed.
public enum ComputerLayout {

    // MARK: windows

    /// The share of the screen's visible frame the sheet and Expand open at.
    public static let screenShare: CGFloat = 0.9

    public static func defaultWindowSize(visibleFrame: CGSize) -> CGSize {
        CGSize(width: (visibleFrame.width * screenShare).rounded(.down),
               height: (visibleFrame.height * screenShare).rounded(.down))
    }

    /// A sheet hangs from its window and AppKit clips one that is bigger, so
    /// it is the screen's 90% only as far as the window allows.
    public static func sheetSize(visibleFrame: CGSize, window: CGSize) -> CGSize {
        let wanted = defaultWindowSize(visibleFrame: visibleFrame)
        return CGSize(width: min(wanted.width, window.width),
                      height: min(wanted.height, window.height))
    }

    // MARK: the picture

    /// The content in its own shape, as large as fits, centred.
    public static func fit(content: CGSize, in area: CGSize) -> CGRect {
        guard content.width > 0, content.height > 0, area.width > 0, area.height > 0
        else { return .zero }
        let scale = min(area.width / content.width, area.height / content.height)
        let size = CGSize(width: content.width * scale, height: content.height * scale)
        return CGRect(x: (area.width - size.width) / 2, y: (area.height - size.height) / 2,
                      width: size.width, height: size.height)
    }

    // MARK: Screen | Terminal | Both

    public static let minTerminalWidth: CGFloat = 420
    public static let minTerminalHeight: CGFloat = 240
    /// The terminal's largest share of the area when both are up.
    public static let maxTerminalShare: CGFloat = 0.45

    public enum Axis: Equatable, Sendable { case horizontal, vertical }

    public struct Split: Equatable, Sendable {
        public let screen: CGRect
        public let terminal: CGRect
        public let axis: Axis
    }

    /// Where each half goes. In Both, the screen is given what it can fill in
    /// its own shape and the terminal takes the rest — never less than a usable
    /// terminal, never more than `maxTerminalShare` of the area.
    public static func split(_ view: ComputerView, area: CGSize, display: CGSize) -> Split {
        let whole = CGRect(origin: .zero, size: area)
        switch view {
        case .screen: return Split(screen: whole, terminal: .zero, axis: .horizontal)
        case .terminal: return Split(screen: .zero, terminal: whole, axis: .horizontal)
        case .both: break
        }
        let aspect = display.height > 0 ? display.width / display.height : 1.6
        if area.width >= area.height {
            let spare = area.width - area.height * aspect
            let terminal = min(max(spare, minTerminalWidth), area.width * maxTerminalShare)
            let screenWidth = area.width - terminal
            return Split(
                screen: CGRect(x: 0, y: 0, width: screenWidth, height: area.height),
                terminal: CGRect(x: screenWidth, y: 0, width: terminal, height: area.height),
                axis: .horizontal)
        }
        let spare = area.height - area.width / aspect
        let terminal = min(max(spare, minTerminalHeight), area.height * 0.5)
        let screenHeight = area.height - terminal
        return Split(
            screen: CGRect(x: 0, y: 0, width: area.width, height: screenHeight),
            terminal: CGRect(x: 0, y: screenHeight, width: area.width, height: terminal),
            axis: .vertical)
    }

    // MARK: Watch mode

    /// The chat beside the computer: a readable column, never most of the window.
    public static let chatPanelRange: ClosedRange<CGFloat> = 320...460

    public static func chatPanelWidth(expanded: Bool, windowWidth: CGFloat) -> CGFloat {
        guard expanded else { return 0 }
        let share = (windowWidth * 0.26).rounded(.down)
        return min(max(share, chatPanelRange.lowerBound), chatPanelRange.upperBound)
    }
}

/// **"The UI blocks stuff."** — the owner, of the take-over: a second header
/// floated over the top of the desk's screen and the keys bar over its bottom.
///
/// The take-over's chrome, laid out *round* the picture rather than over it:
/// one header across the top, the keys bar docked under the screen, the
/// picture fitted (letterboxed) in what is left. Nothing is ever drawn over
/// the live picture. Pinned by `NothingCoversTheDesksScreenTests`.
public enum TakeoverChrome {
    /// The one header: whose computer, Screen | Terminal | Both, the actions.
    public static let headerHeight: CGFloat = 44
    /// Where his keys go, the named keys, paste a line.
    public static let keysBarHeight: CGFloat = 44

    public struct Frames: Equatable, Sendable {
        public let header: CGRect
        /// The screen's whole slot: the picture's letterbox plus its keys bar.
        public let screenSlot: CGRect
        /// The desk's picture as drawn, in its own shape.
        public let picture: CGRect
        public let keysBar: CGRect
        public let terminal: CGRect
    }

    public static func frames(_ view: ComputerView, area: CGSize, display: CGSize) -> Frames {
        let header = CGRect(x: 0, y: 0, width: area.width, height: min(headerHeight, area.height))
        let body = CGSize(width: area.width, height: max(0, area.height - header.height))
        let split = ComputerLayout.split(view, area: body, display: display)
        let slot = split.screen.offsetBy(dx: 0, dy: header.maxY)
        let terminal = split.terminal.isEmpty ? .zero : split.terminal.offsetBy(dx: 0, dy: header.maxY)
        guard !slot.isEmpty else {
            return Frames(header: header, screenSlot: .zero, picture: .zero, keysBar: .zero, terminal: terminal)
        }
        let bar = min(keysBarHeight, slot.height)
        let keysBar = CGRect(x: slot.minX, y: slot.maxY - bar, width: slot.width, height: bar)
        let room = CGSize(width: slot.width, height: slot.height - bar)
        let fitted = ComputerLayout.fit(content: display, in: room)
        let picture = fitted.isEmpty ? .zero : fitted.offsetBy(dx: slot.minX, dy: slot.minY)
        return Frames(header: header, screenSlot: slot, picture: picture, keysBar: keysBar, terminal: terminal)
    }
}

import Foundation
import CoreGraphics

/// **The way in to a desk's own machine, as a value.**
///
/// *"i need to be able to controll the google chrom and the temrinal we should
/// have like i use his screen when it needs me."*
///
/// Every part of that already existed and none of it was reachable. The stage
/// in `AgentScreenPanel` has real mouse and keyboard; `DeskTerminalPanel` runs
/// real commands. What was missing is the *route*: the only way into the stage
/// was a button that existed while the pointer was resting on a 300-point
/// thumbnail, below the fold of a `Form`, in the narrowest column of the
/// window — and the two places a stopped desk actually announces itself, the
/// strip on the conversation and the blocked line in the inspector, offered
/// nothing at all.
///
/// So the route is a value, decided here rather than in a view, for the same
/// reason `DeskStatus` is: three surfaces offer it and they must not end up
/// calling the same thing three different names.
///
/// **Nothing in this file opens anything.** An offer is a thing he may press.
/// A desk stopping is never allowed to present a take-over on its own — it is
/// a modal over the whole window and it takes the keyboard with it, and he is
/// usually mid-sentence somewhere else when a desk stops. One obvious click,
/// from where the problem is announced. `TheTakeOverIsOneClickFromWhereHeIsNeededTests`
/// pins both halves: that nothing opens by itself, and that pressing the named
/// action does open it.
public enum Takeover {

    /// The two names, written once. Every surface that offers a way in uses
    /// these, so a screen reader and a tooltip and a button all say the same
    /// words and the test can ask for them by name.
    public static let screenTitle = "Take over the screen"
    public static let terminalTitle = "Open the terminal"

    /// What the desk can be reached with, given what this deck can serve.
    ///
    /// Two honest absences rather than two dead buttons:
    ///
    /// * **no screen routes on this deck** — `DeckStore.screenClient` is nil,
    ///   there is nothing to draw and a take-over would open a black rectangle;
    /// * **no working directory** — `POST /terminal` takes an absolute path and
    ///   this app must never invent a home folder the deck was not asked about,
    ///   so a desk whose `cwd` the deck never stated gets the screen without the
    ///   terminal rather than a prompt pointing at a guess.
    ///
    /// A desk whose *container* is not up still gets the screen offer: whether
    /// the machine is running is something only the deck can answer, the stage
    /// asks it on open, and "Atlas has no computer running yet" is a sentence
    /// worth reading — not a dead end.
    public static func offers(
        for agent: Agent?, canServeScreen: Bool, canServeShell: Bool
    ) -> [TakeoverOffer] {
        guard let agent else { return [] }
        var offers: [TakeoverOffer] = []
        if canServeScreen {
            offers.append(TakeoverOffer(
                focus: .screen,
                title: screenTitle,
                hint: "Click, type and scroll on \(agent.displayName)'s own machine, "
                    + "full size. Command keys stay with this Mac.",
                symbol: "display"))
        }
        if canServeShell, agent.workspace.hasPrefix("/") {
            offers.append(TakeoverOffer(
                focus: .terminal,
                title: terminalTitle,
                hint: "Run a command on \(agent.displayName)'s machine, in "
                    + "\(agent.shortWorkspace).",
                symbol: "terminal"))
        }
        return offers
    }

    /// The desk, as the stage needs it. Values only — the stage outlives any
    /// one roster refresh and must not go blank because a poll was in flight.
    public static func request(for agent: Agent, focus: TakeoverOffer.Focus) -> TakeoverRequest {
        TakeoverRequest(desk: agent.name, displayName: agent.displayName,
                        workspace: agent.workspace, focus: focus)
    }

    /// **How big the take-over is, measured against the window it lives in.**
    ///
    /// A sheet is sized from its content, and AppKit clips one that is bigger
    /// than the window it is presented from — so both numbers are decided
    /// against real windows rather than picked:
    ///
    /// * `min` is the promise for the **smallest window the app allows**.
    ///   `DeckAppMain` sets `.frame(minWidth: 900, minHeight: 560)`, so a
    ///   take-over that demanded more than that would have Done off the edge
    ///   the moment he dragged the window in.
    /// * `ideal` is what it asks for in **his** window, 1208x949 measured from
    ///   the Accessibility tree of the running app.
    ///
    /// The shipped stage asked for 700x520. The display it is drawing is
    /// 1280x800, so 700 points of sheet minus its own padding put the picture
    /// at about **half scale** — which is a browser whose text cannot be read,
    /// which is a take-over he cannot use. At 1120 the same picture is drawn
    /// at roughly 60% with the terminal beside it and 85% without, and it is
    /// more than three times the inspector column it replaces.
    ///
    /// Pinned by `TheTakeOverIsOneClickFromWhereHeIsNeededTests`.
    ///
    /// **The floor came down (2026-09-30).** 860 of stage plus a 340pt
    /// terminal beside it is 1200pt, and in a 900pt window AppKit centred and
    /// clipped it — his screenshot read "creen" on the left and the terminal
    /// ran off the right. The controls now float over a full-bleed picture
    /// like the phone's, and the terminal is a drawer sized from the stage, so
    /// the floor only has to hold the toolbar.
    /// Pinned by `TheTakeOverLooksLikeThePhoneTests`.
    public enum Stage {
        public static let minWidth: CGFloat = 640
        public static let minHeight: CGFloat = 420
        public static let idealWidth: CGFloat = 1120
        public static let idealHeight: CGFloat = 860
        /// The terminal drawer's widest. Wide enough for a path and a command
        /// without wrapping every line of output.
        public static let terminalWidth: CGFloat = 340

        /// The drawer for a stage this wide: never more than 42% of it, so the
        /// agent's screen keeps most of the sheet, and never more than
        /// `terminalWidth`.
        public static func drawerWidth(stageWidth: CGFloat) -> CGFloat {
            max(0, min(terminalWidth, (stageWidth * 0.42).rounded(.down)))
        }

        /// **The sheet's size: ~90% of his window, in the display's shape**
        /// (1280x800 unless told otherwise), so the picture fills it with no
        /// black bands. Never under the floor.
        public static func sheetSize(window: CGSize, display: CGSize = CGSize(width: 1280, height: 800))
            -> CGSize {
            let aspect = display.width / max(display.height, 1)
            var width = window.width * 0.9
            var height = width / aspect
            if height > window.height * 0.9 {
                height = window.height * 0.9
                width = height * aspect
            }
            width = max(width, minWidth)
            height = max(height, width / aspect)
            return CGSize(width: width.rounded(.down), height: height.rounded(.down))
        }
    }
}

/// **Where a key he presses now will land — one answer, from AppKit's facts.**
///
/// The stage used to hold two: SwiftUI's `@FocusState` drew the caption and
/// armed the monitor, and the window's first responder decided what the
/// monitor did. They disagreed the moment a sheet focused its own text field,
/// and the caption said "Your keys stay in this app" beside a switch that said
/// on. Both the sentence and the routing are now this value, and it is built
/// only from what AppKit will actually do with the next key-down.
public enum KeyRoute: Equatable, Sendable {
    /// Forwarded to the desk's machine.
    case agent
    /// A text field of this app is the first responder and takes it.
    case thisApp
    /// The take-over's window is not the key window, so the key goes to
    /// whatever window is — another app, or a dialog he may not be able to see.
    case anotherWindow

    public static func decide(switchOn: Bool, windowIsKey: Bool,
                              textFieldHasKeys: Bool) -> KeyRoute {
        guard windowIsKey else { return .anotherWindow }
        if textFieldHasKeys { return .thisApp }
        return switchOn ? .agent : .thisApp
    }

    /// The caption beside the Keyboard switch.
    public func caption(agent name: String) -> String {
        switch self {
        case .agent: return "Your keys go to \(name)"
        case .thisApp: return "Your keys stay in this app"
        case .anotherWindow: return "Your keys are with another window. Click here first."
        }
    }
}

/// One way in, named. Drawn as a button by the strip on the conversation, by
/// the inspector's blocked section, and under the thumbnail.
public struct TakeoverOffer: Equatable, Sendable, Identifiable {
    /// Which half of the machine he pressed for. Both open the same stage —
    /// the screen is always there — and this only decides whether the terminal
    /// comes up beside it.
    public enum Focus: String, Equatable, Sendable, CaseIterable {
        case screen
        case terminal
    }

    public let focus: Focus
    public let title: String
    /// What pressing it will do, said before it is pressed. Drawn as a tooltip
    /// and spoken as the button's accessibility hint.
    public let hint: String
    /// A **still** SF Symbol. Nothing in this product animates while it waits.
    public let symbol: String

    public var id: String { focus.rawValue }

    public init(focus: Focus, title: String, hint: String, symbol: String) {
        self.focus = focus
        self.title = title
        self.hint = hint
        self.symbol = symbol
    }
}

/// A take-over that is open, or that is being opened. `nil` on the store means
/// **closed**, and there is no third state.
public struct TakeoverRequest: Equatable, Sendable, Identifiable {
    public let desk: String
    public let displayName: String
    /// The desk's absolute working directory, or empty when the deck never
    /// stated one. Carried as the deck wrote it — the terminal needs the real
    /// path, not the `~`-shortened one a person reads.
    public let workspace: String
    public let focus: TakeoverOffer.Focus

    /// Keyed on the focus as well as the desk, so pressing "Open the terminal"
    /// while the screen is already up rebuilds the sheet with the terminal in
    /// it rather than silently doing nothing.
    public var id: String { "\(desk)#\(focus.rawValue)" }

    public var title: String { "\(displayName)'s computer" }

    /// Whether a command has anywhere to run. See `Takeover.offers`.
    public var hasTerminal: Bool { workspace.hasPrefix("/") }

    /// The terminal comes up beside the picture when that is what he asked
    /// for. He can always reach it by pressing the other button.
    public var showsTerminal: Bool { hasTerminal && focus == .terminal }

    public init(desk: String, displayName: String, workspace: String,
                focus: TakeoverOffer.Focus) {
        self.desk = desk
        self.displayName = displayName
        self.workspace = workspace
        self.focus = focus
    }
}

/// **The agent's screen in its own window** — what Expand opens. Codable so
/// SwiftUI can restore the window; carries only who, never a client.
public struct ExpandedScreen: Codable, Hashable, Sendable {
    public let desk: String
    public let displayName: String

    public init(desk: String, displayName: String) {
        self.desk = desk
        self.displayName = displayName
    }

    public init(_ request: TakeoverRequest) {
        self.init(desk: request.desk, displayName: request.displayName)
    }

    /// The screen of the same desk, with no terminal: the window is for the
    /// picture.
    public var takeoverRequest: TakeoverRequest {
        TakeoverRequest(desk: desk, displayName: displayName, workspace: "", focus: .screen)
    }
}

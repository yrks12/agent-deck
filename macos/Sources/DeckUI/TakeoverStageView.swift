import SwiftUI
import DeckKit

/// **The take-over, over the whole window.**
///
/// *"i need to be able to controll the google chrom and the temrinal we should
/// have like i use his screen when it needs me."*
///
/// Both halves in one place, because they are one job. The picture is the
/// desk's real display with real mouse and keyboard on it (`AgentScreenStage`);
/// beside it, when the deck can run one and the desk has a working directory,
/// is that desk's shell (`DeskTerminalPanel`). Neither of them used to be
/// reachable from the moment that needs them — the screen was behind a button
/// that only existed under the pointer, and the terminal was below the fold of
/// a 300-point column under it.
///
/// **Why it is presented here and not inside the thumbnail.** A sheet is sized
/// from its content and drawn over the window it belongs to, so this is the
/// only place in the app with room to draw a 1280x800 display at a size a
/// person can read. The shipped stage lived inside the inspector's panel and
/// asked for 700 points — about half scale, which is a browser whose text
/// cannot be read. See `Takeover.Stage` for both numbers and the windows they
/// were measured against.
///
/// **It owns its own poll.** `AgentComputerModel` is built here and dies with
/// the sheet, and it asks for the take-over rate (0.25s) rather than the
/// thumbnail's 1s — at 1 Hz he cannot see the result of his own click, which
/// was the original complaint. The thumbnail in the inspector keeps its own
/// slower poll while this is open; that is one extra `ffmpeg` grab a second on
/// the box for as long as the sheet is up, and it stops the moment it closes.
public struct TakeoverStageView: View {
    /// Built here rather than shared with the thumbnail: the two are on screen
    /// for different reasons and at different rates, and a model handed across
    /// a sheet boundary outlives the desk selection that made it.
    @StateObject private var model: AgentComputerModel
    private let request: TakeoverRequest
    private let shells: DeskShellClient?
    private let onClose: () -> Void

    /// Which button he pressed decides whether the terminal comes up with the
    /// picture. Either way it is one click away in the header, never a hidden
    /// one — see `AgentScreenStage.header`.
    @State private var showsTerminal: Bool

    public init(request: TakeoverRequest,
                screens: AgentScreenClient,
                shells: DeskShellClient?,
                onClose: @escaping () -> Void) {
        self.request = request
        self.shells = shells
        self.onClose = onClose
        _model = StateObject(wrappedValue: AgentComputerModel(
            desk: request.desk, displayName: request.displayName, client: screens))
        _showsTerminal = State(initialValue: request.showsTerminal)
    }

    /// The terminal is offered only when there is somewhere for a command to
    /// run: `POST /terminal` takes an absolute path and this app never invents
    /// a home folder the deck was not asked about.
    private var terminal: DeskShellClient? {
        request.hasTerminal ? shells : nil
    }

    public var body: some View {
        HStack(spacing: 0) {
            AgentScreenStage(
                model: model,
                onClose: onClose,
                terminalShown: terminal == nil ? nil : $showsTerminal)

            if let terminal, showsTerminal {
                Divider()
                shellColumn(terminal)
            }
        }
        // **The whole sizing story, and it is measured against real windows.**
        // `min` is what the smallest window `DeckAppMain` allows can hold;
        // `ideal` is what it asks for in his. `maxWidth`/`maxHeight` are
        // `.infinity` so a bigger window gets a bigger picture rather than a
        // letterboxed one — this is a sheet and not a window column, so it is
        // sized by AppKit against the window it is presented from and can
        // never publish an intrinsic size to a `NavigationSplitView`.
        .frame(minWidth: Takeover.Stage.minWidth,
               idealWidth: Takeover.Stage.idealWidth,
               maxWidth: .infinity,
               minHeight: Takeover.Stage.minHeight,
               idealHeight: Takeover.Stage.idealHeight,
               maxHeight: .infinity)
    }

    /// The desk's shell, beside the picture rather than under it: a terminal
    /// wants width for a path and a command, and the display beside it wants
    /// height.
    private func shellColumn(_ client: DeskShellClient) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            DeskTerminalPanel(
                desk: request.desk,
                displayName: request.displayName,
                client: client)
            .padding(12)
            Spacer(minLength: 0)
        }
        .frame(width: Takeover.Stage.terminalWidth)
        .background(.background.secondary)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(request.displayName)'s terminal")
    }
}

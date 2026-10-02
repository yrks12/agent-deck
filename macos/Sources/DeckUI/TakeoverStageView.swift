import SwiftUI
import AppKit
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
    /// The live terminal; when present it replaces the one-command drawer.
    private let terminals: DeskTerminalStreaming?
    private let onClose: () -> Void

    /// Screen, Terminal or Both. Opens on Both when he came for the
    /// terminal, on the screen otherwise; the switch is always on the bar.
    @State private var view: ComputerView
    /// Moves this computer into the main window (Watch); `nil` where it is.
    private let onWatch: (() -> Void)?
    /// Watch mode's chat panel, shown or not; `nil` outside Watch.
    private let chatShown: Binding<Bool>?

    /// Sends a line to the desk's own thread — how Take over and Hand back
    /// are said. `nil` hides them.
    private let tellDesk: ((String) async -> Bool)?
    private let onExpand: (() -> Void)?
    /// The sheet's own size (`Takeover.Stage.sheetSize`); `nil` in a window,
    /// which the window sizes.
    private let size: CGSize?
    /// Who is driving — said to the agent in its thread, the phone's way.
    /// Here, not in the stage, so switching to Terminal alone does not say
    /// "hand back" by tearing the stage down.
    @State private var control: ScreenControl
    @State private var askingAddress = false
    @State private var address = ""

    public init(request: TakeoverRequest,
                screens: AgentScreenClient,
                shells: DeskShellClient?,
                terminals: DeskTerminalStreaming? = nil,
                tellDesk: ((String) async -> Bool)? = nil,
                onExpand: (() -> Void)? = nil,
                size: CGSize? = nil,
                onWatch: (() -> Void)? = nil,
                chatShown: Binding<Bool>? = nil,
                onClose: @escaping () -> Void) {
        self.onWatch = onWatch
        self.chatShown = chatShown
        self.onExpand = onExpand
        self.size = size
        self.request = request
        self.shells = shells
        self.terminals = terminals
        self.tellDesk = tellDesk
        self.onClose = onClose
        _model = StateObject(wrappedValue: AgentComputerModel(
            desk: request.desk, displayName: request.displayName, client: screens))
        let canTerminal = terminals != nil || (request.hasTerminal && shells != nil)
        _view = State(initialValue: canTerminal && request.focus == .terminal ? .both : .screen)
        _control = State(initialValue: ScreenControl(agentName: request.displayName, device: "my Mac"))
    }

    /// The terminal is offered when the live one can be opened, or when the
    /// one-command route has somewhere to run: `POST /terminal` takes an
    /// absolute path and this app never invents a home folder.
    private var oneShot: DeskShellClient? {
        request.hasTerminal ? shells : nil
    }

    private var hasTerminal: Bool { terminals != nil || oneShot != nil }

    /// The desktop's real size, from the deck — 1280x800 once, 1600x1000 now.
    private var display: CGSize {
        guard let status = model.status else { return CGSize(width: 1600, height: 1000) }
        return CGSize(width: CGFloat(status.width), height: CGFloat(status.height))
    }

    public var body: some View {
        // **One computer, three ways to look at it**, and **nothing over the
        // picture** (`TakeoverChrome.frames`): one header across the top, the
        // screen (its keys bar docked under it) and the terminal side by side
        // or stacked below it. The halves are placed by the layout, so the
        // terminal keeps its identity (and its socket) across a switch.
        GeometryReader { geometry in
            let shown = hasTerminal ? view : .screen
            let frames = TakeoverChrome.frames(shown, area: geometry.size, display: display)
            ZStack(alignment: .topLeading) {
                header
                    .frame(width: frames.header.width, height: frames.header.height)
                if shown != .terminal {
                    AgentScreenStage(model: model)
                        .frame(width: frames.screenSlot.width, height: frames.screenSlot.height)
                        .offset(x: frames.screenSlot.minX, y: frames.screenSlot.minY)
                }
                if shown != .screen {
                    shellColumn
                        .frame(width: frames.terminal.width, height: frames.terminal.height)
                        .offset(x: frames.terminal.minX, y: frames.terminal.minY)
                }
            }
        }
        .background(Color.black)
        // `min` is what the smallest window `DeckAppMain` allows can hold;
        // `ideal` is what it asks for in his. `max` is infinite so a bigger
        // window gets a bigger picture.
        // In the sheet, `size` (~90% of his window) is both the floor and
        // what it asks for, so the sheet opens big.
        .frame(minWidth: size?.width ?? Takeover.Stage.minWidth,
               idealWidth: size?.width ?? Takeover.Stage.idealWidth,
               maxWidth: .infinity,
               minHeight: size?.height ?? Takeover.Stage.minHeight,
               idealHeight: size?.height ?? Takeover.Stage.idealHeight,
               maxHeight: .infinity)
        .onDisappear {
            if let text = control.messageOnClose, let tellDesk { Task { _ = await tellDesk(text) } }
        }
        .alert("Open a web address", isPresented: $askingAddress) {
            TextField("example.com", text: $address)
            Button("Go") {
                let inputs = BrowserActions.open(address)
                address = ""
                Task { for input in inputs { await model.send(input) } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Opens it in the browser on \(request.displayName)'s computer.")
        }
    }

    /// **The one header.** There used to be two: this strip ("Atlas's
    /// computer", Screen | Terminal | Both) and, floating over the top of the
    /// picture, the stage's own ("Atlas's screen", take over, an address,
    /// terminal). Merged, and laid above the picture rather than on it.
    private var header: some View {
        HStack(spacing: 8) {
            iconButton("xmark", "Close", help: "Close (⌘W). Escape goes to "
                       + "\(request.displayName)'s computer while the keyboard is switched on.",
                       action: onClose)
                .keyboardShortcut("w", modifiers: .command)
            VStack(alignment: .leading, spacing: 0) {
                Text(request.title).font(.headline).lineLimit(1)
                if view != .terminal, let note = model.displayNote {
                    Text(note).font(.caption).foregroundStyle(.orange).lineLimit(1)
                } else if view != .terminal, let line = model.presentation.statusLine {
                    Text(line).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            if view != .terminal {
                // His Mac with two or more displays: which one is shown.
                MacDisplayMenu(model: model) { current in
                    FlatLabel(current, systemImage: "display.2")
                        .lineLimit(1)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(.white.opacity(0.14), in: Capsule())
                }
                .menuStyle(.borderlessButton)
                .fixedSize()
                .help("Choose which of the Mac's displays to show and control")
            }
            Spacer(minLength: 8)
            if hasTerminal {
                DeckPillPicker(selection: $view,
                               options: ComputerView.allCases.map { ($0, $0.title) },
                               label: "Show the screen, the terminal or both")
                    .fixedSize()
            }
            Spacer(minLength: 8)
            if tellDesk != nil {
                Button(action: toggleControl) {
                    FlatLabel(control.driver == .owner ? "Hand back" : "Take over",
                              systemImage: control.driver == .owner ? "hand.raised.slash" : "hand.raised.fill")
                        .font(.callout.weight(.semibold))
                        .lineLimit(1)
                        .padding(.horizontal, 10).padding(.vertical, 5)
                        .background(control.driver == .owner ? AnyShapeStyle(Color.orange)
                                                             : AnyShapeStyle(.white.opacity(0.14)),
                                    in: Capsule())
                }
                .buttonStyle(.plain)
                .fixedSize()
                .help(control.driver == .owner
                      ? "Tell \(request.displayName) it can use its computer again."
                      : "Tell \(request.displayName) to keep its hands off while you drive.")
            }
            if view != .terminal {
                iconButton("globe", "Open a web address",
                           help: "Open a web address in \(request.displayName)'s browser") { askingAddress = true }
            }
            if let onExpand {
                iconButton("arrow.up.left.and.arrow.down.right", "Expand",
                           help: "Open \(request.displayName)'s computer in its own window you can make "
                           + "as big as you like, or full screen (⌘⇧F)", action: onExpand)
                    .keyboardShortcut("f", modifiers: [.command, .shift])
            }
            if let onWatch {
                Button(action: onWatch) {
                    FlatLabel("Watch", systemImage: "rectangle.inset.filled")
                }
                .buttonStyle(.plain)
                .fixedSize()
                .help("Put \(request.displayName)'s computer in the main window, with the chat beside it (⌘⇧W)")
            }
            if let chatShown {
                Button { chatShown.wrappedValue.toggle() } label: {
                    FlatLabel(chatShown.wrappedValue ? "Hide chat" : "Show chat",
                              systemImage: "bubble.left.and.bubble.right")
                }
                .buttonStyle(.plain)
                .fixedSize()
                .help("Show or hide the conversation beside the computer")
            }
        }
        .font(.callout)
        .padding(.horizontal, 10)
        .background(Color.black)
        .environment(\.colorScheme, .dark)
    }

    private func iconButton(_ glyph: String, _ label: String, help: String,
                            action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: glyph)
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(Color.white)
                .frame(width: 28, height: 28)
                .background(.white.opacity(0.14), in: Circle())
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(label)
    }

    /// Says Take over or Hand back in the desk's thread, the phone's way.
    private func toggleControl() {
        guard let tellDesk else { return }
        DeckHaptics.tap()
        let taking = control.driver == .agent
        let text = taking ? control.takeOverMessage : control.handBackMessage
        if taking { control.tookOver() } else { control.handedBack() }
        Task { _ = await tellDesk(text) }
    }

    /// The desk's shell, in the drawer: the live terminal when the deck has
    /// one (it falls back to the one-command panel by itself), else the panel.
    @ViewBuilder
    private var shellColumn: some View {
        Group {
            if let terminals {
                LiveTerminalView(desk: request.desk, displayName: request.displayName,
                                 streams: terminals, fallback: oneShot)
            } else if let oneShot {
                VStack(alignment: .leading, spacing: 0) {
                    DeskTerminalPanel(
                        desk: request.desk,
                        displayName: request.displayName,
                        client: oneShot)
                    .padding(12)
                    Spacer(minLength: 0)
                }
            }
        }
        .background(.ultraThickMaterial)
        .environment(\.colorScheme, .dark)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(request.displayName)'s terminal")
    }
}

/// **The agent's screen in its own window** (Expand). Resizable, can go full
/// screen, and remembers where he put it. The picture scales with the window;
/// clicks are mapped through the same `ScreenFit` as the sheet.
public struct ExpandedScreenWindow: View {
    let screen: ExpandedScreen
    let screens: AgentScreenClient?
    let terminals: DeskTerminalStreaming?
    @Environment(\.dismiss) private var dismiss

    public init(screen: ExpandedScreen, client: DeckClient) {
        self.screen = screen
        self.screens = client as? AgentScreenClient
        // His Mac (`mac:<node>`) has a live view, not a desk terminal.
        self.terminals = MacScreenName.nodeId(screen.desk) == nil ? client as? DeskTerminalStreaming : nil
    }

    public var body: some View {
        Group {
            if let screens {
                TakeoverStageView(request: screen.takeoverRequest, screens: screens, shells: nil,
                                  terminals: terminals, onClose: { dismiss() })
            } else {
                Text("This deck can't show \(screen.displayName)'s screen.")
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        // The title is set once, when the window appears — never from a
        // view body (`WindowChromeTests`).
        .background(WindowFrameAutosave(name: "AgentComputerWindow", title: "\(screen.displayName)'s screen"))
    }
}

/// Saves and restores the window's frame under `name`, lets it go full
/// screen, and titles it once.
struct WindowFrameAutosave: NSViewRepresentable {
    let name: String
    let title: String
    func makeNSView(context: Context) -> NSView { Probe(name: name, title: title) }
    func updateNSView(_ view: NSView, context: Context) {}

    final class Probe: NSView {
        let name: String
        let title: String
        init(name: String, title: String) {
            self.name = name
            self.title = title
            super.init(frame: .zero)
        }
        required init?(coder: NSCoder) { nil }
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, window.frameAutosaveName != name else { return }
            window.setFrameUsingName(name)
            window.setFrameAutosaveName(name)
            window.title = title
            window.collectionBehavior.insert(.fullScreenPrimary)
        }
    }
}

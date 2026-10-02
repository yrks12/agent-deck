import SwiftUI
import AppKit
import SwiftTerm
import DeckKit

/// **The desk's terminal, as a terminal.**
///
/// *"The terminal of each computer should look and feel like a terminal."*
/// SwiftTerm's emulator on `DeskTerminalModel`'s socket: real colours, real
/// scrollback, ⌘C/⌘V, and vim/top/ssh work because the far end is a PTY.
/// A bar over it switches between his shell and the agent's commands, and says
/// what state the socket is in. When the deck or the desk's image has no live
/// terminal, it falls back to the one-command drawer (`DeskTerminalPanel`).
public struct LiveTerminalView: View {
    @StateObject private var model: DeskTerminalModel
    @StateObject private var bridge: DeckTerminalBridgeBox
    private let displayName: String
    private let fallback: DeskShellClient?

    public init(desk: String, displayName: String, streams: DeskTerminalStreaming,
                fallback: DeskShellClient?) {
        let model = DeskTerminalModel(desk: desk, client: streams)
        _model = StateObject(wrappedValue: model)
        _bridge = StateObject(wrappedValue: DeckTerminalBridgeBox(model: model))
        self.displayName = displayName
        self.fallback = fallback
    }

    public var body: some View {
        Group {
            if model.fallsBackToOneShot, let fallback {
                ScrollView {
                    DeskTerminalPanel(desk: model.desk, displayName: displayName, client: fallback)
                        .padding(12)
                }
            } else {
                VStack(spacing: 0) {
                    bar
                    TerminalHost(bridge: bridge.bridge)
                        .padding(.leading, 6)
                        .background(Color(nsColor: DeckTerminalBridge.background))
                }
            }
        }
        .background(Color(nsColor: DeckTerminalBridge.background))
        .environment(\.colorScheme, .dark)
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }

    private var bar: some View {
        HStack(spacing: 8) {
            DeckPillPicker(selection: Binding(
                get: { model.window },
                set: { model.switchWindow(to: $0) }),
                           options: model.windows.map { ($0, $0.title) },
                           label: "Terminal")
            .fixedSize()
            .help("Your own shell on \(displayName)'s computer, or a read-only view of the commands \(displayName) runs")

            if let caption = model.state.caption(desk: displayName) {
                Text(caption)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 4)
            if model.state.offersRestart {
                Button("Reconnect") { model.retry() }
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(.black.opacity(0.35))
    }
}

/// The bridge, kept for the life of the view: an `NSView` rebuilt per SwiftUI
/// pass would lose the screen and the scrollback.
@MainActor
final class DeckTerminalBridgeBox: ObservableObject {
    let bridge: DeckTerminalBridge
    init(model: DeskTerminalModel) { bridge = DeckTerminalBridge(model: model) }
}

struct TerminalHost: NSViewRepresentable {
    let bridge: DeckTerminalBridge
    func makeNSView(context: Context) -> DeckTerminalNSView { bridge.view }
    func updateNSView(_ view: DeckTerminalNSView, context: Context) {}
}

/// Marks a view that takes the keyboard for itself, so the take-over's key
/// forwarding (`TakeoverSurface`) leaves keys alone while it has focus.
protocol TakesTheKeyboard: AnyObject {}

final class DeckTerminalNSView: TerminalView, TakesTheKeyboard {}

/// **SwiftTerm on one side, `DeskTerminalModel` on the other.** Output is fed
/// to the emulator (and only then counted as processed, for the acks); what
/// the emulator wants to send — keys, paste, its replies to queries — is
/// input; its grid is the PTY's.
@MainActor
final class DeckTerminalBridge: NSObject, TerminalViewDelegate, TerminalOutputSink {
    static let background = NSColor(calibratedWhite: 0.07, alpha: 1)
    static let foreground = NSColor(calibratedWhite: 0.88, alpha: 1)

    let model: DeskTerminalModel
    let view: DeckTerminalNSView

    init(model: DeskTerminalModel) {
        self.model = model
        view = DeckTerminalNSView(frame: NSRect(x: 0, y: 0, width: 640, height: 400))
        super.init()
        view.font = NSFont.monospacedSystemFont(ofSize: 12, weight: .regular)
        view.nativeBackgroundColor = Self.background
        view.nativeForegroundColor = Self.foreground
        view.caretColor = .systemGreen
        view.optionAsMetaKey = true
        view.getTerminal().options.scrollback = 10_000
        view.terminalDelegate = self
        model.attach(self)
        let terminal = view.getTerminal()
        model.resize(cols: terminal.cols, rows: terminal.rows)
    }

    // MARK: TerminalOutputSink

    func terminalFeed(_ bytes: Data) {
        view.feed(byteArray: ArraySlice([UInt8](bytes)))
    }

    func terminalReset() {
        view.getTerminal().resetToInitialState()
        view.needsDisplay = true
    }

    // MARK: TerminalViewDelegate

    nonisolated func send(source: TerminalView, data: ArraySlice<UInt8>) {
        let bytes = Data(data)
        MainActor.assumeIsolated { model.send(input: bytes) }
    }

    nonisolated func sizeChanged(source: TerminalView, newCols: Int, newRows: Int) {
        MainActor.assumeIsolated { model.resize(cols: newCols, rows: newRows) }
    }

    nonisolated func setTerminalTitle(source: TerminalView, title: String) {}
    nonisolated func hostCurrentDirectoryUpdate(source: TerminalView, directory: String?) {}
    nonisolated func scrolled(source: TerminalView, position: Double) {}
    nonisolated func bell(source: TerminalView) {}
    nonisolated func clipboardCopy(source: TerminalView, content: Data) {
        guard let text = String(data: content, encoding: .utf8) else { return }
        DispatchQueue.main.async {
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        }
    }
    nonisolated func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    nonisolated func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
        guard let url = URL(string: link), ["http", "https"].contains(url.scheme ?? "") else { return }
        DispatchQueue.main.async { NSWorkspace.shared.open(url) }
    }
}

import SwiftUI
import UIKit
import SwiftTerm
import DeckKit

/// **The desk's terminal on the iPhone, full screen.**
///
/// SwiftTerm's iOS emulator on `DeskTerminalModel` (the same model the Mac
/// uses), a bar with My shell | Agent's commands and the way back to the
/// screen, and over the keyboard the key bar the screen has — esc, tab, a
/// sticky ctrl, arrows, delete, return, paste — sending terminal bytes
/// (`TerminalKey`). Rules are DeckKit's; this file draws and forwards.
struct PhoneTerminalView: View {
    let route: ScreenRoute
    let onScreen: () -> Void
    let close: () -> Void
    @StateObject private var model: DeskTerminalModel
    @StateObject private var box: PhoneTerminalBridgeBox

    init(route: ScreenRoute, streams: DeskTerminalStreaming,
         onScreen: @escaping () -> Void, close: @escaping () -> Void) {
        self.route = route
        self.onScreen = onScreen
        self.close = close
        let model = DeskTerminalModel(desk: route.desk, client: streams)
        _model = StateObject(wrappedValue: model)
        _box = StateObject(wrappedValue: PhoneTerminalBridgeBox(model: model))
    }

    var body: some View {
        VStack(spacing: 0) {
            bar
            PhoneTerminalHost(bridge: box.bridge)
                .padding(.horizontal, 4)
        }
        .background(Color(uiColor: PhoneTerminalBridge.background).ignoresSafeArea())
        .environment(\.colorScheme, .dark)
        .onAppear { model.start() }
        .onDisappear { model.stop() }
    }

    private var bar: some View {
        VStack(spacing: 6) {
            HStack(spacing: 8) {
                Button(action: close) {
                    Image(systemName: "xmark").font(.system(size: 16, weight: .semibold))
                        .frame(width: 34, height: 34)
                }
                .accessibilityLabel("Close")
                Text("\(route.displayName)'s terminal").font(.headline).lineLimit(1)
                Spacer(minLength: 4)
                Button(action: onScreen) {
                    Label("Screen", systemImage: "display").font(.subheadline.weight(.medium))
                }
                .accessibilityLabel("Show \(route.displayName)'s screen")
            }
            Picker("Terminal", selection: Binding(get: { model.window },
                                                  set: { model.switchWindow(to: $0) })) {
                ForEach(model.windows, id: \.self) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            if let caption = model.state.caption(desk: route.displayName) {
                HStack {
                    Text(caption).font(.caption).foregroundStyle(.secondary).lineLimit(2)
                    Spacer()
                    if model.state.offersRestart {
                        Button("Reconnect") { model.retry() }.font(.caption.weight(.semibold))
                    }
                }
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 10).padding(.vertical, 6)
        .background(.ultraThinMaterial)
    }
}

/// The key bar, riding the keyboard as the terminal's input accessory.
private struct TerminalKeyBar: View {
    @ObservedObject var bridge: PhoneTerminalBridge

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 6) {
                Button { bridge.ctrlArmed.toggle() } label: {
                    Text("ctrl").font(.subheadline.weight(.semibold))
                        .frame(minWidth: 44, minHeight: 34)
                        .foregroundStyle(bridge.ctrlArmed ? Color.black : Color.white)
                        .background(bridge.ctrlArmed ? Color.white : Color.white.opacity(0.14),
                                    in: RoundedRectangle(cornerRadius: 8))
                }
                .accessibilityLabel(bridge.ctrlArmed ? "Control, on" : "Control")
                ForEach(TerminalKey.bar, id: \.self) { key in
                    Button { bridge.press(key) } label: {
                        Group {
                            if let symbol = key.symbol { Image(systemName: symbol) } else { Text(key.label) }
                        }
                        .font(.subheadline.weight(.medium))
                        .frame(minWidth: 44, minHeight: 34)
                        .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
                    }
                    .accessibilityLabel(key.label)
                }
                Button { bridge.paste() } label: {
                    Image(systemName: "doc.on.clipboard").font(.subheadline.weight(.medium))
                        .frame(minWidth: 44, minHeight: 34)
                        .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
                }
                .accessibilityLabel("Paste from iPhone")
            }
            .buttonStyle(.plain)
            .foregroundStyle(.white)
            .padding(.horizontal, 8)
        }
        .frame(height: 44)
        .background(Color(white: 0.12))
        .environment(\.colorScheme, .dark)
    }
}

@MainActor
final class PhoneTerminalBridgeBox: ObservableObject {
    let bridge: PhoneTerminalBridge
    init(model: DeskTerminalModel) { bridge = PhoneTerminalBridge(model: model) }
}

private struct PhoneTerminalHost: UIViewRepresentable {
    let bridge: PhoneTerminalBridge
    func makeUIView(context: Context) -> TerminalView { bridge.view }
    func updateUIView(_ view: TerminalView, context: Context) {}
}

/// SwiftTerm on one side, `DeskTerminalModel` on the other — the phone's twin
/// of the Mac's `DeckTerminalBridge`.
@MainActor
final class PhoneTerminalBridge: NSObject, ObservableObject, TerminalViewDelegate, TerminalOutputSink {
    static let background = UIColor(white: 0.07, alpha: 1)

    let model: DeskTerminalModel
    let view: TerminalView
    /// Sticky ctrl: the next character typed is sent as its control code.
    @Published var ctrlArmed = false
    private var keyBarHost: UIViewController?

    init(model: DeskTerminalModel) {
        self.model = model
        view = TerminalView(frame: CGRect(x: 0, y: 0, width: 390, height: 600),
                            font: UIFont.monospacedSystemFont(ofSize: 12, weight: .regular))
        super.init()
        view.nativeBackgroundColor = Self.background
        view.nativeForegroundColor = UIColor(white: 0.88, alpha: 1)
        view.keyboardAppearance = .dark
        view.getTerminal().options.scrollback = 5_000
        view.terminalDelegate = self
        let host = UIHostingController(rootView: TerminalKeyBar(bridge: self))
        host.view.frame = CGRect(x: 0, y: 0, width: 390, height: 44)
        host.view.backgroundColor = .clear
        keyBarHost = host
        view.inputAccessoryView = host.view
        model.attach(self)
        let terminal = view.getTerminal()
        model.resize(cols: terminal.cols, rows: terminal.rows)
    }

    func press(_ key: TerminalKey) {
        ctrlArmed = false
        model.send(input: Data(key.bytes))
    }

    func paste() {
        guard let text = UIPasteboard.general.string, !text.isEmpty else { return }
        model.send(input: Data(text.utf8))
    }

    // MARK: TerminalOutputSink

    func terminalFeed(_ bytes: Data) {
        view.feed(byteArray: ArraySlice([UInt8](bytes)))
    }

    func terminalReset() {
        view.getTerminal().resetToInitialState()
        view.setNeedsDisplay()
    }

    // MARK: TerminalViewDelegate

    nonisolated func send(source: TerminalView, data: ArraySlice<UInt8>) {
        let bytes = Data(data)
        MainActor.assumeIsolated {
            if ctrlArmed, let text = String(data: bytes, encoding: .utf8),
               let code = TerminalKey.control(of: text) {
                ctrlArmed = false
                model.send(input: Data(code))
            } else {
                model.send(input: bytes)
            }
        }
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
        DispatchQueue.main.async { UIPasteboard.general.string = text }
    }
    nonisolated func rangeChanged(source: TerminalView, startY: Int, endY: Int) {}
    nonisolated func requestOpenLink(source: TerminalView, link: String, params: [String: String]) {
        guard let url = URL(string: link), ["http", "https"].contains(url.scheme ?? "") else { return }
        DispatchQueue.main.async { UIApplication.shared.open(url) }
    }
}

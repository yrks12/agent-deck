import SwiftUI
import UIKit
import DeckKit

/// Which desk's computer is open, for `.fullScreenCover(item:)`.
struct ScreenRoute: Identifiable, Equatable {
    let desk: String
    let displayName: String
    /// The desk's own thread, where "take over" and "hand back" are said.
    /// Nil hides the take-over button rather than inventing a thread.
    var threadID: String?
    var id: String { desk }
}

/// **"<Agent>'s computer", as a remote desktop.**
///
/// Full-bleed, either orientation, opened at a readable zoom. Two modes: Touch
/// (click where you tap) and Trackpad (a cursor you push, for small targets),
/// with a loupe while positioning. Long-press / two-finger tap right-click,
/// double-tap double-clicks, two fingers scroll, press-and-drag selects. A
/// keyboard with sticky ⌘⌃⌥⇧, arrows, combos and paste from the iPhone. And
/// "Take over", which tells the agent to keep its hands off while he drives.
///
/// Every rule is DeckKit's and tested there (`PhoneScreen.swift`,
/// `PhoneRemote.swift`); this file draws and forwards touches.
struct AgentScreenView: View {
    let route: ScreenRoute
    let client: AgentScreenClient?
    let messages: DeckClient?
    @Environment(\.dismiss) private var dismiss
    @State private var showing: ComputerView = .screen

    var body: some View {
        Group {
            if let client {
                // Screen | Terminal: the same computer, two ways in. The
                // terminal needs a client that can open the live stream.
                if showing == .terminal, let streams = client as? DeskTerminalStreaming {
                    PhoneTerminalView(route: route, streams: streams,
                                      onScreen: { showing = .screen }, close: { dismiss() })
                } else {
                    AgentScreenStage(route: route, client: client, messages: messages,
                                     // His Mac has a live view, not a desk terminal.
                                     onTerminal: client is DeskTerminalStreaming
                                        && MacScreenName.nodeId(route.desk) == nil ? { showing = .terminal } : nil,
                                     close: { dismiss() })
                }
            } else {
                VStack(spacing: 16) {
                    ContentUnavailableView("No screen on this deck", systemImage: "display",
                                           description: Text("This deck can't show \(route.displayName)'s computer."))
                    Button("Close") { dismiss() }.buttonStyle(.borderedProminent)
                }
            }
        }
        .onAppear {
            OrientationLock.allow(.allButUpsideDown)
            #if DEBUG
            if ProcessInfo.processInfo.environment["DECK_SCREEN_LANDSCAPE"] == "1" {
                Task { try? await Task.sleep(nanoseconds: 800_000_000); OrientationLock.turn(landscape: true) }
            }
            #endif
        }
        .onDisappear { OrientationLock.allow(.portrait) }
    }
}

// MARK: - orientation

/// The app is portrait; the agent's screen may turn. The app delegate asks
/// this for the mask, and the screen view changes it while it is up.
enum OrientationLock {
    @MainActor static var mask: UIInterfaceOrientationMask = .portrait

    @MainActor static func allow(_ next: UIInterfaceOrientationMask) {
        mask = next
        refresh()
        if next == .portrait { request(.portrait) }
    }

    /// Turn the screen without turning the phone: for a phone with rotation
    /// lock on, which is most of them.
    @MainActor static func turn(landscape: Bool) {
        request(landscape ? .landscapeRight : .portrait)
    }

    @MainActor private static func request(_ orientations: UIInterfaceOrientationMask) {
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            scene.requestGeometryUpdate(.iOS(interfaceOrientations: orientations)) { _ in }
        }
    }

    /// Every controller up the presentation chain is asked again: the
    /// screen is a full-screen cover, and the topmost controller is the one
    /// UIKit consults.
    @MainActor private static func refresh() {
        for case let scene as UIWindowScene in UIApplication.shared.connectedScenes {
            var controller = scene.windows.first(where: \.isKeyWindow)?.rootViewController
                ?? scene.windows.first?.rootViewController
            while let current = controller {
                current.setNeedsUpdateOfSupportedInterfaceOrientations()
                controller = current.presentedViewController
            }
        }
    }
}

final class PhoneAppDelegate: NSObject, UIApplicationDelegate {
    func application(_ application: UIApplication,
                     supportedInterfaceOrientationsFor window: UIWindow?) -> UIInterfaceOrientationMask {
        MainActor.assumeIsolated { OrientationLock.mask }
    }
}

// MARK: - the stage

private struct AgentScreenStage: View {
    let route: ScreenRoute
    let messages: DeckClient?
    let close: () -> Void
    var onTerminal: (() -> Void)? = nil
    @State private var viewing: ScreenViewing
    @Environment(\.scenePhase) private var scenePhase

    init(route: ScreenRoute, client: AgentScreenClient, messages: DeckClient?,
         onTerminal: (() -> Void)? = nil, close: @escaping () -> Void) {
        self.onTerminal = onTerminal
        self.route = route
        self.messages = messages
        self.close = close
        _viewing = State(initialValue: ScreenViewing(model: ScreenViewing.phoneModel(
            desk: route.desk, displayName: route.displayName, client: client)))
    }

    var body: some View {
        RemoteCanvas(model: viewing.model, viewing: viewing, route: route, messages: messages, close: close,
                     onTerminal: onTerminal)
            .onAppear { viewing.appeared(sceneActive: scenePhase == .active) }
            .onDisappear { viewing.disappeared() }
            .onChange(of: scenePhase) { _, phase in viewing.scene(active: phase == .active) }
            .task {
                // Drops back to the idle frame rate once his hands are off.
                while !Task.isCancelled {
                    try? await Task.sleep(nanoseconds: 1_000_000_000)
                    viewing.settle()
                }
            }
    }
}

private struct Ripple: Identifiable {
    let id = UUID()
    let x: Double
    let y: Double
    let isRight: Bool
}

private struct RemoteCanvas: View {
    @ObservedObject var model: AgentComputerModel
    let viewing: ScreenViewing
    let route: ScreenRoute
    let messages: DeckClient?
    let close: () -> Void
    /// Switches to the desk's terminal; nil on a deck without one.
    var onTerminal: (() -> Void)? = nil

    @Environment(\.verticalSizeClass) private var verticalSizeClass
    @State private var image: UIImage?
    @State private var shownJPEG: Data?
    @State private var zoom = ScreenZoom.fitted
    @State private var zoomAtStart = ScreenZoom.fitted
    @State private var openedAt: CGSize?
    @State private var mode: RemoteMode = .touch
    @State private var cursor: TrackpadCursor?
    @State private var scrollDrag = ScreenScrollDrag()
    @State private var scrollPoint: DisplayPoint?
    @State private var pressStart: DisplayPoint?
    @State private var pressEnd: DisplayPoint?
    @State private var cursorAtPressStart: TrackpadCursor?
    @State private var loupe: DisplayPoint?
    @State private var loupeView: CGPoint?
    @State private var ripples: [Ripple] = []
    @State private var typing = false
    @State private var buffer = ""
    @State private var swallowNextChange = false
    @State private var mods = StickyModifiers()
    @State private var control: ScreenControl
    @State private var switchingDriver = false
    @State private var chromeShown = true
    @State private var askingAddress = false
    @State private var address = ""
    @State private var notice: String?
    @State private var sendChain: Task<Void, Never>?
    @FocusState private var keyboardUp: Bool
    /// The key bar's top edge and the toolbar's bottom edge, on screen: what
    /// the keyboard leaves of the stage is measured, not assumed.
    @State private var panelTop: CGFloat?
    @State private var barBottom: CGFloat = 0
    /// The last display point he tapped: the field the keyboard is for.
    @State private var focus: DisplayPoint?
    @State private var keyboardZoom = ScreenKeyboardZoom()
    #if DEBUG
    @State private var demoRan = false
    #endif

    init(model: AgentComputerModel, viewing: ScreenViewing, route: ScreenRoute,
         messages: DeckClient?, close: @escaping () -> Void,
         onTerminal: (() -> Void)? = nil) {
        self.onTerminal = onTerminal
        self.model = model
        self.viewing = viewing
        self.route = route
        self.messages = messages
        self.close = close
        _control = State(initialValue: ScreenControl(agentName: route.displayName))
    }

    private var presentation: AgentScreenPresentation { model.presentation }
    private var isLandscape: Bool { verticalSizeClass == .compact }
    private var canTakeOver: Bool { messages != nil && route.threadID != nil }

    var body: some View {
        ZStack(alignment: .top) {
            GeometryReader { geo in
                let laid = visibleArea(in: geo)
                // Laid out round the toolbar, the status line, the key bar
                // and the keyboard: nothing is drawn over the desk's picture.
                VStack(spacing: 0) {
                    Color.clear.frame(height: laid.top)
                    stage(area: laid.area)
                    // Always reserved: a line coming and going must not resize
                    // the stage, which would reset his zoom.
                    statusLine.frame(maxWidth: .infinity)
                        .frame(height: ScreenStageLayout.statusHeight)
                    Spacer(minLength: 0)
                }
            }
            .ignoresSafeArea()

            if chromeShown || !isLandscape {
                topBar
            } else {
                showChromeButton
            }
        }
        .background(Color.black.ignoresSafeArea())
        .safeAreaInset(edge: .bottom, spacing: 0) { if typing { keyboardPanel } }
        .statusBarHidden(isLandscape)
        .persistentSystemOverlays(isLandscape ? .hidden : .automatic)
        .preferredColorScheme(.dark)
        .onChange(of: presentation.frame?.jpeg) { _, jpeg in decode(jpeg) }
        .onChange(of: isLandscape) { _, landscape in chromeShown = !landscape }
        .alert("Open a web address", isPresented: $askingAddress) {
            TextField("example.com", text: $address)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .keyboardType(.URL)
            Button("Go") { send(BrowserActions.open(address)); address = "" }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("Opens it in the browser on \(route.displayName)'s computer.")
        }
    }

    // MARK: picture

    private func visibleArea(in geo: GeometryProxy) -> (top: Double, area: ScreenVisibleArea) {
        let frame = geo.frame(in: .global)
        let covered = typing ? panelTop.map { frame.maxY - $0 } ?? 0 : 0
        let top = (chromeShown || !isLandscape) ? barBottom - frame.minY : 0
        return ScreenStageLayout.uncovered(viewWidth: frame.width, viewHeight: frame.height,
                                           coveredTop: top, coveredBottom: covered,
                                           statusHeight: ScreenStageLayout.statusHeight)
    }

    @ViewBuilder
    private func stage(area: ScreenVisibleArea) -> some View {
        let size = CGSize(width: area.width, height: area.stageHeight)
        let fit = model.fit(inViewWidth: size.width, height: size.height)
        ZStack {
            Color.black
            if let image {
                Image(uiImage: image)
                    .resizable()
                    .interpolation(.high)
                    .aspectRatio(contentMode: .fit)
                    .frame(width: size.width, height: size.height)
                    .scaleEffect(zoom.scale)
                    .offset(x: zoom.offsetX, y: zoom.offsetY)
                    .opacity(presentation.isFrameLive ? 1 : 0.6)
                    .accessibilityLabel(presentation.spokenLabel)
            } else {
                VStack(spacing: 10) {
                    Image(systemName: presentation.symbol ?? "display").font(.system(size: 34))
                    Text(presentation.spokenLabel)
                        .font(.callout)
                        .multilineTextAlignment(.center)
                        .padding(.horizontal, 32)
                }
                .foregroundStyle(.white.opacity(0.8))
            }

            if let fit {
                ForEach(ripples) { ripple in
                    RippleView(isRight: ripple.isRight).position(x: ripple.x, y: ripple.y)
                }
                if mode == .trackpad, let cursor {
                    let at = zoom.viewPoint(ofDisplayX: cursor.x, y: cursor.y, in: fit)
                    CursorArrow().position(x: at.x + 7, y: at.y + 10).allowsHitTesting(false)
                }
                if let loupe, let image, let anchor = loupeView {
                    LoupeView(image: image, point: loupe,
                              displayWidth: fit.displayWidth, displayHeight: fit.displayHeight)
                        .position(x: min(max(anchor.x, 70), size.width - 70),
                                  y: anchor.y > 170 ? anchor.y - 100 : anchor.y + 100)
                        .allowsHitTesting(false)
                }
            }

            RemoteGestureLayer(handlers: handlers(size: size))
        }
        .frame(width: size.width, height: size.height)
        .clipped()
        .onChange(of: fit) { _, fit in openIfNeeded(fit, size, area) }
        .onChange(of: size) { _, size in
            openIfNeeded(model.fit(inViewWidth: size.width, height: size.height), size, area)
        }
        .onAppear { openIfNeeded(fit, size, area) }
    }

    /// Readable zoom the first time a picture has a size, and again on
    /// rotation. The keyboard coming up centres the field he tapped above it;
    /// going down gives back the zoom he had.
    private func openIfNeeded(_ fit: ScreenFit?, _ size: CGSize, _ area: ScreenVisibleArea) {
        guard let fit, openedAt != size else { return }
        let previous = openedAt
        openedAt = size
        if area.isCovered, let previous {
            withAnimation(.easeOut(duration: 0.25)) {
                zoom = keyboardZoom.shown(current: zoom, currentWidth: previous.width,
                                          currentHeight: previous.height, focus: focus, fit: fit, area: area)
            }
        } else if let back = keyboardZoom.hiddenIfUp(fit: fit, viewWidth: size.width, viewHeight: size.height) {
            withAnimation(.easeOut(duration: 0.25)) { zoom = back }
        } else {
            zoom = .opening(fit: fit, viewWidth: size.width, viewHeight: size.height)
        }
        if cursor == nil { cursor = TrackpadCursor(displayWidth: fit.displayWidth, displayHeight: fit.displayHeight) }
        #if DEBUG
        demoOnce(fit, size)
        #endif
    }

    #if DEBUG
    /// `ScreenDemo`'s scripted tap and keyboard, once, for simulator screenshots.
    private func demoOnce(_ fit: ScreenFit, _ size: CGSize) {
        guard ScreenDemo.isOn, !demoRan else { return }
        demoRan = true
        Task {
            try? await Task.sleep(nanoseconds: 600_000_000)
            if let p = ScreenDemo.tap {
                let at = zoom.viewPoint(ofDisplayX: Double(p.x), y: Double(p.y), in: fit)
                fire(.tap, at: CGPoint(x: at.x, y: at.y), size: size)
            }
            if ScreenDemo.opensKeyboard {
                try? await Task.sleep(nanoseconds: 400_000_000)
                typing = true
                keyboardUp = true
            }
            if ScreenDemo.closesKeyboard {
                try? await Task.sleep(nanoseconds: 2_000_000_000)
                typing = false
                keyboardUp = false
            }
        }
    }
    #endif

    /// Swap the picture only once the next one is decoded, so a frame never
    /// flashes half-drawn; an identical frame is not redrawn at all.
    private func decode(_ jpeg: Data?) {
        guard let jpeg, jpeg != shownJPEG else { return }
        shownJPEG = jpeg
        // The model already decoded it off the main thread (a streamed or a
        // polled frame): wrap those pixels rather than decode them again.
        if let frame = presentation.frame, frame.jpeg == jpeg, let pixels = frame.decoded {
            image = UIImage(cgImage: pixels.cgImage)
            return
        }
        Task.detached(priority: .userInitiated) {
            let decoded = UIImage(data: jpeg)?.preparingForDisplay()
            await MainActor.run {
                if let decoded, shownJPEG == jpeg { image = decoded }
            }
        }
    }

    // MARK: chrome

    private var topBar: some View {
        HStack(spacing: 2) {
            chromeButton("xmark", "Close") { closeScreen() }
            Text(route.displayName)
                .font(.subheadline.weight(.semibold))
                .lineLimit(1)
                .fixedSize()
                .padding(.horizontal, 4)
            if canTakeOver {
                Button { toggleDriver() } label: {
                    // Never "T…": the whole title, a shorter one, or the icon.
                    ViewThatFits(in: .horizontal) {
                        takeOverPill(Label(control.buttonTitle, systemImage: control.symbol))
                        takeOverPill(Label(control.shortButtonTitle, systemImage: control.symbol))
                        takeOverPill(Image(systemName: control.symbol))
                    }
                }
                .buttonStyle(.plain)
                .disabled(switchingDriver)
                .accessibilityLabel(control.buttonTitle)
            }
            Spacer(minLength: 4)
            if let onTerminal {
                chromeButton("terminal", "Show \(route.displayName)'s terminal", action: onTerminal)
            }
            chromeButton(mode == .touch ? "hand.point.up.left" : "cursorarrow.rays",
                         mode == .touch ? "Touch mode. Switch to trackpad" : "Trackpad mode. Switch to touch") {
                mode = mode == .touch ? .trackpad : .touch
                Haptics.tap()
            }
            Menu {
                Button { askingAddress = true } label: { Label("Open a web address…", systemImage: "globe") }
                ForEach([RemoteCombo.back, .forward, .reload, .newTab, .closeTab], id: \.self) { combo in
                    Button { send([combo.input]) } label: { Label(combo.title, systemImage: combo.symbol) }
                }
            } label: {
                chromeIcon("globe")
            }
            .accessibilityLabel("Browser")
            chromeButton(zoom.isZoomed ? "arrow.down.right.and.arrow.up.left" : "plus.magnifyingglass",
                         zoom.isZoomed ? "Show the whole screen" : "Zoom to readable") { toggleZoom() }
            chromeButton(typing ? "keyboard.chevron.compact.down" : "keyboard",
                         typing ? "Hide keyboard" : "Keyboard") {
                typing.toggle()
                keyboardUp = typing
            }
            chromeButton(isLandscape ? "rectangle.portrait.rotate" : "rectangle.landscape.rotate",
                         isLandscape ? "Turn to portrait" : "Turn to landscape") {
                OrientationLock.turn(landscape: !isLandscape)
            }
            if isLandscape {
                chromeButton("chevron.up", "Hide controls") { chromeShown = false }
            }
        }
        .foregroundStyle(.white)
        .padding(.horizontal, 8).padding(.vertical, 5)
        .background(.ultraThinMaterial, in: Capsule())
        .environment(\.colorScheme, .dark)
        .padding(.horizontal, 8)
        .padding(.top, isLandscape ? 6 : 0)
        .onGeometryChange(for: CGFloat.self) { $0.frame(in: .global).maxY } action: { barBottom = $0 }
    }

    private func takeOverPill<Content: View>(_ content: Content) -> some View {
        content
            .font(.caption.weight(.semibold))
            .lineLimit(1)
            .fixedSize()
            .padding(.horizontal, 10).frame(height: 32)
            .background(Capsule().fill(control.driver == .owner ? Color.orange : Color.white.opacity(0.14)))
    }

    private var showChromeButton: some View {
        HStack {
            Spacer()
            chromeButton("chevron.down", "Show controls") { chromeShown = true }
                .background(.ultraThinMaterial, in: Circle())
                .environment(\.colorScheme, .dark)
        }
        .padding(.horizontal, 12).padding(.top, 6)
    }

    private func chromeIcon(_ symbol: String) -> some View {
        Image(systemName: symbol)
            .font(.system(size: 16, weight: .semibold))
            .frame(width: 34, height: 34)
            .contentShape(Rectangle())
    }

    private func chromeButton(_ symbol: String, _ label: String, action: @escaping () -> Void) -> some View {
        Button(action: action) { chromeIcon(symbol) }
            .buttonStyle(.plain)
            .foregroundStyle(.white)
            .accessibilityLabel(label)
    }

    private var statusText: String? {
        notice ?? model.lastInputRefusal?.shortLine ?? presentation.statusLine
    }

    /// Its own row under the picture, never over it.
    @ViewBuilder
    private var statusLine: some View {
        let refusal = model.lastInputRefusal
        if let line = statusText {
            Text(line)
                .font(.caption.weight(.medium))
                .foregroundStyle(.white)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(Capsule().fill(refusal != nil || notice != nil ? Color.red.opacity(0.85)
                                                                          : Color.black.opacity(0.6)))
                .accessibilityLabel(notice ?? refusal?.sentence ?? presentation.spokenLabel)
        }
    }

    // MARK: keyboard

    private var keyboardPanel: some View {
        VStack(spacing: 6) {
            if isLandscape {
                // One row in landscape: the keyboard already takes most of a
                // 402pt-tall screen, and every row here is screen he can't see.
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        namedKeys
                        modifierKeys
                        pasteButton.frame(width: 96)
                        combosMenu.frame(width: 96)
                    }
                    .padding(.horizontal, 10)
                }
            } else {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) { namedKeys }
                        .padding(.horizontal, 10)
                }
                HStack(spacing: 6) {
                    modifierKeys
                    pasteButton
                    combosMenu
                }
                .padding(.horizontal, 10)
            }
            TextField("Type — it goes straight to \(route.displayName)'s screen", text: $buffer)
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .focused($keyboardUp)
                .submitLabel(.return)
                .onSubmit {
                    send([mods.chord(RemoteKeys.enter)])
                    swallowNextChange = !buffer.isEmpty
                    buffer = ""
                    keyboardUp = true
                }
                .onChange(of: buffer) { old, new in typed(from: old, to: new) }
                .padding(.horizontal, 12).padding(.vertical, 8)
                .background(Color.white.opacity(0.1), in: RoundedRectangle(cornerRadius: 10))
                .padding(.horizontal, 10)
        }
        .foregroundStyle(.white)
        .padding(.vertical, 8)
        .background(.ultraThinMaterial)
        .environment(\.colorScheme, .dark)
        .onGeometryChange(for: CGFloat.self) { $0.frame(in: .global).minY } action: { panelTop = $0 }
    }

    @ViewBuilder
    private var namedKeys: some View {
        key("esc", RemoteKeys.escape)
        key("tab", RemoteKeys.tab)
        keySymbol("arrow.left", "Left", RemoteKeys.left)
        keySymbol("arrow.up", "Up", RemoteKeys.up)
        keySymbol("arrow.down", "Down", RemoteKeys.down)
        keySymbol("arrow.right", "Right", RemoteKeys.right)
        keySymbol("delete.left", "Delete", RemoteKeys.backspace)
        keySymbol("return", "Enter", RemoteKeys.enter)
    }

    @ViewBuilder
    private var modifierKeys: some View {
        modifier(.command, "⌘")
        modifier(.control, "⌃")
        modifier(.option, "⌥")
        modifier(.shift, "⇧")
    }

    private var pasteButton: some View {
        Button {
            let pasted = ScreenTyping.paste(UIPasteboard.general.string)
            if pasted.isEmpty { flash("The iPhone clipboard has no text") } else { send(pasted) }
        } label: {
            Label("Paste", systemImage: "doc.on.clipboard").font(.subheadline.weight(.medium))
                .frame(maxWidth: .infinity, minHeight: 34)
                .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("Paste from iPhone")
    }

    private var combosMenu: some View {
        Menu {
            ForEach(RemoteCombo.allCases, id: \.self) { combo in
                Button { send([combo.input]) } label: { Label(combo.title, systemImage: combo.symbol) }
            }
        } label: {
            Text("Combos").font(.subheadline.weight(.medium))
                .frame(maxWidth: .infinity, minHeight: 34)
                .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        }
    }

    private func typed(from old: String, to new: String) {
        if swallowNextChange { swallowNextChange = false; return }
        viewing.interacted()
        if mods.isAnyOn, new.count > old.count, new.hasPrefix(old) {
            // ⌘ then a letter is a chord, not a letter in the field.
            send(mods.typed(String(new.dropFirst(old.count))))
            swallowNextChange = true
            buffer = old
            return
        }
        send(LiveTyping.inputs(from: old, to: new))
        if new.count > 200 { swallowNextChange = true; buffer = "" }
    }

    private func key(_ label: String, _ keysym: String) -> some View {
        Button { send([mods.chord(keysym)]) } label: {
            Text(label).font(.subheadline.weight(.medium))
                .frame(minWidth: 44, minHeight: 34)
                .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func keySymbol(_ symbol: String, _ label: String, _ keysym: String) -> some View {
        Button { send([mods.chord(keysym)]) } label: {
            Image(systemName: symbol).font(.subheadline.weight(.medium))
                .frame(minWidth: 44, minHeight: 34)
                .background(Color.white.opacity(0.14), in: RoundedRectangle(cornerRadius: 8))
        }
        .buttonStyle(.plain)
        .accessibilityLabel(label)
    }

    private func modifier(_ key: StickyModifiers.Key, _ glyph: String) -> some View {
        let on = mods.isOn(key)
        return Button { mods.tap(key); Haptics.tap() } label: {
            Text(glyph).font(.system(size: 18, weight: .semibold))
                .frame(width: 44, height: 34)
                .foregroundStyle(on ? Color.black : Color.white)
                .background(RoundedRectangle(cornerRadius: 8).fill(on ? Color.white : Color.white.opacity(0.14)))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(mods.isLocked(key) ? Color.orange : .clear, lineWidth: 2))
        }
        .buttonStyle(.plain)
        .accessibilityLabel("\(key.rawValue)\(on ? (mods.isLocked(key) ? ", locked" : ", on") : "")")
    }

    // MARK: sending

    /// One gesture after another, in the order he made them.
    private func send(_ inputs: [ScreenInput]) {
        guard !inputs.isEmpty else { return }
        viewing.interacted()
        let previous = sendChain
        let model = self.model
        sendChain = Task {
            await previous?.value
            for input in inputs { await model.send(input) }
            // A fresh picture the moment it lands, not on the next tick: a
            // frame costs ~0.7s over the VPN (measured), so waiting for the
            // poll is a second of not knowing whether the tap worked.
            await model.pollOnce()
        }
    }

    private func flash(_ text: String) {
        notice = text
        Task { try? await Task.sleep(nanoseconds: 2_500_000_000); notice = nil }
    }

    private func ripple(atViewX x: Double, y: Double, right: Bool = false) {
        let r = Ripple(x: x, y: y, isRight: right)
        ripples.append(r)
        Task {
            try? await Task.sleep(nanoseconds: 600_000_000)
            ripples.removeAll { $0.id == r.id }
        }
    }

    // MARK: take over

    private func toggleDriver() {
        guard let messages, let threadID = route.threadID else { return }
        let taking = control.driver == .agent
        let text = taking ? control.takeOverMessage : control.handBackMessage
        switchingDriver = true
        Haptics.tap()
        Task {
            do {
                _ = try await messages.send(threadID: threadID, text: text)
                if taking { control.tookOver() } else { control.handedBack() }
                Haptics.success()
            } catch {
                flash("Couldn't tell \(route.displayName): \((error as? DeckError)?.userFacingText ?? error.localizedDescription)")
                Haptics.failure()
            }
            switchingDriver = false
        }
    }

    private func closeScreen() {
        if let text = control.messageOnClose, let messages, let threadID = route.threadID {
            Task { _ = try? await messages.send(threadID: threadID, text: text) }
        }
        close()
    }

    private func toggleZoom() {
        guard let size = openedAt, let fit = model.fit(inViewWidth: size.width, height: size.height) else { return }
        withAnimation(.easeOut(duration: 0.2)) {
            zoom = zoom.isZoomed ? .fitted : .opening(fit: fit, viewWidth: size.width, viewHeight: size.height)
        }
    }

    // MARK: gestures (view coordinates in, display pixels out — all mapping in DeckKit)

    private func handlers(size: CGSize) -> RemoteGestureLayer.Handlers {
        RemoteGestureLayer.Handlers(
            gesture: { gesture, point in fire(gesture, at: point, size: size) },
            pinch: { phase, scale in pinch(phase, scale, size: size) },
            pan: { phase, delta, total in pan(phase, delta: delta, total: total, size: size) },
            press: { phase, point, delta in press(phase, point: point, delta: delta, size: size) },
            scroll: { phase, start, t in scroll(phase, start, t, size: size) })
    }

    private func fire(_ gesture: RemoteGesture, at point: CGPoint, size: CGSize) {
        guard let fit = model.fit(inViewWidth: size.width, height: size.height),
              let input = RemoteGestures.input(for: gesture, mode: mode, atViewX: point.x, y: point.y,
                                               cursor: cursor, fit: fit, zoom: zoom) else { return }
        Haptics.tap()
        focus = mode == .trackpad ? cursor?.point : zoom.displayPoint(atViewX: point.x, y: point.y, in: fit)
        let right = gesture == .longPress || gesture == .twoFingerTap
        if mode == .trackpad, let cursor {
            let at = zoom.viewPoint(ofDisplayX: cursor.x, y: cursor.y, in: fit)
            ripple(atViewX: at.x, y: at.y, right: right)
        } else {
            ripple(atViewX: point.x, y: point.y, right: right)
        }
        send([input])
    }

    private func pinch(_ phase: UIGestureRecognizer.State, _ scale: CGFloat, size: CGSize) {
        guard let fit = model.fit(inViewWidth: size.width, height: size.height) else { return }
        viewing.interacted()
        switch phase {
        case .began:
            zoomAtStart = zoom
        case .changed, .ended:
            var next = zoomAtStart
            next.scale = zoomAtStart.scale * scale
            let ratio = next.scale / max(zoomAtStart.scale, 0.001)
            next.offsetX = zoomAtStart.offsetX * ratio
            next.offsetY = zoomAtStart.offsetY * ratio
            zoom = next.clamped(to: fit, viewWidth: size.width, viewHeight: size.height)
        default:
            zoom = zoom.clamped(to: fit, viewWidth: size.width, viewHeight: size.height)
        }
    }

    /// One finger dragged: in touch mode it moves a zoomed picture; in
    /// trackpad mode it pushes the cursor, with the loupe, and the picture
    /// follows the cursor. The pointer is moved on the agent's machine when
    /// the finger lifts, so hovers work.
    private func pan(_ phase: UIGestureRecognizer.State, delta: CGSize, total: CGSize, size: CGSize) {
        guard let fit = model.fit(inViewWidth: size.width, height: size.height) else { return }
        viewing.interacted()
        switch mode {
        case .touch:
            switch phase {
            case .began: zoomAtStart = zoom
            case .changed, .ended:
                guard zoomAtStart.isZoomed else { return }
                var next = zoomAtStart
                next.offsetX += total.width
                next.offsetY += total.height
                zoom = next.clamped(to: fit, viewWidth: size.width, viewHeight: size.height)
            default: break
            }
        case .trackpad:
            guard var c = cursor else { return }
            switch phase {
            case .began, .changed:
                c.move(byViewDX: delta.width, dy: delta.height, fit: fit, zoom: zoom)
                cursor = c
                zoom = zoom.following(displayX: c.x, y: c.y, in: fit, viewWidth: size.width, viewHeight: size.height)
                showLoupe(at: c.point, fit: fit)
            default:
                loupe = nil
                send([.move(x: c.point.x, y: c.point.y)])
            }
        }
    }

    /// Held still, then released or dragged: right-click in place, or a drag
    /// that selects text or moves a window.
    private func press(_ phase: UIGestureRecognizer.State, point: CGPoint, delta: CGSize, size: CGSize) {
        guard let fit = model.fit(inViewWidth: size.width, height: size.height) else { return }
        viewing.interacted()
        switch phase {
        case .began:
            Haptics.tap()
            if mode == .trackpad, let c = cursor {
                pressStart = c.point
                pressEnd = c.point
            } else {
                pressStart = zoom.displayPoint(atViewX: point.x, y: point.y, in: fit)
                pressEnd = pressStart
            }
            if let p = pressStart { showLoupe(at: p, fit: fit) }
        case .changed:
            if mode == .trackpad, var c = cursor {
                c.move(byViewDX: delta.width, dy: delta.height, fit: fit, zoom: zoom)
                cursor = c
                zoom = zoom.following(displayX: c.x, y: c.y, in: fit, viewWidth: size.width, viewHeight: size.height)
                pressEnd = c.point
            } else if let p = zoom.displayPoint(atViewX: point.x, y: point.y, in: fit) {
                pressEnd = p
            }
            if let p = pressEnd { showLoupe(at: p, fit: fit) }
        case .ended:
            loupe = nil
            if let start = pressStart, let end = pressEnd {
                let input = RemoteGestures.pressEnded(from: start, to: end)
                focus = end
                let at = zoom.viewPoint(ofDisplayX: Double(end.x), y: Double(end.y), in: fit)
                ripple(atViewX: at.x, y: at.y, right: start == end)
                send([input])
            }
            pressStart = nil; pressEnd = nil
        default:
            loupe = nil; pressStart = nil; pressEnd = nil
        }
    }

    private func showLoupe(at point: DisplayPoint, fit: ScreenFit) {
        loupe = point
        let at = zoom.viewPoint(ofDisplayX: Double(point.x), y: Double(point.y), in: fit)
        loupeView = CGPoint(x: at.x, y: at.y)
    }

    private func scroll(_ phase: UIGestureRecognizer.State, _ start: CGPoint, _ t: CGSize, size: CGSize) {
        viewing.interacted()
        switch phase {
        case .began:
            scrollDrag = ScreenScrollDrag()
            guard let fit = model.fit(inViewWidth: size.width, height: size.height) else { scrollPoint = nil; return }
            if mode == .trackpad, let cursor {
                scrollPoint = cursor.point
            } else {
                scrollPoint = zoom.displayPoint(atViewX: start.x, y: start.y, in: fit)
                    // Fingers on the letterbox still scroll the middle of the screen.
                    ?? DisplayPoint(x: fit.displayWidth / 2, y: fit.displayHeight / 2)
            }
        case .changed:
            guard let at = scrollPoint,
                  let input = scrollDrag.input(at: at, translationX: t.width, translationY: t.height) else { return }
            send([input])
        default:
            scrollPoint = nil
        }
    }
}

// MARK: - small views

private struct RippleView: View {
    let isRight: Bool
    @State private var grown = false
    var body: some View {
        Circle()
            .strokeBorder(isRight ? Color.orange : Color.white, lineWidth: 2.5)
            .background(Circle().fill((isRight ? Color.orange : Color.white).opacity(grown ? 0 : 0.35)))
            .frame(width: 44, height: 44)
            .scaleEffect(grown ? 1.4 : 0.3)
            .opacity(grown ? 0 : 1)
            .allowsHitTesting(false)
            .onAppear { withAnimation(.easeOut(duration: 0.45)) { grown = true } }
    }
}

private struct CursorArrow: View {
    var body: some View {
        Image(systemName: "cursorarrow")
            .font(.system(size: 22, weight: .regular))
            .foregroundStyle(.white)
            .shadow(color: .black, radius: 1.5)
            .shadow(color: .black.opacity(0.6), radius: 3)
    }
}

/// The picture under the finger or cursor, magnified, with a crosshair on
/// the exact pixel that will be clicked.
private struct LoupeView: View {
    let image: UIImage
    let point: DisplayPoint
    let displayWidth: Int
    let displayHeight: Int
    private let size: CGFloat = 120
    private let pointsPerPixel: CGFloat = 2

    var body: some View {
        let w = CGFloat(displayWidth) * pointsPerPixel
        let h = CGFloat(displayHeight) * pointsPerPixel
        ZStack {
            Color.black
            Image(uiImage: image)
                .resizable()
                .interpolation(.none)
                .frame(width: w, height: h)
                .position(x: size / 2 + w / 2 - CGFloat(point.x) * pointsPerPixel,
                          y: size / 2 + h / 2 - CGFloat(point.y) * pointsPerPixel)
            Path { p in
                p.move(to: CGPoint(x: size / 2 - 10, y: size / 2)); p.addLine(to: CGPoint(x: size / 2 + 10, y: size / 2))
                p.move(to: CGPoint(x: size / 2, y: size / 2 - 10)); p.addLine(to: CGPoint(x: size / 2, y: size / 2 + 10))
            }
            .stroke(Color.red, lineWidth: 1.5)
        }
        .frame(width: size, height: size)
        .clipShape(Circle())
        .overlay(Circle().strokeBorder(Color.white, lineWidth: 2))
        .shadow(radius: 6)
    }
}

// MARK: - touches, from UIKit

/// SwiftUI has no two-finger drag or two-finger tap, and cannot make a single
/// tap wait for a double. UIKit's recognisers can, so the stage's touches are
/// read here and handed back as view coordinates.
private struct RemoteGestureLayer: UIViewRepresentable {
    struct Handlers {
        var gesture: (RemoteGesture, CGPoint) -> Void
        var pinch: (UIGestureRecognizer.State, CGFloat) -> Void
        /// Phase, delta since the last call, total translation.
        var pan: (UIGestureRecognizer.State, CGSize, CGSize) -> Void
        /// Phase, finger location, delta since the last call.
        var press: (UIGestureRecognizer.State, CGPoint, CGSize) -> Void
        var scroll: (UIGestureRecognizer.State, CGPoint, CGSize) -> Void
    }

    var handlers: Handlers

    func makeCoordinator() -> Coordinator { Coordinator(handlers) }

    func makeUIView(context: Context) -> UIView {
        let view = UIView()
        view.backgroundColor = .clear
        view.isMultipleTouchEnabled = true
        let c = context.coordinator

        let doubleTap = UITapGestureRecognizer(target: c, action: #selector(Coordinator.doubleTapped(_:)))
        doubleTap.numberOfTapsRequired = 2
        let tap = UITapGestureRecognizer(target: c, action: #selector(Coordinator.tapped(_:)))
        tap.require(toFail: doubleTap)
        let twoFingerTap = UITapGestureRecognizer(target: c, action: #selector(Coordinator.twoFingerTapped(_:)))
        twoFingerTap.numberOfTouchesRequired = 2
        let press = UILongPressGestureRecognizer(target: c, action: #selector(Coordinator.pressed(_:)))
        press.minimumPressDuration = 0.4
        let pan = UIPanGestureRecognizer(target: c, action: #selector(Coordinator.panned(_:)))
        pan.minimumNumberOfTouches = 1
        pan.maximumNumberOfTouches = 1
        pan.require(toFail: press)
        let pinch = UIPinchGestureRecognizer(target: c, action: #selector(Coordinator.pinched(_:)))
        let scroll = UIPanGestureRecognizer(target: c, action: #selector(Coordinator.scrolled(_:)))
        scroll.minimumNumberOfTouches = 2
        scroll.maximumNumberOfTouches = 2
        for recogniser in [doubleTap, tap, twoFingerTap, press, pan, pinch, scroll] as [UIGestureRecognizer] {
            recogniser.delegate = c
            view.addGestureRecognizer(recogniser)
        }
        c.pinch = pinch
        c.scroll = scroll
        return view
    }

    func updateUIView(_ uiView: UIView, context: Context) {
        context.coordinator.handlers = handlers
    }

    final class Coordinator: NSObject, UIGestureRecognizerDelegate {
        var handlers: Handlers
        weak var pinch: UIPinchGestureRecognizer?
        weak var scroll: UIPanGestureRecognizer?
        private enum Mode { case undecided, zooming, scrolling }
        /// Two fingers are either a pinch or a scroll; whichever moves first
        /// by a clear margin owns them until they lift.
        private var twoFingerMode: Mode = .undecided
        private var scrollStart: CGPoint = .zero
        private var lastPan: CGPoint = .zero
        private var lastPress: CGPoint = .zero

        init(_ handlers: Handlers) { self.handlers = handlers }

        @objc func tapped(_ g: UITapGestureRecognizer) {
            if g.state == .ended { handlers.gesture(.tap, g.location(in: g.view)) }
        }

        @objc func doubleTapped(_ g: UITapGestureRecognizer) {
            if g.state == .ended { handlers.gesture(.doubleTap, g.location(in: g.view)) }
        }

        @objc func twoFingerTapped(_ g: UITapGestureRecognizer) {
            if g.state == .ended { handlers.gesture(.twoFingerTap, g.location(in: g.view)) }
        }

        @objc func pressed(_ g: UILongPressGestureRecognizer) {
            let at = g.location(in: g.view)
            let delta = CGSize(width: at.x - lastPress.x, height: at.y - lastPress.y)
            lastPress = at
            handlers.press(g.state, at, g.state == .began ? .zero : delta)
        }

        @objc func panned(_ g: UIPanGestureRecognizer) {
            let t = g.translation(in: g.view)
            if g.state == .began { lastPan = .zero }
            let delta = CGSize(width: t.x - lastPan.x, height: t.y - lastPan.y)
            lastPan = t
            handlers.pan(g.state, delta, CGSize(width: t.x, height: t.y))
        }

        @objc func pinched(_ g: UIPinchGestureRecognizer) {
            switch g.state {
            case .began:
                handlers.pinch(.began, 1)
            case .changed:
                if twoFingerMode == .undecided, abs(g.scale - 1) > 0.08 { twoFingerMode = .zooming }
                if twoFingerMode == .zooming { handlers.pinch(.changed, g.scale) }
            default:
                if twoFingerMode == .zooming { handlers.pinch(.ended, g.scale) }
                handlers.pinch(.cancelled, 1)
                resetIfLifted()
            }
        }

        @objc func scrolled(_ g: UIPanGestureRecognizer) {
            let t = g.translation(in: g.view)
            switch g.state {
            case .began:
                scrollStart = g.location(in: g.view)
                handlers.scroll(.began, scrollStart, .zero)
            case .changed:
                if twoFingerMode == .undecided, hypot(t.x, t.y) > 14 { twoFingerMode = .scrolling }
                if twoFingerMode == .scrolling { handlers.scroll(.changed, scrollStart, CGSize(width: t.x, height: t.y)) }
            default:
                handlers.scroll(.ended, scrollStart, .zero)
                resetIfLifted()
            }
        }

        private func resetIfLifted() {
            let busy = [pinch?.state, scroll?.state].contains { $0 == .began || $0 == .changed }
            if !busy { twoFingerMode = .undecided }
        }

        func gestureRecognizer(_ g: UIGestureRecognizer,
                               shouldRecognizeSimultaneouslyWith other: UIGestureRecognizer) -> Bool {
            (g === pinch && other === scroll) || (g === scroll && other === pinch)
        }
    }
}

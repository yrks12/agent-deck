import SwiftUI
import AppKit
import DeckKit

/// **"<Agent>'s screen" — the slot that currently holds a developer's string.**
///
/// The inspector draws
/// `Working in /home/agentdeck/.claude/agent-bus/workspaces/new-hire-77ec17`
/// where the reference product draws a live thumbnail of the agent's browser,
/// captioned `COS's screen`, with an **Open** button revealed on hover. This is
/// that panel. The whole server side of it has been deployed the whole time
/// (`docs/client-api.md` §15) and no line of this app had ever called it.
///
/// Three rules this file lives under, each measured in
/// `AgentScreenPanelTests`:
///
/// **1. It is a value, and it is quiet.** `DeckStore.composerDraft` is
/// `@Published`, so one character typed into the conversation is one publish on
/// the store, and a view that observes the store re-runs its body on every one
/// — counted elsewhere in this repo at 21 evaluations across 20 keystrokes
/// against 1 for the equatable shape. This panel holds a JPEG and refreshes
/// once a second in an always-open inspector, so it is the worst possible place
/// to put that back. `AgentScreenPanelBody` therefore takes values, is
/// `Equatable`, is handed over with `.equatable()`, and excludes its closures
/// from `==`. Its own model publishes to it and to nothing else — the store is
/// never touched by the poll.
///
/// **2. Nothing here animates while it waits.** The reference draws a spinner
/// for `Connecting` (`ref-03`). This app deleted every indeterminate
/// `ProgressView` after four measured runs against a real deck: an AppKit
/// animation drives the window's display cycle and everything sharing that
/// window is re-measured on every frame, which is how the Mac ended up pegging
/// a core. The word "Connecting" is kept; the spin is not.
///
/// **3. A dead frame is never drawn as a live one.** `server/screen.py`: *"a
/// frame without an age is a lie — a checkout page looks the same a second old
/// and twenty minutes dead."* A frame past the deck's own `stale_after` is
/// dimmed and captioned with how old it is, and it says so out loud too.
public struct AgentScreenPanel: View {
    /// Its own model, deliberately not the store. `@StateObject` so the poll
    /// survives the parent redrawing and dies with the panel.
    @StateObject private var model: AgentComputerModel
    @State private var isHovering = false

    /// Whether this app is the one the owner is using. `.inactive` here is the
    /// window going background, and it is half of the poll's gate: a 1 Hz JPEG
    /// poll behind another app's window is a battery and bandwidth leak, and
    /// each frame is an `ffmpeg` grab on the box.
    @Environment(\.controlActiveState) private var activeState

    /// `workspace` is the desk's folder, home-relative — `Agent.shortWorkspace`.
    /// It is the fact the raw `Working in …` line carried, and this panel keeps
    /// it off the face and in reach rather than dropping it.
    private let workspace: String?

    /// **The panel does not present the take-over any more; it asks for it.**
    ///
    /// It used to own an `isOpen` `@State` and a `.sheet` of its own, which
    /// meant the only thing that could open a take-over was this thumbnail —
    /// so the strip that announces a stopped desk, and the blocked sentence in
    /// this same inspector, had no way to offer one. The sheet is presented
    /// once, at `DeckRootView`, from `DeckStore.takeover`.
    private let onOpen: () -> Void

    public init(desk: String, displayName: String, workspace: String? = nil,
                client: AgentScreenClient, onOpen: @escaping () -> Void) {
        self.workspace = workspace
        self.onOpen = onOpen
        _model = StateObject(wrappedValue: AgentComputerModel(
            desk: desk, displayName: displayName, client: client))
    }

    public var body: some View {
        AgentScreenPanelBody(
            presentation: model.presentation,
            isHovering: isHovering,
            workspace: workspace,
            onOpen: onOpen
        )
        .equatable()
        .onHover { isHovering = $0 }
        .onAppear {
            model.setWindowActive(activeState != .inactive)
            // A glance, not a session: the thumbnail asks for the slow rate and
            // the take-over on top of it asks for the fast one.
            model.addWatcher(.thumbnail)
        }
        .onDisappear { model.removeWatcher(.thumbnail) }
        .onChange(of: activeState) { _, state in
            model.setWindowActive(state != .inactive)
        }
    }
}

/// Everything the panel draws, as values. Nothing in here observes anything.
struct AgentScreenPanelBody: View, Equatable {
    let presentation: AgentScreenPresentation
    let isHovering: Bool
    /// **The line this panel replaces.**
    ///
    /// The inspector's slot currently reads
    /// `Working in /home/agentdeck/.claude/agent-bus/workspaces/new-hire-77ec17`,
    /// which is a developer's string in a manager's product. The screen goes in
    /// that slot — but the folder is the one fact that string carried, and it is
    /// what a person debugging a desk actually needs, so it is kept: in the
    /// tooltip, and behind a named copy action reachable by keyboard and by
    /// right-click. Off his face, not out of the product.
    let workspace: String?
    let onOpen: () -> Void

    static let copyWorkspaceAction = "Copy workspace path"
    /// Said by both ways in, so the picture and the button under it do not
    /// describe the same thing two different ways.
    static let openHint = "Click and type on this computer, full size."

    /// **Closures are excluded on purpose.** A closure is never equal to
    /// another closure, so comparing them would make `==` always false and
    /// `.equatable()` would skip nothing — the rebuild-per-keystroke straight
    /// back. Everything the body actually draws is compared, which is the other
    /// half of the rule: miss a field and the picture freezes instead.
    static func == (a: AgentScreenPanelBody, b: AgentScreenPanelBody) -> Bool {
        a.presentation == b.presentation
            && a.isHovering == b.isHovering
            && a.workspace == b.workspace
    }

    /// What the picture is showing first; where the desk is working second. A
    /// desk with no workspace gets no second line rather than an empty one.
    var helpText: String {
        guard let workspace, !workspace.isEmpty else { return presentation.spokenLabel }
        return "\(presentation.spokenLabel)\nWorking in \(workspace)"
    }

    var body: some View {
        VStack(spacing: 6) {
            // **The picture is the button.** It used to be a still image with
            // a control revealed under the pointer; a person who never happened
            // to rest the mouse on it had no reason to believe there was
            // anything to open at all. Hovering now only draws a ring — the
            // affordance a pointer expects — and never *adds* the way in.
            if presentation.showsOpen {
                Button(action: onOpen) { framedPicture }
                    .buttonStyle(.plain)
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(presentation.spokenLabel)
                    .accessibilityHint(Self.openHint)
            } else {
                // **Not a disabled button.** `.disabled` washes its content
                // out, and the content here is the sentence saying *why* there
                // is nothing to open — "Atlas has no computer running yet". A
                // dimmed explanation is the one thing this state must not be.
                framedPicture
                    .accessibilityElement(children: .ignore)
                    .accessibilityLabel(presentation.spokenLabel)
            }

            Text(presentation.caption)
                .font(.caption)
                .foregroundStyle(.secondary)
                // The picture above already says this to a screen reader, with
                // the age attached. Repeating it as a bare noun adds nothing.
                .accessibilityHidden(true)

            // **On screen, always, with words on it.** This is the control he
            // could not find. It is drawn only over a machine there is
            // something to take over — `showsOpen` is false for a desk with no
            // computer and for a deck that refused — so it is never a button
            // that opens a black rectangle.
            if presentation.showsOpen {
                Button(action: onOpen) {
                    HStack(alignment: .center, spacing: 5) {
                        Image(systemName: "arrow.up.left.and.arrow.down.right")
                        Text(Takeover.screenTitle)
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.deckSecondary)
                .controlSize(.small)
                .accessibilityLabel("\(Takeover.screenTitle): \(presentation.caption)")
                .accessibilityHint(Self.openHint)
            }
        }
        // Reachable by keyboard and by rotor as well as by the two buttons.
        .accessibilityAction(named: Takeover.screenTitle) {
            if presentation.showsOpen { onOpen() }
        }
        // Both routes to the folder, so it is reachable with a pointer and
        // without one.
        .contextMenu {
            if workspace != nil {
                Button(Self.copyWorkspaceAction, action: copyWorkspace)
            }
        }
        .accessibilityAction(named: Self.copyWorkspaceAction, copyWorkspace)
        .help(helpText)
    }

    /// The picture in its slot, at the display's own shape. The ring is the
    /// pointer's affordance and nothing more — it never adds a control, and it
    /// is never the only thing saying the picture can be pressed.
    private var framedPicture: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .fill(.quaternary)
            picture
        }
        .frame(maxWidth: .infinity)
        .aspectRatio(1280.0 / 800.0, contentMode: .fit)
        .clipShape(RoundedRectangle(cornerRadius: 8, style: .continuous))
        .overlay {
            RoundedRectangle(cornerRadius: 8, style: .continuous)
                .strokeBorder(isHovering && presentation.showsOpen
                              ? DeckPalette.ink : Color.primary.opacity(0.10),
                              lineWidth: isHovering && presentation.showsOpen ? 2 : 1)
        }
        // **Open, on the picture, under the pointer.** The reference product's
        // thumbnail says it in a dark pill dead centre, and so does this one.
        // It is an overlay, so the panel is exactly as tall with the pointer
        // on it as off it - and it decorates a way in that is already on
        // screen (the button under the caption), it never creates one.
        .overlay {
            if isHovering && presentation.showsOpen {
                HStack(alignment: .center, spacing: 6) {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.caption.weight(.semibold))
                    Text("Open")
                        .font(.callout.weight(.semibold))
                }
                .foregroundStyle(.white)
                .padding(.horizontal, 14)
                .padding(.vertical, 7)
                .background(Color.black.opacity(0.78), in: Capsule())
                .overlay(Capsule().strokeBorder(Color.white.opacity(0.18), lineWidth: 1))
                .shadow(color: .black.opacity(0.35), radius: 8, y: 2)
                .accessibilityHidden(true)
            }
        }
        .contentShape(Rectangle())
    }

    private func copyWorkspace() {
        guard let workspace, !workspace.isEmpty else { return }
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(workspace, forType: .string)
    }

    @ViewBuilder
    private var picture: some View {
        if let frame = presentation.frame, let image = frame.nsImage {
            Image(nsImage: image)
                .resizable()
                .aspectRatio(contentMode: .fit)
                // A stale frame is visibly old. Never a dead frame drawn as
                // though it were what the agent is looking at now.
                .opacity(presentation.isFrameLive ? 1 : 0.45)
                .grayscale(presentation.isFrameLive ? 0 : 1)
                .overlay(alignment: .bottom) {
                    if let line = presentation.statusLine {
                        Text(line)
                            .font(.caption2)
                            .padding(.horizontal, 6)
                            .padding(.vertical, 2)
                            .background(.thinMaterial, in: Capsule())
                            .padding(4)
                            .accessibilityHidden(true)
                    }
                }
        } else {
            // No picture. A still symbol and the sentence — the same shape the
            // connection indicator and the hiring pane use, and for the same
            // measured reason: an animation here runs for as long as the deck
            // stays quiet, which on `URLSession.shared` is 60 seconds.
            VStack(spacing: 6) {
                if let symbol = presentation.symbol {
                    Image(systemName: symbol)
                        .font(.title3)
                        .foregroundStyle(.secondary)
                        .accessibilityHidden(true)
                }
                if let line = presentation.statusLine {
                    Text(line)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                        .accessibilityHidden(true)
                }
            }
            .padding(8)
        }
    }
}

/// **Open: the same display, bigger, and live under his hands.**
///
/// *"the agents desktop i see the chrome but i cant use it."* The transport was
/// finished the whole time; this view's **vocabulary** was the defect. It had
/// one click, no right button, no double click, no wheel, no drag, and a
/// keyboard that was a text field with a Send button beside it. Every one of
/// those is now a real gesture.
///
/// The transform is still the whole correctness story: the picture is drawn at
/// whatever size this sheet has and the deck takes coordinates in the display's
/// own 1280x800, so a point is scaled back before it is sent. A point on the
/// letterbox rather than the picture is **not sent at all**, at either end of a
/// drag — the deck refuses an off-screen coordinate with a 400 instead of
/// clamping, on purpose, and a client that clamped would click somewhere
/// plausible and hide its own bug.
///
/// **Command is never taken.** ⌘W, ⌘Q and ⌘, keep working while the keyboard is
/// pointed at the container, which is what stops a take-over trapping him
/// inside itself. Escape *is* forwarded — a terminal is unusable without it —
/// so the way out of this sheet is Done, or ⌘W, or switching the keyboard off.
struct AgentScreenStage: View {
    @ObservedObject var model: AgentComputerModel

    @State private var typed = ""
    /// On by default: he pressed Open to use the machine. Off is how he gets
    /// his own keyboard back without closing the sheet.
    @State private var sendsKeystrokes = true
    @State private var lastMoveSentAt = Date.distantPast
    @StateObject private var surface: TakeoverSurface

    /// `surface` is for tests that ask the caption's own value; the app never
    /// passes one. The header (close, whose computer, take over, an address)
    /// is `TakeoverStageView`'s: one header for the whole computer.
    init(model: AgentComputerModel, surface: TakeoverSurface? = nil) {
        self.model = model
        _surface = StateObject(wrappedValue: surface ?? TakeoverSurface())
    }

    /// Two releases in one burst are a double click; anything past that is the
    /// same burst again and is dropped rather than replayed as a triple.
    @State private var burst = 0

    /// **Nothing is drawn over the picture.** The owner, of controls that
    /// floated on glass over it: "the UI blocks stuff" -- they hid Chromium's
    /// tabs and address bar and the desk's taskbar. The picture is fitted in
    /// what the keys bar, docked underneath, leaves (`TakeoverChrome`).
    var body: some View {
        VStack(spacing: 0) {
            ZStack {
                Color.black
                stage
            }
            controls
                .frame(maxWidth: .infinity)
                .frame(height: TakeoverChrome.keysBarHeight)
                .background(Color.black)
        }
        .environment(\.colorScheme, .dark)
        // **No size of its own.** `TakeoverStageView` decides how big the
        // take-over is, against the window it is presented from — see
        // `Takeover.Stage`.
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Its own watcher, paired with its own disappear, and asking for the
        // fast rate: the thumbnail behind this sheet keeps its slow one, so
        // closing the take-over puts the box back down rather than switching
        // off a panel that is still on screen.
        .onAppear {
            model.setWindowActive(true)
            model.addWatcher(.takeover)
            surface.start(model: model)
            surface.switchOn = sendsKeystrokes
        }
        .onDisappear {
            surface.stop()
            model.removeWatcher(.takeover)
        }
        // Hidden (⌘H) or minimised: nothing on screen to stream to. Watching
        // from behind another app still counts — the window is visible.
        .onReceive(NotificationCenter.default.publisher(
            for: NSWindow.didChangeOcclusionStateNotification)) { _ in
            model.setWindowActive(AppVisibility.anyWindowVisible)
        }
        .onReceive(NotificationCenter.default.publisher(
            for: NSApplication.didHideNotification)) { _ in
            model.setWindowActive(false)
        }
        .onReceive(NotificationCenter.default.publisher(
            for: NSApplication.didUnhideNotification)) { _ in
            model.setWindowActive(AppVisibility.anyWindowVisible)
        }
        // Pushed, never read back through a captured `@State`: the monitor runs
        // outside `body` and a value it captured at `onAppear` would be the one
        // the switch had then, for the rest of the session. Whether the field
        // has his keys is asked of the window's first responder, by the
        // surface, and nowhere else.
        .onChange(of: sendsKeystrokes) { _, on in surface.switchOn = on }
    }

    private var stage: some View {
        GeometryReader { geometry in
            ZStack {
                if let frame = model.presentation.frame,
                   let image = frame.nsImage {
                    Image(nsImage: image)
                        .resizable()
                        .interpolation(.high)
                        .aspectRatio(contentMode: .fit)
                        .opacity(model.presentation.isFrameLive ? 1 : 0.45)
                } else {
                    FlatLabel(model.presentation.statusLine ?? "Nothing to show",
                              systemImage: model.presentation.symbol ?? "questionmark")
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
            .background(TakeoverProbe(surface: surface, size: geometry.size))
            .contentShape(Rectangle())
            .gesture(pointer(in: geometry.size))
            .onContinuousHover { phase in
                guard case .active(let location) = phase,
                      let fit = fit(geometry.size) else { return }
                hover(at: location, in: fit)
            }
            .accessibilityElement()
            .accessibilityLabel(model.presentation.spokenLabel)
            .accessibilityHint("Click, drag, scroll and type on this computer. Turn "
                               + "the keyboard switch off to type in this app again.")
        }
    }

    /// Press, drag, release. `minimumDistance: 0` so a plain click is a drag of
    /// no length, which is the only way one gesture can be both.
    ///
    /// **Nothing goes out until the release.** `screen/input` takes the whole
    /// press-move-release as one `drag`, so an `onChanged` here would be one
    /// HTTP round trip per pixel of travel and would still not be a drag on the
    /// far end — it would be a trail of clicks.
    private func pointer(in size: CGSize) -> some Gesture {
        DragGesture(minimumDistance: 0)
            .onEnded { value in
                guard let fit = fit(size) else { return }
                let start = value.startLocation
                let end = value.location
                let moved = hypot(end.x - start.x, end.y - start.y) > 3

                if moved {
                    burst = 0
                    Task {
                        await model.drag(fromViewX: start.x, y: start.y,
                                         toViewX: end.x, y: end.y, in: fit,
                                         button: Self.buttonForCurrentEvent())
                    }
                    return
                }

                let clicks = NSApp.currentEvent?.clickCount ?? 1
                burst = clicks == 1 ? 1 : burst + 1
                guard burst <= 2 else { return }
                let count = burst
                let button = Self.buttonForCurrentEvent()
                Task {
                    await model.click(atViewX: end.x, y: end.y, in: fit,
                                      button: button, count: count)
                }
            }
    }

    /// Hover, so menus open and tooltips appear — but not sixty times a second.
    /// Each one is an HTTP round trip and the picture only changes four times a
    /// second anyway.
    private func hover(at location: CGPoint, in fit: ScreenFit) {
        let now = Date()
        guard now.timeIntervalSince(lastMoveSentAt) > 0.12 else { return }
        lastMoveSentAt = now
        Task { await model.move(toViewX: location.x, y: location.y, in: fit) }
    }

    private func fit(_ size: CGSize) -> ScreenFit? {
        model.fit(inViewWidth: size.width, height: size.height)
    }

    /// Control-click is a right click on a Mac, and the container is the only
    /// place that convention has to survive.
    private static func buttonForCurrentEvent() -> MouseButton {
        (NSApp.currentEvent?.modifierFlags.contains(.control) ?? false) ? .right : .left
    }

    /// **Every one of these is reachable without a pointer and without the key
    /// capture.** The capture is a convenience for hands on the machine; a
    /// screen reader user gets the same keys as named buttons.
    private var controls: some View {
        HStack(spacing: 8) {
            // The words are drawn here rather than handed to `Toggle`, which
            // would line its own label up with the switch for us — the
            // alignment fallback this app measured at 99-100% CPU with nobody
            // touching the machine. `BaselineFallbackTests` pins it.
            Image(systemName: "keyboard").foregroundStyle(.secondary).accessibilityHidden(true)
            Toggle("", isOn: $sendsKeystrokes)
                .labelsHidden()
                .toggleStyle(.switch).tint(DeckPalette.working)
                .controlSize(.small)
                .accessibilityLabel("Send my keystrokes to \(model.displayName)'s computer")
                .help("While this is on, what you type goes to the container. Command keys "
                      + "always stay with this Mac, so ⌘W still closes this window.")
            // The same value the key monitor routes by — never a second
            // opinion about focus that can disagree with it.
            if let refusal = model.lastInputRefusal {
                FlatLabel(refusal.sentence, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(1)
                    .layoutPriority(1)
            } else {
                Text(surface.route.caption(agent: model.displayName))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .layoutPriority(1)
            }
            Spacer(minLength: 0)
            Menu {
                ForEach(Self.namedKeys, id: \.self) { key in
                    Button(key) { Task { await model.send(.key(key)) } }
                        .accessibilityLabel("Press \(key) on \(model.displayName)'s computer")
                }
            } label: {
                Text("Keys").font(.callout)
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("Return, Tab and Escape on \(model.displayName)'s computer")
            TextField("Paste a line…", text: $typed)
                .textFieldStyle(.plain)
                .padding(.horizontal, 10).padding(.vertical, 5)
                .background(.white.opacity(0.1), in: Capsule())
                .frame(minWidth: 80, maxWidth: 260)
                .onSubmit(sendTyped)
                .accessibilityLabel("Type a line on \(model.displayName)'s computer")
                .accessibilityHint("Sent exactly as written, including spaces.")
            Button(action: sendTyped) {
                Image(systemName: "arrow.up").font(.system(size: 12, weight: .bold))
                    .foregroundStyle(.black)
                    .frame(width: 24, height: 24)
                    .background(Circle().fill(typed.isEmpty ? Color.white.opacity(0.4) : Color.white))
            }
            .buttonStyle(.plain)
            .disabled(typed.isEmpty)
            .accessibilityLabel("Send")
        }
        .padding(.horizontal, 12)
    }

    /// The keys worth a button of their own: the ones a form needs and the one
    /// that gets out of a menu.
    static let namedKeys = ["Return", "Tab", "Escape"]

    /// Sent exactly as typed, and only cleared once it has gone. He puts
    /// passwords through this field.
    private func sendTyped() {
        let text = typed
        guard !text.isEmpty else { return }
        Task {
            await model.send(.type(text))
            if model.lastInputRefusal == nil { typed = "" }
        }
    }
}

/// **The events SwiftUI has no gesture for.**
///
/// macOS 14 SwiftUI can express a drag and a click and nothing else this surface
/// needs: there is no scroll-wheel gesture, no right-mouse gesture and no way to
/// read a raw key press. Those three arrive through a local `NSEvent` monitor,
/// which sees only this app's own events and only while the take-over is open.
///
/// It is deliberately **not** a first-responder view. Stealing first responder
/// fights the text field beside it and leaves no way back to it; a monitor that
/// asks "is this window key, and is the pointer on the picture" answers the same
/// question without taking anything away from the rest of the app.
///
/// **It owns the one answer to "where does the next key go".** `route` is
/// rebuilt from the window's real key status and first responder whenever
/// either changes, the caption draws it, and a key-down is forwarded only when
/// it says `.agent`. It used to be two answers — `@FocusState` for the caption
/// and the arming, the responder chain for the monitor — and a sheet that
/// focused its own text field on open made them disagree: switch on, caption
/// "Your keys stay in this app", keys in a field he never chose.
///
/// So it does one thing to the responder chain, and only ever *off* a text
/// field, never onto itself: with the switch on, a field he has not chosen —
/// the one the sheet focuses before he has touched anything, or the one he
/// left by clicking the picture — gives the keyboard back to the window.
/// Clicking the field takes it again, and the caption says so.
@MainActor
final class TakeoverSurface: ObservableObject {
    /// The AppKit view behind the drawn picture, so a pointer position can be
    /// converted into the picture's own coordinates rather than guessed at.
    fileprivate weak var view: NSView?
    fileprivate var size: CGSize = .zero

    /// The Keyboard switch, pushed in by the stage.
    var switchOn = false {
        didSet { responderChanged() }
    }

    /// Where a key pressed now lands. Published only when it changes.
    @Published private(set) var route: KeyRoute = .anotherWindow

    /// Whether he has pressed a key or a button in this window yet. Until he
    /// has, a text field holding the keyboard was put there by the sheet.
    private var heHasActed = false

    private var monitor: Any?
    private weak var model: AgentComputerModel?
    private weak var window: NSWindow?
    private var responderWatch: NSKeyValueObservation?
    private var keyWatches: [NSObjectProtocol] = []

    func start(model: AgentComputerModel) {
        self.model = model
        guard monitor == nil else { return }
        monitor = NSEvent.addLocalMonitorForEvents(
            matching: [.keyDown, .leftMouseDown, .scrollWheel, .rightMouseDown,
                       .otherMouseDown]
        ) { [weak self] event in
            guard let self else { return event }
            return MainActor.assumeIsolated { self.handle(event) }
        }
    }

    func stop() {
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
        model = nil
        attach(to: nil)
    }

    deinit {
        if let monitor { NSEvent.removeMonitor(monitor) }
        keyWatches.forEach(NotificationCenter.default.removeObserver)
    }

    /// Follows the window the picture is in: its first responder, and whether
    /// it is the key window — the only two facts `route` is made of.
    fileprivate func attach(to window: NSWindow?) {
        guard window !== self.window || window == nil else { return }
        responderWatch = nil
        keyWatches.forEach(NotificationCenter.default.removeObserver)
        keyWatches = []
        self.window = window
        if let window {
            responderWatch = window.observe(\.firstResponder) { [weak self] _, _ in
                MainActor.assumeIsolated { self?.responderChanged() }
            }
            for name in [NSWindow.didBecomeKeyNotification, NSWindow.didResignKeyNotification] {
                keyWatches.append(NotificationCenter.default.addObserver(
                    forName: name, object: window, queue: .main
                ) { [weak self] _ in
                    MainActor.assumeIsolated { self?.refresh() }
                })
            }
        }
        responderChanged()
    }

    private func refresh() {
        let next = KeyRoute.decide(switchOn: switchOn,
                                   windowIsKey: window?.isKeyWindow == true,
                                   textFieldHasKeys: Self.isTypingIntoThisApp(window))
        if next != route { route = next }
    }

    private func responderChanged() {
        refresh()
        guard switchOn, !heHasActed, Self.isTypingIntoThisApp(window) else { return }
        // Not from inside the responder change that told us about it.
        Task { @MainActor [weak self] in self?.releaseTextField() }
    }

    /// The keyboard goes back to the window, which is where the monitor
    /// forwards from. Nothing becomes first responder in the field's place.
    private func releaseTextField() {
        guard switchOn, let window, Self.isTypingIntoThisApp(window) else { return }
        window.makeFirstResponder(nil)
        refresh()
    }

    /// Nil swallows the event; returning it lets the rest of the app have it.
    private func handle(_ event: NSEvent) -> NSEvent? {
        guard let model, let view, let window = view.window,
              event.window === window else { return event }

        if event.type == .leftMouseDown {
            // His click on the picture means "this machine": the keys leave the
            // line field he is no longer looking at. Returned either way — the
            // SwiftUI gesture still needs the press.
            heHasActed = true
            if Self.point(of: event, in: view) != nil { releaseTextField() }
            return event
        }
        guard window.isKeyWindow else { return event }

        switch event.type {
        case .keyDown:
            heHasActed = true
            refresh()
            // Command belongs to his Mac, always. Everything else goes where
            // the caption says it goes — the same value, read at the same
            // moment, so the two cannot disagree.
            guard route == .agent, !event.modifierFlags.contains(.command) else { return event }
            let stroke = Self.stroke(from: event)
            guard ScreenKeys.input(for: stroke) != nil else { return event }
            Task { await model.press(stroke) }
            return nil

        case .scrollWheel:
            guard let point = Self.point(of: event, in: view),
                  let fit = self.fit(for: model) else { return event }
            let lines = event.hasPreciseScrollingDeltas ? Self.pointsPerLine : 1
            let dy = Double(event.scrollingDeltaY) / lines
            let dx = Double(event.scrollingDeltaX) / lines
            Task { await model.scroll(atViewX: point.x, y: point.y,
                                      wheelY: dy, wheelX: dx, in: fit) }
            return nil

        case .rightMouseDown, .otherMouseDown:
            guard let point = Self.point(of: event, in: view),
                  let fit = self.fit(for: model) else { return event }
            let button: MouseButton = event.type == .rightMouseDown ? .right : .middle
            Task { await model.click(atViewX: point.x, y: point.y, in: fit,
                                     button: button, count: 1) }
            return nil

        default:
            return event
        }
    }

    /// A trackpad reports pixels; the wheel takes notches. Sixteen points is one
    /// line, which is what every scroll view on this platform assumes.
    static let pointsPerLine = 16.0

    /// **Is a field of this app taking these keys?** A SwiftUI `TextField` on
    /// macOS edits through AppKit's field editor, which is an `NSTextView`
    /// installed as the window's first responder, and a `TextEditor` is one
    /// directly. Either way, a keystroke aimed at a text field of this app is
    /// never a keystroke for the container. Nor is one aimed at the desk's own
    /// terminal beside the picture (`TakesTheKeyboard`): it has its own socket.
    static func isTypingIntoThisApp(_ window: NSWindow?) -> Bool {
        window?.firstResponder is NSTextView || window?.firstResponder is TakesTheKeyboard
    }

    private func fit(for model: AgentComputerModel) -> ScreenFit? {
        model.fit(inViewWidth: size.width, height: size.height)
    }

    /// The event's position in the picture's own coordinates, top-left down —
    /// AppKit's origin is the other corner and getting that wrong is a click in
    /// the mirror image of where he aimed.
    private static func point(of event: NSEvent, in view: NSView) -> CGPoint? {
        let local = view.convert(event.locationInWindow, from: nil)
        guard view.bounds.contains(local) else { return nil }
        return CGPoint(x: local.x,
                       y: view.isFlipped ? local.y : view.bounds.height - local.y)
    }

    static func stroke(from event: NSEvent) -> KeyStroke {
        let flags = event.modifierFlags
        return KeyStroke(keyCode: event.keyCode,
                         characters: event.characters ?? "",
                         unmodified: event.charactersIgnoringModifiers ?? "",
                         control: flags.contains(.control),
                         option: flags.contains(.option),
                         command: flags.contains(.command),
                         shift: flags.contains(.shift))
    }
}

/// Hands the AppKit view behind the picture to the monitor, plus the size the
/// layout actually gave it. Draws nothing.
private struct TakeoverProbe: NSViewRepresentable {
    let surface: TakeoverSurface
    let size: CGSize

    func makeNSView(context: Context) -> NSView {
        let view = ProbeView()
        view.surface = surface
        surface.view = view
        surface.size = size
        return view
    }

    func updateNSView(_ view: NSView, context: Context) {
        (view as? ProbeView)?.surface = surface
        surface.view = view
        surface.size = size
    }

    /// Tells the surface which window it is in, the moment it knows.
    final class ProbeView: NSView {
        weak var surface: TakeoverSurface?
        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            surface?.attach(to: window)
        }
    }
}


extension AgentScreenFrame {
    /// The pixels the model already decoded off the main thread, wrapped
    /// without a copy; decoding `jpeg` here, on the main thread at draw time,
    /// only when that failed.
    var nsImage: NSImage? {
        if let decoded {
            return NSImage(cgImage: decoded.cgImage,
                           size: NSSize(width: decoded.cgImage.width,
                                        height: decoded.cgImage.height))
        }
        return NSImage(data: jpeg)
    }
}

/// Whether any of the app's windows is actually on screen: not hidden, not
/// minimised, not fully covered.
enum AppVisibility {
    @MainActor static var anyWindowVisible: Bool {
        !NSApp.isHidden && NSApp.windows.contains {
            $0.isVisible && !$0.isMiniaturized && $0.occlusionState.contains(.visible)
        }
    }
}

import AppKit
import SwiftUI
import DeckKit

/// **While agents may control this Mac, he always sees it.** A small
/// floating pill at the top of the main screen, over every app and Space:
/// "Atlas is controlling your Mac · 28 min left — Stop (⌃⌥⌘.)".
///
/// Also the "his hands win" monitor: while control is on, any real mouse or
/// key event (one without our marker) pauses agent input for a few seconds.
/// Both exist only while a grant is live, so nothing runs when it is off.
@MainActor
public final class MacControlChrome {
    private var panel: NSPanel?
    private var monitor: Any?
    private var stop: () -> Void = {}
    private var last: MacBridgeState?
    /// Re-draws "· N min left" while shown; gone when control is off.
    private var tick: Timer?

    public init() {}

    /// Show or hide everything for this state. Cheap when nothing changed.
    public func update(state: MacBridgeState, now: Date = Date(), onStop: @escaping () -> Void) {
        stop = onStop
        last = state
        guard let grant = state.control, grant.live(at: now.timeIntervalSince1970) else {
            hide()
            return
        }
        if tick == nil {
            tick = Timer.scheduledTimer(withTimeInterval: 20, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated {
                    guard let self, let s = self.last else { return }
                    self.update(state: s, onStop: self.stop)
                }
            }
        }
        let recent = state.lastControl.flatMap { now.timeIntervalSince($0.at) < 15 ? $0.desk : nil }
        let who = recent.flatMap { $0.hasPrefix("@") ? nil : $0 } ?? grant.scope
        let text = MacControlCopy.banner(scope: who, now: now.timeIntervalSince1970, until: grant.until)
        show(text)
        watchHisHands()
    }

    public func hide() {
        tick?.invalidate()
        tick = nil
        panel?.orderOut(nil)
        panel = nil
        if let monitor { NSEvent.removeMonitor(monitor) }
        monitor = nil
    }

    private func show(_ text: String) {
        let view = MacControlBannerView(text: text, onStop: { [weak self] in self?.stop() })
        if let panel, let host = panel.contentView as? NSHostingView<MacControlBannerView> {
            host.rootView = view
            return
        }
        let host = NSHostingView(rootView: view)
        let size = NSSize(width: 520, height: 40)
        let p = NSPanel(contentRect: NSRect(origin: .zero, size: size),
                        styleMask: [.borderless, .nonactivatingPanel], backing: .buffered, defer: false)
        p.contentView = host
        p.isOpaque = false
        p.backgroundColor = .clear
        p.hasShadow = true
        p.level = .statusBar
        p.collectionBehavior = [.canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle]
        p.isMovableByWindowBackground = true
        p.hidesOnDeactivate = false
        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            p.setFrameOrigin(NSPoint(x: f.midX - size.width / 2, y: f.maxY - size.height - 6))
        }
        p.orderFrontRegardless()
        panel = p
    }

    private func watchHisHands() {
        guard monitor == nil else { return }
        let mask: NSEvent.EventTypeMask = [.mouseMoved, .leftMouseDown, .rightMouseDown, .leftMouseDragged,
                                           .scrollWheel, .keyDown]
        monitor = NSEvent.addGlobalMonitorForEvents(matching: mask) { event in
            let mark = event.cgEvent?.getIntegerValueField(.eventSourceUserData) ?? 0
            if mark != MacInputInjector.marker { MacOwnerActivity.shared.touched() }
        }
    }
}

struct MacControlBannerView: View {
    let text: String
    let onStop: () -> Void

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "cursorarrow.motionlines").foregroundStyle(.white)
            Text(text).font(.callout.weight(.semibold)).foregroundStyle(.white).lineLimit(1)
            Spacer(minLength: 4)
            Button(action: onStop) {
                Text("Stop (\(MacControlCopy.hotkey))").font(.callout.weight(.semibold))
                    .padding(.horizontal, 10).padding(.vertical, 3)
                    .background(Capsule().fill(Color.white))
                    .foregroundStyle(Color.red)
            }
            .buttonStyle(.plain)
        }
        .padding(.horizontal, 14)
        .frame(height: 40)
        .background(Capsule().fill(Color.red.opacity(0.92)))
    }
}

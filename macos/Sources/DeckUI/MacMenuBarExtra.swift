import SwiftUI
import DeckKit

/// **What the menu-bar extra says and offers, as values.** The icon fills while
/// a job runs; the menu lists each running job with its own Stop, then the
/// plan's three: Pause Mac access (kills everything and stops polling, desks
/// see `mac_paused`), Open Activity, Quit.
struct MacMenuBarModel: Equatable {
    enum PauseAction: Equatable { case pause, resume, none }
    struct Job: Equatable, Identifiable { let id: String; let title: String }

    let symbol: String
    let status: String
    let jobs: [Job]
    let pauseTitle: String
    let pauseAction: PauseAction
    /// "Atlas is controlling your Mac · 28 min left", while a grant is live.
    let controlStatus: String?
    let stopControlTitle = "Stop Mac control (\(MacControlCopy.hotkey))"
    let openActivityTitle = "Open Activity"
    let quitTitle = "Quit Agent Deck"

    init(state: MacBridgeState, now: Date = Date()) {
        let grant = state.control.flatMap { $0.live(at: now.timeIntervalSince1970) ? $0 : nil }
        controlStatus = grant.map { MacControlCopy.banner(scope: $0.scope, now: now.timeIntervalSince1970, until: $0.until) }
        if grant != nil {
            symbol = "cursorarrow.motionlines"
        } else if state.isInUse {
            symbol = "hand.point.up.left.fill"
        } else {
            switch state.connection {
            case .off: symbol = "circle.slash"
            case .paused: symbol = "pause.circle"
            case .refused: symbol = "exclamationmark.circle"
            case .connecting, .online: symbol = "hand.point.up.left"
            }
        }

        switch (state.mode, state.connection) {
        case (_, .paused), (.paused, _): status = "Paused — desks can't reach this Mac"
        case (.off, _): status = "Off"
        case (_, .refused): status = "Not connected"
        case (let mode, .connecting): status = Self.name(mode) + " · connecting…"
        case (let mode, .online): status = Self.name(mode) + " · connected"
        case (let mode, .off): status = Self.name(mode)
        }

        jobs = state.running.map { Job(id: $0.id, title: "\(MacGrantCopy.displayName($0.desk)) — \($0.summary)") }

        switch state.mode {
        case .paused: (pauseTitle, pauseAction) = ("Resume Mac access", .resume)
        case .off: (pauseTitle, pauseAction) = ("Pause Mac access", .none)
        case .ask, .full: (pauseTitle, pauseAction) = ("Pause Mac access", .pause)
        }
    }

    static func name(_ mode: MacMode) -> String {
        switch mode {
        case .off: return "Off"
        case .ask: return "Ask me"
        case .full: return "Full access"
        case .paused: return "Paused"
        }
    }
}

/// What the menu's buttons do. The app shell (A4) wires these to the bridge.
public struct MacMenuBarActions {
    public var stopJob: (String) -> Void
    public var pause: () -> Void
    public var resume: () -> Void
    public var openActivity: () -> Void
    public var quit: () -> Void
    public var stopControl: () -> Void

    public init(stopJob: @escaping (String) -> Void = { _ in }, pause: @escaping () -> Void = {},
                resume: @escaping () -> Void = {}, openActivity: @escaping () -> Void = {},
                quit: @escaping () -> Void = {}, stopControl: @escaping () -> Void = {}) {
        self.stopControl = stopControl
        self.stopJob = stopJob
        self.pause = pause
        self.resume = resume
        self.openActivity = openActivity
        self.quit = quit
    }
}

/// The menu itself, separate from the scene so it can be drawn in a test.
public struct MacMenuBarContent: View {
    let state: MacBridgeState
    let actions: MacMenuBarActions

    public init(state: MacBridgeState, actions: MacMenuBarActions) {
        self.state = state
        self.actions = actions
    }

    public var body: some View {
        let model = MacMenuBarModel(state: state)
        Text(model.status)
        if let control = model.controlStatus {
            Divider()
            Text(control)
            Button(model.stopControlTitle, action: actions.stopControl)
        }
        if !model.jobs.isEmpty {
            Divider()
            ForEach(model.jobs) { job in
                Button("Stop  \(job.title)") { actions.stopJob(job.id) }
            }
        }
        Divider()
        switch model.pauseAction {
        case .pause: Button(model.pauseTitle, action: actions.pause)
        case .resume: Button(model.pauseTitle, action: actions.resume)
        case .none: EmptyView()
        }
        Button(model.openActivityTitle, action: actions.openActivity)
        Divider()
        Button(model.quitTitle, action: actions.quit)
            .keyboardShortcut("q")
    }
}

/// The menu-bar extra. Inserted while Mac access is not Off (the plan: "Keep
/// running in the menu bar (on while access is not Off)"), so a closed window
/// does not end the bridge. The app shell puts it in its scene list.
public struct MacMenuBarScene: Scene {
    let state: MacBridgeState
    let actions: MacMenuBarActions
    @Binding var isInserted: Bool

    public init(state: MacBridgeState, actions: MacMenuBarActions, isInserted: Binding<Bool>) {
        self.state = state
        self.actions = actions
        self._isInserted = isInserted
    }

    public var body: some Scene {
        MenuBarExtra(isInserted: $isInserted) {
            MacMenuBarContent(state: state, actions: actions)
        } label: {
            Image(systemName: MacMenuBarModel(state: state).symbol)
                .accessibilityLabel("Agent Deck Mac access")
        }
        .menuBarExtraStyle(.menu)
    }
}

import SwiftUI
import AppKit
import DeckKit

/// What changing "Let agents use this Mac" does. Only Full access is gated.
enum MacModeChange: Equatable {
    case apply(MacMode)
    /// Show the confirmation sheet first; nothing changes until he confirms.
    case confirmFull
}

/// The words and rules of Settings → Mac, without a window.
enum MacAccessCopy {
    static let fullAccessConfirm = "Turn on full access"

    /// The Screenshots switch: Full access already includes looking, so it
    /// shows on and locked there; in Ask me it is his to flip.
    static func screenshotSwitch(_ config: MacPolicyConfig) -> (on: Bool, enabled: Bool, caption: String) {
        if config.mode == .full {
            return (true, false, "Included in Full access.")
        }
        return (config.screenshotEnabled, true, "Off unless you turn it on. macOS will ask you once.")
    }
    static let fullAccessTitle = "Turn on full access?"

    static func modeChange(from: MacMode, to: MacMode) -> MacModeChange {
        (to == .full && from != .full) ? .confirmFull : .apply(to)
    }

    static func fullAccessBody(host: String?) -> String {
        let where_ = (host?.isEmpty == false) ? host! : "your deck"
        return "Any desk on \(where_) will be able to run anything on this Mac as you — read your files, keys and anything your account can reach — without asking. Only for a server you control."
    }

    static func modeBlurb(_ mode: MacMode) -> String {
        switch mode {
        case .off: return "Desks can't see this Mac at all."
        case .ask: return "A desk asks first. You answer once: an hour, or always for that desk."
        case .full: return "Desks can do anything you can on this Mac, without asking."
        case .paused: return "Paused from the menu bar. Desks are told to wait."
        }
    }

    /// The one sentence shown when the transport guard has switched the
    /// feature off; `nil` when nothing is wrong.
    static func transportSentence(for connection: MacConnection) -> String? {
        if case .refused(let why) = connection { return why }
        return nil
    }

    /// "1 hour (32 min left)" · "Always" · "Denied until 14:00".
    static func grantLine(_ grant: MacGrant, now: Date, locale: Locale = .current,
                          timeZone: TimeZone = .current) -> String {
        switch grant.state {
        case .always: return "Always"
        case .hour:
            let left = (grant.until ?? now.timeIntervalSince1970) - now.timeIntervalSince1970
            return left < 60 ? "1 hour (under a minute left)" : "1 hour (\(Int(ceil(left / 60))) min left)"
        case .denied:
            guard let until = grant.until else { return "Denied" }
            var style = Date.FormatStyle(date: .omitted, time: .shortened)
            style.locale = locale
            style.timeZone = timeZone
            return "Denied until " + Date(timeIntervalSince1970: until).formatted(style)
        }
    }

    /// Grants still in force, by desk name.
    static func liveGrants(_ config: MacPolicyConfig, now: Date) -> [(desk: String, grant: MacGrant)] {
        let t = now.timeIntervalSince1970
        return config.grants
            .filter { $0.value.until.map { t < $0 } ?? true }
            .sorted { $0.key < $1.key }
            .map { (desk: $0.key, grant: $0.value) }
    }

    static func shortPath(_ path: String, home: String = NSHomeDirectory()) -> String {
        if path == home { return "~" }
        return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
    }

    static func resetNeverTouch(_ config: inout MacPolicyConfig) {
        config.neverTouch = MacPolicyConfig.defaultNeverTouch
    }

    static func add(_ entry: String, to list: inout [String]) {
        let e = entry.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !e.isEmpty, !list.contains(e) else { return }
        list.append(e)
    }
}

/// What the tab asks of the rest of the app. The shell (A4) wires these to the
/// bridge; until then they are no-ops and the tab renders from values.
public struct MacAccessSettingsActions {
    public var revoke: (String) -> Void
    public var openActivity: () -> Void
    public var openFullDiskAccess: () -> Void
    // Mac control
    public var startControl: (String?) -> Void
    public var stopControl: () -> Void
    /// "Privacy_Accessibility" / "Privacy_ScreenCapture": opens that pane.
    public var openPrivacyPane: (String) -> Void
    public var openLiveView: (() -> Void)?
    public var permissions: () -> MacPermissions

    public init(revoke: @escaping (String) -> Void = { _ in }, openActivity: @escaping () -> Void = {},
                openFullDiskAccess: @escaping () -> Void = {},
                startControl: @escaping (String?) -> Void = { _ in }, stopControl: @escaping () -> Void = {},
                openPrivacyPane: @escaping (String) -> Void = { _ in }, openLiveView: (() -> Void)? = nil,
                permissions: @escaping () -> MacPermissions = { MacPermissions(accessibility: false, screenRecording: false) }) {
        self.revoke = revoke
        self.openActivity = openActivity
        self.openFullDiskAccess = openFullDiskAccess
        self.startControl = startControl
        self.stopControl = stopControl
        self.openPrivacyPane = openPrivacyPane
        self.openLiveView = openLiveView
        self.permissions = permissions
    }
}

/// **Settings → Mac.** One scrolling page of plain cards, in the order the plan
/// lists them: who may use this Mac, which folders, what is never touched,
/// what desks may open, which desks have access, and this Mac's name.
///
/// No `Form`, `Label`, `LabeledContent` or labelled `Toggle`: those resolve a
/// text baseline inside SwiftUI, which is the measured 100% CPU spin
/// (`BaselineFallbackTests`). Captions are drawn beside bare switches.
public struct MacAccessSettingsView: View {
    @Binding var config: MacPolicyConfig
    @Binding var macName: String
    @Binding var keepInMenuBar: Bool
    @Binding var startAtLogin: Bool
    let state: MacBridgeState
    /// The deck's host, for the Full access sheet ("Any desk on <host>…").
    let deckHost: String?
    let now: Date
    let actions: MacAccessSettingsActions

    @State private var confirmingFull = false
    @State private var newNever = ""
    @State private var newException = ""
    @State private var controlScope: String?

    public init(config: Binding<MacPolicyConfig>, macName: Binding<String>, keepInMenuBar: Binding<Bool>,
                startAtLogin: Binding<Bool>, state: MacBridgeState, deckHost: String?, now: Date = Date(),
                actions: MacAccessSettingsActions = MacAccessSettingsActions()) {
        self._config = config
        self._macName = macName
        self._keepInMenuBar = keepInMenuBar
        self._startAtLogin = startAtLogin
        self.state = state
        self.deckHost = deckHost
        self.now = now
        self.actions = actions
    }

    private var guardSentence: String? { MacAccessCopy.transportSentence(for: state.connection) }
    private var isFull: Bool { config.mode == .full }

    public var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                modeCard
                controlCard
                foldersCard
                neverTouchCard
                exceptionsCard
                openCard
                desksCard
                thisMacCard
            }
            .padding(20)
            .frame(maxWidth: 560)
            .frame(maxWidth: .infinity)
        }
        .sheet(isPresented: $confirmingFull) { fullAccessSheet }
    }

    // MARK: cards

    private var modeCard: some View {
        card("Let agents use this Mac") {
            DeckPillPicker(selection: modeBinding,
                           options: [(MacMode.off, "Off"), (MacMode.ask, "Ask me"), (MacMode.full, "Full access")],
                           label: "Let agents use this Mac")
            .fixedSize()
            .disabled(guardSentence != nil)
            Text(MacAccessCopy.modeBlurb(config.mode))
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            if let sentence = guardSentence {
                FlatLabel(sentence, systemImage: "lock.trianglebadge.exclamationmark")
                    .font(.callout)
                    .foregroundStyle(AttentionPalette.waitingText.swiftUIColor)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if isFull {
                HStack(spacing: 10) {
                    Text("So a desk isn't blocked while you're away, let Agent Deck see all your files.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    Spacer(minLength: 0)
                    Button("Grant Full Disk Access…", action: actions.openFullDiskAccess)
                        .controlSize(.small)
                }
            }
            HStack(spacing: 10) {
                Text("Every job is logged, shown in the menu bar, and can be stopped.")
                    .font(.caption).foregroundStyle(.secondary)
                Spacer(minLength: 0)
                Button("Open Activity", action: actions.openActivity).buttonStyle(.link).font(.caption)
            }
        }
    }

    private var foldersCard: some View {
        card("Folders agents may change", dimmed: isFull) {
            if config.folders.isEmpty {
                Text("None yet. The first time a desk asks, you can choose one. Good first pick: \(MacGrantCopy.suggestedFolder)")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(config.folders, id: \.self) { folder in
                listRow(MacAccessCopy.shortPath(folder), mono: false) { config.folders.removeAll { $0 == folder } }
            }
            HStack {
                Button("Add folder…") { if let p = Self.chooseFolder() { MacAccessCopy.add(p, to: &config.folders) } }
                if isFull { Text("Full access ignores this list.").font(.caption).foregroundStyle(.secondary) }
            }
        }
    }

    private var neverTouchCard: some View {
        card("Never touch", dimmed: isFull) {
            Text("Desks can't read or change anything matching these, even inside a folder you allowed.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ScrollView {
                VStack(spacing: 6) {
                    ForEach(config.neverTouch, id: \.self) { entry in
                        listRow(entry, mono: true) { config.neverTouch.removeAll { $0 == entry } }
                    }
                }
                .padding(.trailing, 8)
            }
            .frame(maxHeight: 190)
            addRow("Add a path or pattern", text: $newNever) {
                MacAccessCopy.add(newNever, to: &config.neverTouch); newNever = ""
            }
            HStack {
                Button("Reset to defaults") { MacAccessCopy.resetNeverTouch(&config) }
                    .disabled(config.neverTouch == MacPolicyConfig.defaultNeverTouch)
                Spacer(minLength: 0)
            }
            Text("Agent Deck's own settings and activity record are always off limits.")
                .font(.caption).foregroundStyle(.secondary)
        }
    }

    private var exceptionsCard: some View {
        card("Exceptions", dimmed: isFull) {
            Text("Specific files a desk may use even though they match Never touch, like ~/Projects/acme/.env.")
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            ForEach(config.exceptions, id: \.self) { entry in
                listRow(entry, mono: true) { config.exceptions.removeAll { $0 == entry } }
            }
            addRow("Add a file", text: $newException) {
                MacAccessCopy.add(newException, to: &config.exceptions); newException = ""
            }
        }
    }

    /// Desks he could scope control to: any that asked, has a grant, or did something here.
    private var knownDesks: [String] {
        var out: [String] = []
        for d in state.controlAsks + config.grants.keys.sorted() + state.recent.map(\.desk)
        where !d.isEmpty && !d.hasPrefix("@") && !out.contains(d) { out.append(d) }
        return out
    }

    private var controlCard: some View {
        let grant = state.control.flatMap { $0.live(at: now.timeIntervalSince1970) ? $0 : nil }
        let perms = actions.permissions()
        return card(MacControlCopy.switchTitle) {
            HStack(spacing: 12) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(grant.map { MacControlCopy.banner(scope: $0.scope, now: now.timeIntervalSince1970, until: $0.until) }
                         ?? "Off. Agents can click, type and scroll here only while this is on.")
                        .font(.body)
                    Text("30 minutes at a time. Stop with \(MacControlCopy.hotkey), the menu bar, or by locking your Mac; "
                         + "moving your mouse pauses agents for a few seconds. Never types into password fields.")
                        .font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                if grant != nil {
                    Button("Stop", action: actions.stopControl).controlSize(.small)
                } else {
                    Menu(controlScope.map(MacGrantCopy.displayName) ?? "Every desk") {
                        Button("Every desk") { controlScope = nil }
                        ForEach(knownDesks, id: \.self) { d in Button(MacGrantCopy.displayName(d)) { controlScope = d } }
                    }
                    .fixedSize()
                    Button("Turn on") { actions.startControl(controlScope) }
                        .controlSize(.small)
                        .disabled(config.mode == .off || config.mode == .paused)
                }
            }
            if !perms.accessibility {
                permissionRow("Allow Agent Deck in Accessibility so agents can click and type.",
                              pane: "Privacy_Accessibility")
            }
            if !perms.screenRecording {
                permissionRow("Allow Agent Deck in Screen Recording so agents (and your phone) can see this screen.",
                              pane: "Privacy_ScreenCapture")
            }
            if let open = actions.openLiveView {
                HStack {
                    Text("See this Mac the way your phone does.").font(.caption).foregroundStyle(.secondary)
                    Spacer(minLength: 0)
                    Button("Live view", action: open).buttonStyle(.link).font(.caption).disabled(grant == nil)
                }
            }
        }
    }

    private func permissionRow(_ text: String, pane: String) -> some View {
        HStack(spacing: 10) {
            Text(text).font(.caption).foregroundStyle(AttentionPalette.waitingText.swiftUIColor)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button("Open System Settings…") { actions.openPrivacyPane(pane) }.controlSize(.small)
        }
    }

    private var openCard: some View {
        card("What else desks may do") {
            switchRow("Open apps and links", caption: "Lets a desk open a website or an app for you.",
                      isOn: $config.openEnabled)
            Divider()
            let shot = MacAccessCopy.screenshotSwitch(config)
            switchRow("Screenshots of this screen", caption: shot.caption,
                      isOn: shot.enabled ? $config.screenshotEnabled : .constant(shot.on), enabled: shot.enabled)
        }
    }

    private var desksCard: some View {
        let live = MacAccessCopy.liveGrants(config, now: now)
        return card("Desks with access") {
            if live.isEmpty {
                Text(isFull ? "Full access doesn't need a list: every desk on your deck can use this Mac."
                             : "No desk has access yet. They ask the first time they need it.")
                    .font(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            ForEach(live, id: \.desk) { item in
                HStack(spacing: 10) {
                    MacDeskFace(desk: item.desk, size: 26)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(MacGrantCopy.displayName(item.desk)).font(.body)
                        Text(MacAccessCopy.grantLine(item.grant, now: now))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 8)
                    Button("Revoke") { actions.revoke(item.desk) }.controlSize(.small)
                }
            }
        }
    }

    private var thisMacCard: some View {
        card("This Mac") {
            HStack(spacing: 10) {
                Text("Name shown to desks").font(.body)
                Spacer(minLength: 8)
                TextField("", text: $macName, prompt: Text("My Mac"))
                    .deckField().frame(maxWidth: 220)
            }
            Divider()
            switchRow("Keep running in the menu bar",
                      caption: "Needed for desks to reach this Mac after you close the window.",
                      isOn: $keepInMenuBar, enabled: config.mode != .off)
            Divider()
            switchRow("Start at login", caption: nil, isOn: $startAtLogin)
        }
    }

    // MARK: full access sheet

    var fullAccessSheet: some View {
        VStack(alignment: .leading, spacing: 14) {
            FlatLabel(MacAccessCopy.fullAccessTitle, systemImage: "exclamationmark.triangle.fill")
                .font(.title3.weight(.semibold))
                .foregroundStyle(AttentionPalette.waitingText.swiftUIColor)
            Text(MacAccessCopy.fullAccessBody(host: deckHost))
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
            Text("Every job is still logged, shown in the menu bar, and can be stopped.")
                .font(.callout).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { confirmingFull = false }.keyboardShortcut(.cancelAction)
                Button(MacAccessCopy.fullAccessConfirm) {
                    config.mode = .full
                    confirmingFull = false
                }
                .buttonStyle(.deckDestructive)
            }
        }
        .padding(22)
        .frame(width: 440)
    }

    // MARK: pieces

    private var modeBinding: Binding<MacMode> {
        Binding(
            get: { config.mode == .paused ? .off : config.mode },
            set: { wanted in
                switch MacAccessCopy.modeChange(from: config.mode, to: wanted) {
                case .apply(let mode): config.mode = mode
                case .confirmFull: confirmingFull = true
                }
            })
    }

    private func card<Content: View>(_ title: String, dimmed: Bool = false,
                                     @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(title).font(.headline)
            content()
        }
        .padding(16)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Color.primary.opacity(0.05)))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
            .strokeBorder(Color.primary.opacity(0.08), lineWidth: 1))
        .opacity(dimmed ? 0.55 : 1)
    }

    private func switchRow(_ title: String, caption: String?, isOn: Binding<Bool>, enabled: Bool = true) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.body)
                if let caption {
                    Text(caption).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            Toggle("", isOn: isOn).labelsHidden().toggleStyle(.switch).tint(DeckPalette.working).disabled(!enabled)
        }
    }

    private func listRow(_ text: String, mono: Bool, remove: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            Text(text)
                .font(mono ? .system(.callout, design: .monospaced) : .callout)
                .lineLimit(1).truncationMode(.middle)
                .frame(maxWidth: .infinity, alignment: .leading)
            Button(action: remove) { Image(systemName: "minus.circle") }
                .buttonStyle(.borderless).foregroundStyle(.secondary)
                .accessibilityLabel("Remove \(text)")
        }
    }

    private func addRow(_ prompt: String, text: Binding<String>, add: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            TextField("", text: text, prompt: Text(prompt))
                .deckField().font(.system(.callout, design: .monospaced))
                .onSubmit(add)
            Button("Add", action: add).disabled(text.wrappedValue.trimmingCharacters(in: .whitespaces).isEmpty)
        }
    }

    /// The folder picker. Returns the chosen path, or `nil` if he cancelled.
    static func chooseFolder() -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Allow this folder"
        panel.message = "Choose a folder agents may change."
        return panel.runModal() == .OK ? panel.url?.path : nil
    }
}

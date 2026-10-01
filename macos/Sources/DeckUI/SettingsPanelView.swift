import SwiftUI
import DeckKit

/// The right-hand inspector for the selected agent.
///
/// **This is the one `Form` in the app that is on screen without being
/// opened** — `DeckRootView` sets `showInspector = true`, so it is drawn beside
/// every conversation, all the time. That makes what it costs *per publish*
/// matter more than what it costs to draw once.
///
/// `DeckStore.composerDraft` is `@Published`, so a single character typed into
/// the conversation is one `objectWillChange` on the store, and a view that
/// observes the store re-evaluates its body on every one of them — whether or
/// not anything it draws has changed. Counted in `InspectorIsQuietTests` over
/// 20 keystrokes: **21** body evaluations for the observing shape against **1**
/// for the shape below. Twenty of those rebuilt ten `Form` rows, their
/// sections, two text fields, a switch and the routines `ForEach` for nothing.
///
/// So this view is deliberately two pieces: a thin observer that reads the four
/// facts the panel draws, and an `Equatable` body handed to SwiftUI through
/// `.equatable()`. When the values are unchanged SwiftUI stops at the wrapper
/// and the inspector's tree is not rebuilt at all. The store is still passed
/// down — the rows need it to save, to toggle, to delete — but it is passed as
/// a plain reference that nothing subscribes to, and compared by identity.
public struct SettingsPanelView: View {
    @ObservedObject var store: DeckStore
    /// Closes the inspector. Handed in by the root, which owns whether it is
    /// showing; the default is for a pane hosted without a window to close.
    private var onCollapse: () -> Void = {}

    public init(store: DeckStore) {
        self.store = store
    }

    /// The inspector, given the way to close itself.
    func collapsing(with action: @escaping () -> Void) -> SettingsPanelView {
        var copy = self
        copy.onCollapse = action
        return copy
    }

    public var body: some View {
        SettingsPanelBody(
            agent: store.settingsAgent,
            settingsError: store.settingsError,
            routines: store.routines,
            routinesProblem: store.routinesProblem,
            routinesEmptyMessage: store.routinesEmptyMessage,
            // Deck-wide, not this desk's: the tray's whole reason for existing
            // is the ask raised somewhere he is not looking.
            attention: store.attention,
            macSignIns: store.macSignIns,
            delivery: .current,
            store: store,
            screens: store.screenClient,
            shells: store.shellClient,
            onCollapse: onCollapse
        )
        .equatable()
        .navigationSplitViewColumnWidth(
            min: Theme.inspectorWidth.lowerBound,
            ideal: 300,
            max: Theme.inspectorWidth.upperBound
        )
    }
}

/// Everything the inspector draws, as values. Nothing in here observes the
/// store, so nothing in here re-runs because a message arrived, an unread count
/// moved or a key was pressed in the composer.
struct SettingsPanelBody: View, Equatable {
    let agent: Agent?
    let settingsError: DeckError?
    let routines: [Routine]
    let routinesProblem: String?
    let routinesEmptyMessage: String?
    /// Everything waiting on him **anywhere on the deck**. Empty draws nothing
    /// at all — see `AttentionTrayView`.
    let attention: [AttentionItem]
    /// Where each "Sign in on this Mac" has got to, by card id. `let` with no
    /// default for the reason `screens` has none.
    let macSignIns: [String: MacSignInPhase]
    /// What this build can really send a notification on. One fact, one
    /// sentence derived from it, so the line under the switch stays true on
    /// both sides of a channel being wired or taken away.
    let delivery: NotificationDelivery
    /// Held for the actions the rows perform, and for nothing else. Not
    /// `@ObservedObject`: subscribing here would put the rebuild straight back.
    let store: DeckStore
    /// What fetches a desk's screen, or `nil` on a deck that cannot serve one.
    /// A dependency rather than a drawn value, so it is deliberately absent
    /// from `==` below — like `store`, which is compared only by identity.
    ///
    /// **`let` with no default, and that is the detector.** These three were
    /// `var … = []` / `= .current` / `= nil`, so deleting `attention:` or
    /// `screens:` from the call site in `SettingsPanelView.body` compiled
    /// cleanly and every test still passed — the tray and the screen panel
    /// would simply stop being drawn, silently. That is exactly the defect
    /// this file was fixing, one call site up. With no default the compiler
    /// refuses, which is the only check that cannot be forgotten.
    let screens: AgentScreenClient?
    /// What runs one command on a desk's machine, or `nil` on a deck that
    /// cannot. A dependency and not a drawn value, so it is out of `==` for the
    /// same reason `screens` is — and `let` with no default for the same reason
    /// too: deleting `shells:` from the call site must not compile.
    let shells: DeskShellClient?
    /// Closes the inspector. A closure and not a drawn value, so it is out of
    /// `==` like the other actions - and a `var` with a default so the panel
    /// can be hosted on its own (a test, a preview) without a window to close.
    var onCollapse: () -> Void = {}

    /// Whether the profile and settings popover is open. Local to this pane:
    /// the gear is the only thing that flips it.
    @State private var showsProfile = false

    /// The comparison SwiftUI skips the rebuild on. It is every value drawn
    /// below and the identity of the store — miss one and the panel goes stale
    /// instead of quiet, which is why `InspectorIsQuietTests` asserts a change
    /// in each of them is seen.
    static func == (a: SettingsPanelBody, b: SettingsPanelBody) -> Bool {
        a.agent == b.agent
            && a.settingsError == b.settingsError
            && a.routines == b.routines
            && a.routinesProblem == b.routinesProblem
            && a.routinesEmptyMessage == b.routinesEmptyMessage
            && a.attention == b.attention
            && a.macSignIns == b.macSignIns
            && a.delivery == b.delivery
            && a.store === b.store
    }

    var body: some View {
        VStack(spacing: 0) {
            topBar

            // A scroll view and not a `Form`: the pane is a stack of cards, and
            // a grouped form would draw each of them as an inset settings row.
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    // **Above everything, including "no agent selected".** A
                    // desk stopped on a question is the one thing here that is
                    // costing him time right now, and it is about a desk he is
                    // by definition not looking at - so it cannot be behind a
                    // selection.
                    AttentionTrayView(
                        items: attention,
                        answer: { item, action in
                            Task { await store.answer(item: item, with: action) }
                        },
                        openDesk: { store.openDesk(named: $0) },
                        signIns: macSignIns,
                        canSignInOnMac: store.signIn != nil,
                        macBrowserLabel: store.macBrowserLabel,
                        signIn: { item, verb in
                            Task {
                                switch verb {
                                case .useLogin: await store.useMacLogin(item)
                                case .start: await store.startMacSignIn(item)
                                case .share: await store.finishMacSignIn(item)
                                case .cancel: await store.cancelMacSignIn(item)
                                }
                            }
                        })
                    .equatable()

                    if let agent {
                        VStack(alignment: .leading, spacing: 16) {
                            // Above the picture: this is the one screen a
                            // person opens when a desk looks dead, and the
                            // sentence saying what to do about it is the reason
                            // he opened it.
                            if let blocked = agent.blocked {
                                blockedCard(agent, blocked)
                            } else if attention.isEmpty {
                                allClear
                            }

                            // **The machine, above the paperwork.** The desk's
                            // screen is the thing he opens this panel to look
                            // at; name and title are set once, behind the gear.
                            screen(agent)

                            RoutinesPanelView(
                                agent: agent,
                                routines: routines,
                                problem: routinesProblem,
                                emptyMessage: routinesEmptyMessage,
                                store: store
                            )

                            terminal(agent)

                            if let error = settingsError {
                                FlatLabel(error.userFacingText, systemImage: "exclamationmark.triangle")
                                    .foregroundStyle(.red)
                                    .font(.callout)
                                    .fixedSize(horizontal: false, vertical: true)
                            }
                        }
                        .padding(.horizontal, 12)
                    } else {
                        ContentUnavailableView(
                            "No agent selected",
                            systemImage: "slider.horizontal.3",
                            description: Text("Pick an agent to see its screen and routines.")
                        )
                    }
                }
                .padding(.bottom, 16)
            }
        }
        // The inspector is a column too, and it was measured asking for 680
        // points inside a window he is allowed to make 560 tall - the same
        // fault as the transcript's, smaller only because its content is. Its
        // nested split group came back at the same 19,723 on his display.
        .takesTheHeightItIsGiven()
    }

    /// Gear and collapse, top right, as the reference product has them.
    private var topBar: some View {
        HStack(alignment: .center, spacing: 2) {
            Spacer(minLength: 0)
            barButton("gearshape", label: "Profile and settings") { showsProfile.toggle() }
                .popover(isPresented: $showsProfile, arrowEdge: .leading) {
                    if let agent {
                        ProfileSettingsForm(agent: agent, settingsError: settingsError,
                                            delivery: delivery, store: store)
                    }
                }
                .disabled(agent == nil)
            barButton("chevron.right.2", label: "Hide the side panel", action: onCollapse)
        }
        .padding(.horizontal, 10)
        .padding(.top, 8)
    }

    private func barButton(_ symbol: String, label: String,
                           action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .regular))
                .foregroundStyle(.secondary)
                .frame(width: 28, height: 28)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(label)
        .accessibilityLabel(label)
    }

    /// Nothing waiting is said once, small, and in a good mood - the tray
    /// itself still draws nothing at all when it is empty.
    private var allClear: some View {
        HStack(alignment: .center, spacing: 6) {
            Image(systemName: "checkmark.circle.fill")
                .foregroundStyle(.green)
                .accessibilityHidden(true)
            Text("Nothing is waiting on you.")
                .font(.callout)
                .foregroundStyle(.secondary)
        }
        .padding(.top, 4)
        .accessibilityElement(children: .combine)
    }

    /// The desk is stopped and says why, with the ways into its machine under
    /// the sentence - "open its window and answer the prompt yourself" was
    /// always the last line of these, and this is that window.
    private func blockedCard(_ agent: Agent, _ blocked: Blocked) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            FlatLabel(blocked.sentence, systemImage: "exclamationmark.triangle.fill")
                .foregroundStyle(.orange)
                .font(.callout)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel("Needs a look: \(blocked.sentence)")
            ForEach(takeoverOffers(agent)) { offer in
                takeoverButton(agent, offer)
            }
        }
        .padding(12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(Color.orange.opacity(0.10),
                    in: RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous))
    }

    /// **The desk's own machine: the picture, and the box he types into.**
    ///
    /// Where it lives. A desk hired by talking to it never picked this — the
    /// deck allocated it — so it is shown, quietly, and never asked for. Absent
    /// from the payload means absent from the panel.
    ///
    /// **The picture, not the path.** `Working in
    /// /home/agentdeck/.claude/agent-bus/workspaces/new-hire-77ec17` is a
    /// developer's string in a manager's product, and it is exactly where the
    /// reference product draws a live frame of the machine the desk is working
    /// on. The panel keeps the folder in reach — it takes `workspace` — and
    /// puts the screen on the face.
    ///
    /// Both halves are honest absences rather than empty boxes. A deck without
    /// the agent's-computer routes has no `screens` and nothing to show. A deck
    /// that cannot run a command has no `shells`. And a desk whose `cwd` the
    /// deck never stated has nowhere for the first command to run: `POST
    /// /terminal` takes an absolute path, and the one thing this app must not
    /// do is invent a home directory the deck was never asked about — he gets
    /// the screen without the terminal, rather than a prompt pointing at a
    /// guess.
    ///
    /// `.id(agent.name)` so selecting another desk builds a new model: a
    /// transcript and a working directory belong to the desk they were made on,
    /// and carrying either across is showing him one desk's answers under
    /// another desk's name.
    @ViewBuilder
    private func screen(_ agent: Agent) -> some View {
        if let screens {
            AgentScreenPanel(
                desk: agent.name,
                displayName: agent.displayName,
                workspace: agent.workspaceLine.map { _ in agent.shortWorkspace },
                client: screens,
                onOpen: { store.takeOver(desk: agent.name, focus: .screen) })
            .id(agent.name)
        } else if let workspace = agent.workspaceLine {
            Text(workspace)
                .font(.caption)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .accessibilityLabel(workspace)
        }
    }

    @ViewBuilder
    private func terminal(_ agent: Agent) -> some View {
        if let shells, agent.workspace.hasPrefix("/") {
            DeskTerminalPanel(
                desk: agent.name,
                displayName: agent.displayName,
                client: shells)
            .id(agent.name)
        }
    }

    /// The ways into this desk's machine, decided by the same function the
    /// conversation's strip uses so the two never call it different names.
    private func takeoverOffers(_ agent: Agent) -> [TakeoverOffer] {
        Takeover.offers(for: agent, canServeScreen: screens != nil,
                        canServeShell: shells != nil)
    }

    private func takeoverButton(_ agent: Agent, _ offer: TakeoverOffer) -> some View {
        Button {
            store.takeOver(desk: agent.name, focus: offer.focus)
        } label: {
            // `.center`, never a text baseline — an SF Symbol has no line of
            // text to sit on, and a baseline-aligned stack sends SwiftUI to a
            // hidden NSTextField for a fallback on every pass.
            HStack(alignment: .center, spacing: 5) {
                Image(systemName: offer.symbol)
                Text(offer.title)
            }
            .frame(maxWidth: .infinity)
        }
        .modifier(TakeoverButtonLook(isPrimary: offer.focus == .screen))
        .controlSize(.small)
        .help(offer.hint)
        .accessibilityLabel(offer.title)
        .accessibilityHint(offer.hint)
    }
}

/// **The profile and the settings, behind the gear.**
///
/// What used to fill the inspector: the avatar, the name (not editable - it is
/// the identity every thread id is keyed on), a title, a description, the
/// notification switch and the sentence saying what a notification can reach.
/// Set once, so it is a popover and not a permanent column of fields.
///
/// It owns its own edits, so typing here never touches the pane behind it, and
/// it is only built while it is open.
struct ProfileSettingsForm: View {
    let agent: Agent
    let settingsError: DeckError?
    let delivery: NotificationDelivery
    let store: DeckStore

    @State private var title = ""
    @State private var detail = ""
    @State private var notifications = true

    var body: some View {
        Form {
            Section {
                HStack {
                    Spacer()
                    AvatarView(agent: agent, size: 64)
                    Spacer()
                }
                .padding(.vertical, 6)
                .listRowBackground(Color.clear)
            }

            // Labels above the fields, not beside them: a trailing-aligned
            // field in a narrow popover truncates the value being edited.
            Section("Profile") {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Name").font(.caption).foregroundStyle(.secondary)
                    // Not editable: the name is the identity every thread id,
                    // message and org edge on this deck is keyed on.
                    Text(agent.name)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .accessibilityLabel("Name: \(agent.name), not editable")
                }
                LabelledField("Title", text: $title)
                VStack(alignment: .leading, spacing: 4) {
                    Text("Description").font(.caption).foregroundStyle(.secondary)
                    TextField("Description", text: $detail, axis: .vertical)
                        .textFieldStyle(.roundedBorder)
                        .labelsHidden()
                        .lineLimit(3...8)
                }
                Button("Save changes") {
                    Task { await store.saveProfile(title: title, detail: detail) }
                }
                .disabled(!hasEdits)
            }

            Section("Notifications") {
                // The switch draws no label of its own. A `Toggle` lines its
                // label up with the control for you - the same class of
                // construct as the `Label` four live runs named as the driver
                // of the 100% CPU spin. The caption is drawn beside it and
                // spoken BY the switch, so a screen reader still finds one
                // operable element saying the same words.
                HStack(alignment: .top, spacing: 8) {
                    Text(AgentSettingsModel.notificationsCaption)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityHidden(true)
                    Spacer(minLength: 0)
                    Toggle("", isOn: $notifications)
                        .labelsHidden()
                        .toggleStyle(.switch)
                        .accessibilityLabel(AgentSettingsModel.notificationsCaption)
                }
                .onChange(of: notifications) { _, enabled in
                    guard enabled != agent.notificationsEnabled else { return }
                    Task { await store.setNotifications(enabled) }
                }
                // **Asked, not asserted.** This line used to be a literal
                // typed in here, true when it was written and wrong the day the
                // pushes shipped. It now comes off the one value that knows
                // what this build can send on, so it is true on both sides of
                // that. See `OneSentenceAboutDeliveryTests`.
                FlatLabel(delivery.caveat, systemImage: "info.circle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let error = settingsError {
                Section {
                    FlatLabel(error.userFacingText, systemImage: "exclamationmark.triangle")
                        .foregroundStyle(.red)
                        .font(.callout)
                }
            }
        }
        .formStyle(.grouped)
        .frame(width: 340, height: 480)
        .onAppear(perform: load)
        .onChange(of: agent.name) { _, _ in load() }
    }

    private var hasEdits: Bool {
        title != agent.title || detail != agent.detail
    }

    /// A visible caption plus a labelled field: the label is on screen for
    /// sighted users and attached to the control for VoiceOver.
    private struct LabelledField: View {
        let caption: String
        @Binding var text: String

        init(_ caption: String, text: Binding<String>) {
            self.caption = caption
            self._text = text
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 4) {
                Text(caption).font(.caption).foregroundStyle(.secondary)
                TextField(caption, text: $text)
                    .textFieldStyle(.roundedBorder)
                    .labelsHidden()
                    .accessibilityLabel(caption)
            }
        }
    }

    private func load() {
        title = agent.title
        detail = agent.detail
        notifications = agent.notificationsEnabled
    }
}

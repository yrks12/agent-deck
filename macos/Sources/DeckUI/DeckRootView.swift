import SwiftUI
import DeckKit

/// The iPhone's layout on a Mac: the roster on the left drawn like the
/// phone's roster list, the conversation drawn like its thread, and the side
/// panel (attention cards, the desk's settings, routines, the screen) as a
/// secondary column he opens — from the toolbar, ⌥⌘I, or the roster's
/// "N things need your attention" card.
public struct DeckRootView: View {
    @StateObject private var store: DeckStore
    /// Closed at launch: the phone has no third column, and the owner asked
    /// for the Mac to feel exactly like the phone. Nothing is lost — every
    /// card in it is one click away (`showAttention:` below).
    @State private var showInspector = false

    /// **Whether the roster is on screen, owned by the app rather than by
    /// AppKit's autosave.**
    ///
    /// It was not, and he lost an evening to it. MEASURED, from
    /// `defaults read <bundle id>` on his Mac, verbatim:
    ///
    ///     "NSSplitView Subview Frames … SidebarNavigationSplitView" = (
    ///         "0.000000, 0.000000, 308.000000, 1990.000000, YES, NO",
    ///         "0.000000, 0.000000,  900.000000, 1990.000000, NO,  NO"
    ///     );
    ///
    /// That trailing `YES` is AppKit's **collapsed** flag. With no binding
    /// here, the autosave was the only thing deciding it: the column restored
    /// collapsed on every launch, and a collapsed roster in this product means
    /// **no desk is reachable at all** — which looks exactly like a dead app.
    /// There was no code path that could ever say "show the roster" again.
    ///
    /// Deliberately NOT `@SceneStorage`: persisting this is the defect, not the
    /// feature. Restoring a window's size and divider position is a courtesy;
    /// restoring a state in which the product cannot be used is not. He can
    /// still drag the divider away within a session; every launch brings it
    /// back. Pinned by `TheRosterColumnComesBackTests`.
    @State private var columnVisibility = NavigationSplitViewVisibility.all

    /// `shell` is the terminal route, handed in beside the client rather than
    /// cast out of it: `HTTPDeckClient` cannot be extended to conform from
    /// outside its own file. Nil on the fixture, which answers that route
    /// itself — see `DeckStore.shellClient`.
    public init(client: DeckClient, shell: DeskShellClient? = nil,
                visibleRowsPerSection: Int = .max) {
        _store = StateObject(
            wrappedValue: DeckStore(client: client, shell: shell,
                                    visibleRowsPerSection: visibleRowsPerSection)
        )
    }

    public var body: some View {
        // **Each of these three closures is a window column, and a column must
        // never ask for a size of its own.**
        //
        // Every one of them is hosted in its own `NSHostingView` inside an
        // `NSSplitViewItem`, and *that hosting view* publishes SwiftUI's ideal
        // size to AppKit as its `intrinsicContentSize`. Auto Layout treats an
        // intrinsic size as required, grows the enclosing `NSSplitView` until
        // it is satisfied, and centres the oversized result in a window that
        // cannot hold it — on whichever axis over-reported.
        //
        // Both axes did. MEASURED on his display, from the Accessibility tree
        // of the running app after the first round of this fix:
        //
        //     AXWindow   @(304,  33) 1208 x  949
        //      ├ AXGroup @(242,-473)  300 x 2013   <- roster column
        //      └ AXGroup @(234,-481) 1347 x 2029   <- detail column
        //
        // Too tall, and beginning at x=242 and x=234 inside a window that
        // begins at x=304 — so the roster and the conversation sat off the top
        // and bottom, and the inspector was clipped past the right edge of the
        // screen. He photographed it: approval cards drawing in the middle
        // band, blank everywhere else.
        //
        // The previous round capped the descendants it could find — the
        // transcript's `ScrollView`, the settings panel. That fixes the ones
        // you found and leaves the class open: anything inside a column that
        // still asks for a size rides straight out through the root. The
        // inspector was still asking for **721pt of width in a 300pt column**,
        // 661 of which was one wrapping sentence. So the cap goes here, at the
        // boundary, where it cannot be got round.
        //
        // Pinned by `AColumnFitsTheWindowItIsInTests`, which measures these
        // three roots as composed, on both axes.
        NavigationSplitView(columnVisibility: $columnVisibility) {
            Self.rosterColumn(store: store, showAttention: { showInspector = true })
        } detail: {
            Self.conversationColumn(store: store, inspector: $showInspector)
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    showInspector.toggle()
                } label: {
                    // The glyph alone, said out loud by the button. A macOS
                    // toolbar draws a `Label` as its icon anyway, so the title
                    // was never on screen — it was only paying for an alignment
                    // resolved inside SwiftUI, on an `NSToolbar` item, which is
                    // exactly where the live sample's hot chain ends.
                    Image(systemName: "sidebar.right")
                }
                .accessibilityLabel("Agent settings")
                .help("Show or hide the agent settings panel")
                .keyboardShortcut("i", modifiers: [.command, .option])
            }
        }
        // **The take-over, presented once, over the whole window.**
        //
        // Not inside the thumbnail that used to own it: three surfaces offer a
        // way in — the strip that announces a stopped desk, the blocked line in
        // the inspector, and the picture itself — and a sheet owned by one of
        // them can only ever be opened by that one. Here it is also the only
        // place with room to draw a 1280x800 display at a size he can read.
        //
        // Attached to the split view rather than to any column: a `.sheet` on a
        // column root would be inside an `NSSplitViewItem`'s hosting view, and
        // `AColumnFitsTheWindowItIsInTests` measures exactly those three roots.
        .sheet(item: Binding(
            get: { store.takeover },
            // Dismissed by the sheet itself — Escape reaches the container, so
            // this is Done, ⌘W, or AppKit closing it. Only ever nil.
            set: { if $0 == nil { store.endTakeover() } }
        )) { request in
            if let screens = store.screenClient {
                TakeoverStageView(
                    request: request,
                    screens: screens,
                    shells: store.shellClient,
                    onClose: { store.endTakeover() })
            }
        }
        // Connectors & Skills, opened from the sidebar's bottom row. On the
        // split view for the same reason as the take-over above.
        .sheet(isPresented: Binding(
            get: { store.isShowingStore },
            set: { if !$0 { store.dismissStore() } }
        )) {
            if let client = store.storeClient {
                StorePanelView(client: client, desks: store.agents.keys.sorted(),
                               onClose: { store.dismissStore() })
            }
        }
        .task { await store.loadRoster() }
    }

    // MARK: - the three column roots
    //
    // Named rather than written inline **so the test measures the app's own
    // columns and not a copy of them**. The first version of
    // `AColumnFitsTheWindowItIsInTests` rebuilt the columns by hand, measured
    // `TranscriptList` and `SettingsPanelView` in isolation, and reported
    // 622 tests / 0 failures against a build whose inspector was 421pt wider
    // than the column it was in. A detector that assembles its own subject can
    // only ever check the assembly it wrote down.

    /// The roster, as `NavigationSplitView` hosts it.
    static func rosterColumn(store: DeckStore, showAttention: @escaping () -> Void = {}) -> some View {
        SidebarView(store: store, onShowAttention: showAttention)
            .takesTheSizeItIsGiven()
    }

    /// The conversation, with the inspector attached — one closure, two hosted
    /// columns, because `.inspector` is its own `NSSplitViewItem`.
    static func conversationColumn(store: DeckStore, inspector: Binding<Bool>) -> some View {
        ThreadView(store: store)
            .takesTheSizeItIsGiven()
            .inspector(isPresented: inspector) {
                inspectorColumn(store: store, collapse: { inspector.wrappedValue = false })
            }
            // **Capped again on the outside, and that is not belt-and-braces.**
            // `.inspector` is a modifier on this expression, so the outermost
            // modifier is what the hosting view publishes — and the inspector
            // put an ideal width of 370 straight back on a column already
            // capped underneath it. Measured: 370 with this line absent,
            // `noIntrinsicMetric` with it present.
            .takesTheSizeItIsGiven()
    }

    /// The inspector. Capped in its own right rather than by the modifier on
    /// the pane beside it: it is hosted separately, so it reports separately —
    /// and it was the worst offender, at 721pt of ideal width in a column whose
    /// widest setting is 340.
    static func inspectorColumn(
        store: DeckStore, collapse: @escaping () -> Void = {}
    ) -> some View {
        SettingsPanelView(store: store).collapsing(with: collapse)
            .takesTheSizeItIsGiven()
    }
}

/// Where the deck lives and what token to use. Nothing is written in plaintext:
/// the field talks to the Keychain.
public struct DeckSettingsView: View {
    @AppStorage("deckBaseURL") private var baseURL = "http://127.0.0.1:7788"
    @State private var token = ""
    @State private var status: String?
    private let tokens: TokenStore

    public init(tokens: TokenStore = KeychainTokenStore()) {
        self.tokens = tokens
    }

    public var body: some View {
        Form {
            Section("Deck") {
                TextField("Base URL", text: $baseURL)
                    .accessibilityLabel("Deck base URL")
                Text("Used when you next open Agent Deck. DECK_URL in the environment overrides it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Section("API token") {
                SecureField("Bearer token", text: $token)
                    .accessibilityLabel("Deck API bearer token")
                HStack {
                    Button("Save to Keychain") { save() }
                        .disabled(token.isEmpty)
                    Button("Remove") { remove() }
                    Spacer()
                    if let status {
                        Text(status).font(.caption).foregroundStyle(.secondary)
                    }
                }
                Text("The token is stored in your login Keychain. It is never written to a file and never logged.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .formStyle(.grouped)
        .frame(width: 420)
        .padding()
        .onAppear {
            // Presence only. The value itself is never put back on screen.
            status = ((try? tokens.token()) ?? nil) == nil ? "No token saved" : "Token saved"
        }
    }

    private func save() {
        do {
            try tokens.setToken(token)
            token = ""
            status = "Token saved"
            // The main window failed against an empty Keychain and would keep
            // saying so until a relaunch. Tell it to ask the deck again.
            NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
        } catch let error as DeckError {
            status = error.userFacingText
        } catch {
            status = "Could not save to the Keychain"
        }
    }

    private func remove() {
        try? tokens.removeToken()
        token = ""
        status = "No token saved"
        NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil)
    }
}

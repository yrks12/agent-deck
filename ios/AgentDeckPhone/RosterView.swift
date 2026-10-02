import SwiftUI
import DeckKit

/// Where a tap on a row goes.
struct ThreadRoute: Hashable {
    let threadID: String
    let agent: String
}

/// **The roster.** The chief of staff on top as the hero — the one desk he
/// talks to — then every section the deck sends, in its order. The same rows
/// the Mac sidebar draws, from the same `SidebarSnapshot`.
struct RosterView: View {
    @EnvironmentObject private var store: PhoneStore
    @Environment(\.phoneCalls) private var calls
    @State private var showsStore = false
    @State private var signIn: AccountSignInRequest?
    @State private var showsAccounts = false
    @State private var macScreen: ScreenRoute?
    @State private var showsCallSettings = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let chief = store.snapshot.chief {
                    NavigationLink(value: route(chief)) { ChiefHero(row: chief) }
                        .contextMenu { callButton(chief); moveMenu(chief) }
                        .buttonStyle(.plain)
                        .padding(.horizontal, 16)
                        .padding(.top, 4)
                }
                if store.hasLoaded && !store.snapshot.allRows.isEmpty {
                    RosterPulse(rows: store.snapshot.allRows)
                        .padding(.horizontal, 20)
                        .padding(.top, 10)
                }
                if !store.attention.isEmpty {
                    AttentionBanner(count: store.attention.count)
                        .padding(.horizontal, 16)
                        .padding(.top, 12)
                }
                if let usage = store.usage {
                    AccountExpiryBanner(warnings: AccountsPresentation.expiryWarnings(usage.accounts),
                                        onSignIn: store.accountLoginClient == nil ? nil : { signIn = $0 })
                        .padding(.horizontal, 16)
                        .padding(.top, 12)
                    UsageMeterView(usage: usage)
                        .padding(.horizontal, 16)
                        .padding(.top, 12)
                }
                ForEach(store.macs) { mac in
                    YourMacRow(mac: mac) {
                        macScreen = ScreenRoute(desk: mac.screenName, displayName: mac.name, threadID: nil)
                        Haptics.tap()
                    }
                    .padding(.horizontal, 16)
                    .padding(.top, 12)
                }
                // Every desk once, Pinned first: the same list the Mac draws
                // (`SidebarSnapshot.listedSections`).
                ForEach(store.snapshot.listedSections) { section in
                    self.section(section.name, rows: section.rows)
                }
                if !store.hasLoaded {
                    firstLoadState
                } else if store.snapshot.allRows.isEmpty {
                    emptyState
                }
            }
            // The full width of the screen, under the title. An overlay on the
            // scroll view was laid out in a ~136pt column beside the large
            // title on iOS 26 — "Check-/ing in/ with/ your/ desks".
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.bottom, 24)
        }
        .background(PhoneTheme.canvas)
        .refreshable { await store.refreshAll() }
        .navigationTitle("Agents")
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                // Not live: a tap tries again now instead of waiting the backoff out.
                Button { store.retryNow(); Haptics.tap() } label: { ConnectionDot(state: store.connection) }
                    .buttonStyle(.plain)
                    .disabled(store.connection == .live)
                    .accessibilityHint(store.connection == .live ? "" : "Tap to try again")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Button { showsCallSettings = true; Haptics.tap() } label: { Image(systemName: "phone.arrow.down.left") }
                    .accessibilityLabel("Calls from desks")
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    if !store.deckLabel.isEmpty { Text("Connected to \(store.deckLabel)") }
                    Button("Refresh") { Task { await store.refreshAll() } }
                    if store.storeClient != nil {
                        Button("Connectors & Skills") { showsStore = true }
                    }
                    if store.accountLoginClient != nil {
                        Button("Claude accounts") { showsAccounts = true }
                    }
                    Button("Disconnect", role: .destructive) { store.disconnect() }
                } label: { Image(systemName: "ellipsis.circle") }
            }
        }
        .fullScreenCover(item: $macScreen) { route in
            AgentScreenView(route: route, client: store.screenClient, messages: nil)
        }
        .sheet(isPresented: $showsCallSettings) {
            NavigationStack {
                CallSettingsView(client: store.incomingCallClient)
                    .toolbar { Button("Done") { showsCallSettings = false } }
            }
        }
        .sheet(isPresented: $showsStore) {
            if let client = store.storeClient { StoreScreen(client: client) }
        }
        .sheet(isPresented: $showsAccounts) {
            if let usage = store.usage {
                VStack(alignment: .leading, spacing: 14) {
                    Text("Claude accounts").font(.headline)
                    ClaudeAccountsList(usage: usage) { request in
                        showsAccounts = false
                        // One sheet at a time: the list closes, then the sign-in opens.
                        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { signIn = request }
                    }
                    Spacer(minLength: 0)
                }
                .padding(20)
                .background(PhoneTheme.canvas)
                .presentationDetents([.medium, .large])
            }
        }
        .sheet(item: $signIn) { request in
            if let client = store.accountLoginClient {
                AccountSignInSheet(client: client, request: request,
                                   onDone: { Task { await store.refreshUsage() } },
                                   dismiss: { signIn = nil })
                    .presentationDetents([.medium, .large])
            }
        }
    }

    private func callButton(_ row: SidebarRow) -> some View {
        Button { store.placeCall(calls, agent: row.agent.name) } label: {
            Label("Call \(row.agent.displayName)", systemImage: "phone.fill")
        }
    }

    private func accountBadge(for row: SidebarRow) -> String? {
        guard let usage = store.usage else { return nil }
        return AccountsPresentation.badge(for: row.agent, accounts: usage.accounts,
                                          defaultID: usage.policy?.defaultAccount)
    }

    /// "Move to account…" on a long-press. Nothing on a deck with one account.
    private func moveMenu(_ row: SidebarRow) -> some View {
        MoveToAccountMenu(agent: row.agent, usage: store.usage) { account in
            Task { await store.moveDesk(row.agent.name, to: account) }
        }
    }

    private func route(_ row: SidebarRow) -> ThreadRoute {
        ThreadRoute(threadID: row.threadID ?? row.agent.threadID, agent: row.agent.name)
    }

    @ViewBuilder
    private func section(_ title: String, rows: [SidebarRow]) -> some View {
        if !rows.isEmpty {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .tracking(0.6)
                .foregroundStyle(.secondary)
                .padding(.horizontal, 20)
                .padding(.top, 22)
                .padding(.bottom, 6)
            ForEach(rows) { row in
                NavigationLink(value: route(row)) {
                    RosterRowContent(row: row, accountBadge: accountBadge(for: row))
                        .padding(.horizontal, 16)
                        .padding(.vertical, 11)
                }
                    .buttonStyle(RowPressStyle())
                    .contextMenu { callButton(row); moveMenu(row) }
                if let move = store.accountMoves[row.agent.name] {
                    AccountMoveNotice(status: move) { store.dismissAccountMove(row.agent.name) }
                        .padding(.horizontal, 80)
                        .padding(.bottom, 8)
                }
                if row.id != rows.last?.id {
                    Divider().padding(.leading, 80)
                }
            }
        }
    }

    /// Still finding the deck: what it is doing, why if a try failed, and a
    /// Retry that does not wait for the backoff.
    private var firstLoadState: some View {
        VStack(spacing: 14) {
            CharacterState(attention: .working, title: "Checking in with your desks",
                           message: store.loadError.map { "Still trying: \($0)" }
                               ?? "One moment while the deck catches up.")
            if store.loadError != nil {
                Button("Retry") { store.retryNow(); Haptics.tap() }
                    .buttonStyle(.borderedProminent)
            }
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 60)
    }

    @ViewBuilder
    private var emptyState: some View {
        Group {
            if let problem = store.loadError {
                VStack(spacing: 14) {
                    CharacterState(sleepy: true, title: "Couldn't reach your deck",
                                   message: problem + "\nPull down to try again.")
                    Button("Retry") { store.retryNow(); Haptics.tap() }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                CharacterState(title: "No desks here yet",
                               message: "Start a desk on your Mac and it will turn up here.")
            }
        }
        .padding(.top, 60)
    }
}

struct ConnectionDot: View {
    let state: ConnectionState
    var body: some View {
        HStack(spacing: 5) {
            Circle()
                .fill(state == .live ? PhoneTheme.working : Color.secondary)
                .frame(width: 7, height: 7)
            Text(state.phoneLabel).font(.caption).foregroundStyle(.secondary)
                .fixedSize()
        }
        .padding(.horizontal, 4)
        .accessibilityElement(children: .combine)
    }
}

struct RowPressStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .background(configuration.isPressed ? Color.primary.opacity(0.08) : .clear)
    }
}

/// "Your Mac": its live view, and you can drive it like a desk's computer.
/// His own Mac: no agent control grant is needed to watch or drive it.
private struct YourMacRow: View {
    let mac: MacNodeSummary
    let open: () -> Void

    var body: some View {
        Button(action: open) {
            HStack(spacing: 12) {
                Image(systemName: "desktopcomputer").font(.title3)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Your Mac · \(mac.name)").font(.body.weight(.semibold))
                    Text(mac.online ? "Live view · tap and type to drive it"
                         : "Asleep or offline — open Agent Deck on it")
                        .font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
                Image(systemName: "chevron.right").font(.caption).foregroundStyle(.secondary)
            }
            .padding(14)
            .background(RoundedRectangle(cornerRadius: 14).fill(Color.primary.opacity(0.06)))
        }
        .buttonStyle(.plain)
    }
}

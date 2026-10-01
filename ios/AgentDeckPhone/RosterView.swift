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

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 0) {
                if let chief = store.snapshot.chief {
                    NavigationLink(value: route(chief)) { ChiefHero(row: chief) }
                        .contextMenu { callButton(chief) }
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
                    UsageMeterView(usage: usage)
                        .padding(.horizontal, 16)
                        .padding(.top, 12)
                }
                // Every desk once, Pinned first: the same list the Mac draws
                // (`SidebarSnapshot.listedSections`).
                ForEach(store.snapshot.listedSections) { section in
                    self.section(section.name, rows: section.rows)
                }
                if store.hasLoaded && store.snapshot.allRows.isEmpty {
                    emptyState
                }
            }
            .padding(.bottom, 24)
        }
        .background(PhoneTheme.canvas)
        .overlay {
            if !store.hasLoaded {
                CharacterState(attention: .working, title: "Checking in with your desks",
                               message: "One moment while the deck catches up.")
            }
        }
        .refreshable { await store.refreshAll() }
        .navigationTitle("Agents")
        .navigationBarTitleDisplayMode(.large)
        .toolbar {
            ToolbarItem(placement: .topBarLeading) { ConnectionDot(state: store.connection) }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    if !store.deckLabel.isEmpty { Text("Connected to \(store.deckLabel)") }
                    Button("Refresh") { Task { await store.refreshAll() } }
                    if store.storeClient != nil {
                        Button("Connectors & Skills") { showsStore = true }
                    }
                    Button("Disconnect", role: .destructive) { store.disconnect() }
                } label: { Image(systemName: "ellipsis.circle") }
            }
        }
        .sheet(isPresented: $showsStore) {
            if let client = store.storeClient { StoreScreen(client: client) }
        }
    }

    private func callButton(_ row: SidebarRow) -> some View {
        Button { store.placeCall(calls, agent: row.agent.name) } label: {
            Label("Call \(row.agent.displayName)", systemImage: "phone.fill")
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
                    RosterRowContent(row: row)
                        .padding(.horizontal, 16)
                        .padding(.vertical, 11)
                }
                    .buttonStyle(RowPressStyle())
                    .contextMenu { callButton(row) }
                if row.id != rows.last?.id {
                    Divider().padding(.leading, 80)
                }
            }
        }
    }

    @ViewBuilder
    private var emptyState: some View {
        Group {
            if let problem = store.loadError {
                CharacterState(sleepy: true, title: "Couldn't reach your deck",
                               message: problem + "\nPull down to try again.")
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

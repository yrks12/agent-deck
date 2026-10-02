import SwiftUI
import DeckKit

/// **The roster, drawn like the iPhone's.** The chief's card, the pulse line
/// and "N things need your attention" on top; then Pinned and the sections,
/// each desk once, as the phone's rows (`RosterViews.swift`, the same file the
/// phone compiles). Search, "+", Connectors & Skills and the account stay:
/// they are the Mac's.
public struct SidebarView: View {
    @ObservedObject var store: DeckStore
    @State private var query = ""
    /// Opens the side panel, where the attention cards are.
    let onShowAttention: () -> Void

    public init(store: DeckStore, onShowAttention: @escaping () -> Void = {}) {
        self.store = store
        self.onShowAttention = onShowAttention
    }

    private var manualSetupIsShowing: Binding<Bool> {
        Binding(
            get: { store.isShowingManualSetup },
            set: { if !$0 { store.dismissManualSetup() } }
        )
    }

    public var body: some View {
        // Free unless DECK_DIAGNOSE=1. The sidebar is on screen every second
        // the app is, so if anything is being rebuilt continuously it is this.
        let _ = Diagnostics.count("sidebar.body")
        VStack(spacing: 0) {
            SidebarHeader(
                query: $query,
                onSearch: { store.search($0) },
                onAdd: { store.beginNewAgent() },
                onManualSetup: { store.showManualSetup() }
            )
            Divider()
            // Above the roster and outside the List: it is not a desk, so it is
            // not selectable, sortable or searchable alongside ones that are.
            if let pending = store.pending {
                ProvisionalRow(row: pending.row) { store.cancelPending() }
                Divider()
            }
            content
            Divider()
            SidebarFooterView(
                footer: SidebarFooter.forCurrentOwner(),
                storeIsReachable: store.storeClient != nil,
                onOpenStore: { store.showStore() }
            )
        }
        .background(DeckPalette.canvas)
        .navigationSplitViewColumnWidth(
            min: Theme.sidebarWidth.lowerBound,
            ideal: 300,
            max: Theme.sidebarWidth.upperBound
        )
        .sheet(isPresented: manualSetupIsShowing) {
            ManualAgentSheet(store: store)
        }
        .sheet(item: Binding(get: { store.accountSignIn },
                             set: { if $0 == nil { store.endSignIn() } })) { request in
            if let client = store.accountLoginClient {
                AccountSignInSheet(client: client, request: request,
                                   onDone: { Task { await store.accountSignedIn() } },
                                   dismiss: { store.endSignIn() })
            }
        }
    }

    @ViewBuilder
    private var content: some View {
        // A search that matched nothing is not an empty deck and not a failure.
        // It gets its own screen so those three can never be confused.
        if let outcome = store.searchOutcome, outcome.matchCount == 0 {
            ContentUnavailableView(
                outcome.emptyTitle ?? "No matches",
                systemImage: "magnifyingglass",
                description: Text(outcome.emptyDescription ?? "")
            )
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            states
        }
    }

    private var states: some View {
        Group {
            switch store.roster {
            case .loading:
                ProgressView("Loading roster")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .empty:
                ContentUnavailableView(
                    "No agents yet",
                    systemImage: "person.2.slash",
                    description: Text("This deck has no desks on its roster.")
                )
            case .failed(let error):
                // "Can't reach the deck" was said here for every failure,
                // including an empty Keychain with the deck answering 200.
                // The heading now comes from the cause.
                FailureView(failure: FailurePresentation.make(error)) {
                    Task { await store.loadRoster() }
                }
            case .loaded(let snapshot):
                loaded(snapshot)
            }
        }
    }

    private func loaded(_ snapshot: SidebarSnapshot) -> some View {
        List(selection: selectionBinding) {
            // Every desk once, Pinned first — the phone's list. A `List` keyed
            // by desk cannot hold a desk twice, and the phone never draws one
            // twice either.
            ForEach(snapshot.listedSections) { section in
                Section {
                    ForEach(section.rows) { row in
                        AgentRow(row: row, accountBadge: accountBadge(for: row),
                                 accountMove: store.accountMoves[row.agent.name],
                                 dismissAccountMove: { store.dismissAccountMove(row.agent.name) })
                            .tag(row.agent.name)
                            .contextMenu { moveMenu(for: row) }
                    }
                    if section.hasOverflow {
                        MoreUnreadsPill(label: section.overflowLabel)
                            .listRowSeparator(.hidden)
                    }
                } header: {
                    Text(section.name.uppercased())
                        .font(.caption.weight(.semibold))
                        .tracking(0.6)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .listStyle(.sidebar)
        .scrollContentBackground(.hidden)
        .background(DeckPalette.canvas)
        // Above the list rather than its first rows: the chief stays put
        // while the roster scrolls. How much of the phone's top block fits is
        // decided by the column's height, so the desks themselves are never
        // pushed off a short window (`TheRosterIsOnScreenTests`, down to the
        // window's 560pt floor).
        .modifier(TopBlockFitting { room in topBlock(snapshot, room: room) })
    }

    /// The phone's top block, as much of it as the column has room for.
    @ViewBuilder
    private func topBlock(_ snapshot: SidebarSnapshot, room: SidebarRoom) -> some View {
        VStack(spacing: 10) {
            if let chief = snapshot.chief {
                ChiefBlock(row: chief, isSelected: store.selectedAgentName == chief.agent.name,
                           compact: room != .roomy) {
                    store.select(agent: chief.agent.name, threadID: chief.threadID)
                }
                .contextMenu { moveMenu(for: chief) }
            }
            if room >= .medium, !snapshot.allRows.isEmpty {
                RosterPulse(rows: snapshot.allRows + snapshot.pinned)
                    .padding(.horizontal, 4)
            }
            if room >= .short, !store.attention.isEmpty {
                Button {
                    DeckHaptics.tap()
                    onShowAttention()
                } label: {
                    AttentionBanner(count: store.attention.count, hint: "Show")
                }
                .buttonStyle(.plain)
                .help("Open the side panel with the cards that need you")
                .accessibilityHint("Opens the side panel with the attention cards")
            }
            // Not gated on room: a login about to run out is the one thing
            // here he cannot fix by waiting.
            if let usage = store.usage {
                AccountExpiryBanner(
                    warnings: AccountsPresentation.expiryWarnings(usage.accounts),
                    onSignIn: store.canSignInToAccounts ? { store.beginSignIn($0) } : nil)
            }
            if room >= .medium, let usage = store.usage {
                UsageMeterView(usage: usage, compact: room != .roomy)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 10)
        .background(DeckPalette.canvas)
    }

    private func accountBadge(for row: SidebarRow) -> String? {
        guard let usage = store.usage else { return nil }
        return AccountsPresentation.badge(for: row.agent, accounts: usage.accounts,
                                          defaultID: usage.policy?.defaultAccount)
    }

    /// "Move to account…", on a right-click. Nothing on a deck with one account.
    private func moveMenu(for row: SidebarRow) -> some View {
        MoveToAccountMenu(agent: row.agent, usage: store.usage) { account in
            Task { await store.moveDesk(row.agent.name, to: account) }
        }
    }

    private var selectionBinding: Binding<String?> {
        Binding(
            // While the unsaved thread is open it is what the pane is showing,
            // so no desk below is highlighted as if it were.
            get: { store.pending == nil ? store.selectedAgentName : nil },
            set: { name in
                guard let name,
                      case .loaded(let snapshot) = store.roster,
                      let row = snapshot.allRows.first(where: { $0.id == name })
                else { return }
                store.select(agent: row.agent.name, threadID: row.threadID)
            }
        )
    }
}

/// **The chief of staff, in its own block above the roster.**
///
/// He talks to one desk; that desk hires and directs the rest. Mixed into the
/// list it was one row of twelve, sorted by whoever spoke last and liable to
/// scroll away. Here it stays put, at a size that says it is not one of them.
struct ChiefBlock: View {
    let row: SidebarRow
    var isSelected = false
    var compact = false
    let onSelect: () -> Void

    var body: some View {
        Button {
            DeckHaptics.tap()
            onSelect()
        } label: {
            ChiefHero(row: row, isSelected: isSelected, compact: compact)
        }
        .buttonStyle(.plain)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(spokenLabel)
        .accessibilityAddTraits(isSelected ? [.isButton, .isSelected] : .isButton)
    }

    private var spokenLabel: String {
        var parts = ["\(row.agent.displayName), chief of staff"]
        if let word = row.attention.spokenWord { parts.append(word) }
        if row.isUnread { parts.append("\(row.unreadCount) unread") }
        parts.append(row.previewText)
        return parts.joined(separator: ", ")
    }
}

/// The unsaved "+" thread, in the roster's place but plainly not one of it:
/// dashed edge, "not created yet", and a cancel that deletes nothing because
/// nothing was ever made.
struct ProvisionalRow: View {
    let row: PendingRow
    let onCancel: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: "person.crop.circle.badge.plus")
                .font(.title2)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(row.title)
                        .font(.system(size: 13, weight: .semibold))
                    Text("Not created yet")
                        .font(.caption2.weight(.medium))
                        .padding(.horizontal, 5)
                        .padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                        .foregroundStyle(.secondary)
                }
                Text(row.detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 4)
            Button(action: onCancel) {
                Image(systemName: "xmark")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.plain)
            .help("Discard this — nothing has been created")
            .accessibilityLabel("Discard the new agent you have not created yet")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: 8)
                .strokeBorder(style: StrokeStyle(lineWidth: 1, dash: [4, 3]))
                .foregroundStyle(.tertiary)
                .padding(.horizontal, 6)
        )
        .accessibilityElement(children: .contain)
        .accessibilityLabel(row.accessibilityLabel)
    }
}

struct AgentRow: View {
    let row: SidebarRow
    var accountBadge: String?
    var accountMove: AccountMoveStatus?
    var dismissAccountMove: () -> Void = {}

    var body: some View {
        // The good signal for `SidebarBodyWorkTests`: a count of zero for the
        // work below means nothing only if this row actually drew, and an
        // off-screen `List` does not realise its rows. Free unless
        // DECK_DIAGNOSE=1.
        let _ = Diagnostics.count("sidebar.row.body")
        // The phone's row, from the file the phone compiles.
        // One line of preview and a 40pt face: the Mac's type is smaller
        // than the phone's, and five desks must fit a 560pt window.
        VStack(alignment: .leading, spacing: 4) {
            RosterRowContent(row: row, avatarSize: 40, previewLines: 1, accountBadge: accountBadge)
                .accessibilityElement(children: .ignore)
                .accessibilityLabel(accessibilityLabel)
            // Outside the row's one spoken element, so the dismiss stays reachable.
            if let accountMove {
                AccountMoveNotice(status: accountMove, dismiss: dismissAccountMove)
                    .padding(.leading, 54)
            }
        }
        .padding(.vertical, Theme.rowVerticalPadding)
    }

    private var accessibilityLabel: String {
        var parts = [row.agent.displayName, row.agent.title]
        if let accountBadge { parts.append("account \(accountBadge)") }
        // The dot is a colour, and a colour is not a status. This is where it
        // becomes a word.
        if let word = row.attention.spokenWord { parts.append(word) }
        if row.isUnread { parts.append("\(row.unreadCount) unread") }
        if let spoken = row.spokenTimestampLabel { parts.append(spoken) }
        // The badge already announces "needs a look"; VoiceOver reads the
        // reason once, from the sentence, not the badge word and then the
        // sentence both.
        parts.append(row.agent.blocked?.sentence ?? row.previewText)
        return parts.joined(separator: ", ")
    }
}

/// What tells a blocked desk apart from one that has simply not been started:
/// not colour alone, and not "OFFLINE" repeated — a word plus an icon plus a
/// full sentence one hop away, in the panel.
struct BlockedBadge: View {
    let blocked: Blocked

    var body: some View {
        FlatLabel(Blocked.badgeText, systemImage: "exclamationmark.triangle.fill")
            .font(.caption2.weight(.semibold))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Color.orange.opacity(0.18), in: Capsule())
            .foregroundStyle(Color.orange)
            // The word alone is not the point of a screen reader visit here;
            // the sentence is, and it is already read as the row's preview
            // line (see `accessibilityLabel` above), so this glyph stays out
            // of the way rather than saying "Needs a look" a second time.
            .accessibilityHidden(true)
    }
}

/// The unread / waiting dot at a row's right edge.
struct RowDotView: View {
    let dot: RowDot
    var size: CGFloat = 9

    var body: some View {
        switch dot {
        case .none:
            EmptyView()
        case .unread:
            Circle().fill(DeckPalette.ink).frame(width: size, height: size)
                .overlay(Circle().strokeBorder(.background, lineWidth: size > 10 ? 2 : 0))
        case .waiting:
            Circle().fill(AvatarView.tint(for: .waitingForYou) ?? .orange).frame(width: size, height: size)
        }
    }
}

struct MoreUnreadsPill: View {
    let label: String

    var body: some View {
        HStack {
            Spacer()
            Text(label)
                .font(.caption.weight(.medium))
                .padding(.horizontal, 10)
                .padding(.vertical, 4)
                .background(.quaternary, in: Capsule())
                .foregroundStyle(.secondary)
            Spacer()
        }
        .padding(.vertical, 4)
        .accessibilityLabel(label)
    }
}


/// The search field and the "+", above the roster.
struct SidebarHeader: View {
    @Binding var query: String
    let onSearch: (String) -> Void
    let onAdd: () -> Void
    let onManualSetup: () -> Void

    var body: some View {
        HStack(spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                TextField("Search agents", text: $query)
                    .textFieldStyle(.plain)
                    .onChange(of: query) { _, value in onSearch(value) }
                    .accessibilityLabel("Search agents by name, title or last message")
                if !query.isEmpty {
                    Button {
                        query = ""
                        onSearch("")
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Clear the search")
                }
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))

            // Clicking "+" starts the conversation. The form is on the menu
            // behind it — a second door, not one he can hit by accident.
            Menu {
                Button(PendingThread.manualDoorTitle, action: onManualSetup)
                    .help("Fill in a name, title, folder, engine and boss yourself")
            } label: {
                Image(systemName: "plus")
                    .font(.body.weight(.medium))
                    .frame(width: 22, height: 22)
            } primaryAction: {
                onAdd()
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
            .help("Talk to a new agent about what you want it for")
            .accessibilityLabel("Add an agent")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(DeckPalette.canvas)
    }
}

/// Connectors & Skills, and who is signed in. The first row opens the store;
/// on a deck whose connection cannot serve `/v1/store/*` it says so and stays
/// a plain line rather than opening a panel that could never load.
struct SidebarFooterView: View {
    let footer: SidebarFooter
    var storeIsReachable = true
    var onOpenStore: () -> Void = {}

    var body: some View {
        VStack(spacing: 0) {
            if footer.storeIsAvailable && storeIsReachable {
                Button(action: onOpenStore) {
                    row(icon: "puzzlepiece.extension", title: footer.storeTitle,
                        detail: footer.storeDetail, opens: true)
                }
                .buttonStyle(.plain)
                .help("Browse connectors and skills and add them to your desks")
                .accessibilityHint("Opens the Connectors and Skills store")
            } else {
                row(icon: "puzzlepiece.extension", title: footer.storeTitle,
                    detail: "Not available on this deck connection", opens: false)
            }
            Divider().padding(.leading, 44)
            HStack(spacing: 10) {
                // A person, not a desk: initials on a circle, like the reference.
                Circle()
                    .fill(Color.secondary.opacity(0.28))
                    .frame(width: 26, height: 26)
                    .overlay(
                        Text(footer.accountLook.initials)
                            .font(.system(size: 10, weight: .semibold, design: .rounded))
                            .foregroundStyle(.secondary)
                    )
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 1) {
                    Text(footer.accountTitle).font(.callout.weight(.medium))
                    Text(footer.accountDetail).font(.caption).foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(footer.accountTitle). \(footer.accountDetail)")
        }
        .background(DeckPalette.canvas)
    }

    private func row(icon: String, title: String, detail: String, opens: Bool) -> some View {
        HStack(spacing: 10) {
            Image(systemName: icon)
                .frame(width: 24)
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).font(.callout.weight(.medium))
                Text(detail).font(.caption).foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if opens {
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.tertiary)
                    .accessibilityHidden(true)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .contentShape(Rectangle())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title). \(detail)")
    }
}

/// **How much of the phone's top block a roster column has room for.** Read
/// off the height the list is given, so the rows under it keep at least five
/// desks' worth of space: a short window drops the usage card to bars, then
/// the pulse line and the meter, then the attention card, never the desks.
enum SidebarRoom: Int, Comparable {
    case tight, short, medium, roomy

    static func < (lhs: SidebarRoom, rhs: SidebarRoom) -> Bool { lhs.rawValue < rhs.rawValue }

    static func forListHeight(_ height: CGFloat) -> SidebarRoom {
        if height >= 640 { return .roomy }
        if height >= 530 { return .medium }
        if height >= 430 { return .short }
        return .tight
    }
}

/// Puts the top block above the list, sized by the list's own height.
private struct TopBlockFitting<Block: View>: ViewModifier {
    let block: (SidebarRoom) -> Block

    func body(content: Content) -> some View {
        GeometryReader { geo in
            content.safeAreaInset(edge: .top, spacing: 0) {
                block(SidebarRoom.forListHeight(geo.size.height))
            }
        }
    }
}

import SwiftUI
import DeckKit

// MARK: - the state behind the panel

/// Holds what the Connectors & Skills panel has loaded. The decisions — which
/// badge, what an install needs, what the catalog is asked for — are
/// `StoreBrowser`'s and `StoreInstallDraft`'s; this only does the fetching.
///
/// Trusted catalogs are fetched once per open, per tab, and searched on the
/// Mac. "Other sources" asks the deck on every (debounced) search, because the
/// deck never caches unverified items.
@MainActor
public final class StorePanelModel: ObservableObject {
    /// One page is enough for a trusted catalog today; if the deck says there
    /// are more, the panel says so rather than looking complete.
    static let pageSize = 100

    let client: StoreClient
    public let desks: [String]

    @Published public var browser = StoreBrowser()
    @Published public private(set) var trusted: [String: LoadState<StoreCatalogPage>] = [:]
    @Published public private(set) var unverified: LoadState<StoreCatalogPage> = .loading
    @Published public private(set) var installed: LoadState<StoreInstalledPage> = .loading
    /// The last update/remove, one line per desk, or why it was refused.
    @Published public private(set) var installedNote: String?
    @Published public private(set) var busyRows: Set<String> = []

    public init(client: StoreClient, desks: [String]) {
        self.client = client
        self.desks = desks
    }

    /// What the list draws for the current tab and mode.
    public var listState: LoadState<[StoreItem]> {
        guard let kind = browser.tab.kind else { return .empty }
        let page = browser.showsUnverified ? unverified : (trusted[kind] ?? .loading)
        switch page {
        case .loading: return .loading
        case .empty: return .empty
        case .failed(let error): return .failed(error)
        case .loaded(let page):
            let items = browser.visible(page.items)
            return items.isEmpty ? .empty : .loaded(items)
        }
    }

    /// "Showing 100 of 140" when the deck holds more than one page.
    public var truncationNote: String? {
        guard let kind = browser.tab.kind else { return nil }
        let page = browser.showsUnverified ? unverified : (trusted[kind] ?? .loading)
        guard case .loaded(let loaded) = page, loaded.total > loaded.items.count else { return nil }
        return "Showing \(loaded.items.count) of \(loaded.total). Search to narrow it down."
    }

    public var staleNote: String? {
        guard let kind = browser.tab.kind, !browser.showsUnverified,
              case .loaded(let page)? = trusted[kind], page.stale else { return nil }
        return "The deck could not refresh the catalog, so this is its last good copy."
    }

    /// Called whenever the tab, mode or (in Other sources) the search changes.
    public func load() async {
        if browser.tab == .installed { await loadInstalled(); return }
        guard let request = browser.catalogRequest else { return }
        if request.trust == .trusted {
            if case .loaded? = trusted[request.kind] { return }
            trusted[request.kind] = .loading
            trusted[request.kind] = await fetch(request)
        } else {
            unverified = .loading
            // Debounced: a keystroke that is followed by another is not sent.
            try? await Task.sleep(nanoseconds: 350_000_000)
            guard !Task.isCancelled else { return }
            let answer = await fetch(request)
            guard !Task.isCancelled else { return }
            unverified = answer
        }
    }

    private func fetch(_ request: StoreCatalogRequest) async -> LoadState<StoreCatalogPage> {
        do {
            let page = try await client.storeCatalog(
                kind: request.kind, query: request.query, trust: request.trust,
                limit: Self.pageSize, offset: 0)
            return .loaded(page)
        } catch {
            return .failed(Self.deckError(error))
        }
    }

    public func retry() async {
        if let kind = browser.tab.kind, !browser.showsUnverified { trusted[kind] = nil }
        await load()
    }

    public func loadInstalled() async {
        if case .loaded = installed {} else { installed = .loading }
        do {
            let page = try await client.storeInstalled(desk: nil)
            installed = page.desks.allSatisfy { $0.items.isEmpty } ? .empty : .loaded(page)
        } catch {
            installed = .failed(Self.deckError(error))
        }
    }

    /// A Connect installed something: drop what `installed_on` it changed.
    public func didInstallElsewhere() {
        trusted = [:]
        if case .loaded = installed { Task { await loadInstalled() } }
    }

    /// An install changes `installed_on`, so the trusted pages are dropped and
    /// fetched again the next time their tab is drawn.
    public func install(_ request: StoreInstallRequest) async throws -> StoreInstallResult {
        do {
            let result = try await client.storeInstall(request)
            trusted = [:]
            if case .loaded = installed { await loadInstalled() }
            return result
        } catch {
            throw Self.deckError(error)
        }
    }

    public func update(_ row: StoreInstalled) async {
        await act(on: row) {
            try await $0.storeUpdate(id: row.id, desks: .desks([row.desk])).reload
        }
    }

    public func remove(_ row: StoreInstalled) async {
        await act(on: row) {
            try await $0.storeUninstall(id: row.id, desks: .desks([row.desk])).reload
        }
    }

    private func act(on row: StoreInstalled,
                     _ call: (StoreClient) async throws -> [StoreReload]) async {
        let key = Self.rowKey(row)
        busyRows.insert(key)
        defer { busyRows.remove(key) }
        do {
            let reload = try await call(client)
            installedNote = reload.map(StoreBrowser.reloadLine).joined(separator: "\n")
            trusted = [:]
            await loadInstalled()
        } catch {
            installedNote = Self.deckError(error).userFacingText
        }
    }

    static func rowKey(_ row: StoreInstalled) -> String { "\(row.desk)|\(row.id)" }

    static func deckError(_ error: Error) -> DeckError {
        (error as? DeckError) ?? .transport(error.localizedDescription)
    }
}

// MARK: - the panel

/// **Connectors & Skills.** A sheet over the window: search, three tabs, a
/// list of cards, and a detail with the install control. Styled like the rest
/// of the app — plain stacks on the window colour, quaternary fills, no form
/// chrome.
public struct StorePanelView: View {
    @StateObject private var model: StorePanelModel
    @State private var selected: StoreItem?
    let onClose: () -> Void

    public init(client: StoreClient, desks: [String], onClose: @escaping () -> Void) {
        _model = StateObject(wrappedValue: StorePanelModel(client: client, desks: desks))
        self.onClose = onClose
    }

    init(model: StorePanelModel, selected: StoreItem? = nil, onClose: @escaping () -> Void = {}) {
        _model = StateObject(wrappedValue: model)
        _selected = State(initialValue: selected)
        self.onClose = onClose
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let item = selected {
                StoreItemDetailView(item: item, model: model, onBack: { selected = nil })
                    .id(item.id)
            } else {
                content
                if model.browser.tab != .installed {
                    Divider()
                    otherSourcesToggle
                }
            }
        }
        .frame(minWidth: 620, idealWidth: 720, minHeight: 520, idealHeight: 680)
        .background(Color(nsColor: .windowBackgroundColor))
        .task(id: model.browser.catalogRequest) {
            // Installed is loaded by its own `.task`; this one is the catalog.
            if model.browser.tab != .installed { await model.load() }
        }
    }

    // MARK: header

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: "puzzlepiece.extension")
                    .font(.title3)
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
                Text("Connectors & Skills")
                    .font(.title3.weight(.semibold))
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                Button("Done", action: onClose)
                    .keyboardShortcut(.cancelAction)
            }
            HStack(spacing: 10) {
                searchField
                DeckPillPicker(
                    selection: Binding(
                        get: { model.browser.tab },
                        set: { tab in
                            selected = nil
                            model.browser.tab = tab
                        }),
                    options: StoreTab.allCases.map { ($0, $0.title) },
                    label: "Show connectors, skills, plugins or what is installed")
                .fixedSize()
            }
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }

    private var searchField: some View {
        HStack(spacing: 6) {
            Image(systemName: "magnifyingglass")
                .foregroundStyle(.secondary)
                .accessibilityHidden(true)
            TextField("Search connectors, skills and plugins", text: $model.browser.searchText)
                .textFieldStyle(.plain)
                .accessibilityLabel("Search connectors, skills and plugins")
                .onSubmit { selected = nil }
            if !model.browser.searchText.isEmpty {
                Button {
                    model.browser.searchText = ""
                } label: {
                    Image(systemName: "xmark.circle.fill").foregroundStyle(.secondary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Clear the search")
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 6)
        .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
        .disabled(model.browser.tab == .installed)
    }

    // MARK: list

    @ViewBuilder
    private var content: some View {
        if model.browser.tab == .installed {
            StoreInstalledList(model: model)
        } else {
            switch model.listState {
            case .loading:
                ProgressView(model.browser.showsUnverified ? "Searching other sources" : "Loading the catalog")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .failed(let error):
                StoreFailure(error: error) { Task { await model.retry() } }
            case .empty:
                ContentUnavailableView(emptyTitle, systemImage: "magnifyingglass",
                                       description: Text(emptyDetail))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .loaded(let items):
                ScrollView {
                    LazyVStack(spacing: 8) {
                        if let note = model.staleNote {
                            StoreNote(text: note, systemImage: "clock.arrow.circlepath")
                        }
                        ForEach(items) { item in
                            StoreCard(item: item) { selected = item }
                        }
                        if let note = model.truncationNote {
                            StoreNote(text: note, systemImage: "ellipsis.circle")
                        }
                    }
                    .padding(16)
                }
            }
        }
    }

    private var emptyTitle: String {
        model.browser.searchText.isEmpty ? "Nothing here yet" : "No matches"
    }

    private var emptyDetail: String {
        let what = StoreKindText.plural(model.browser.tab.kind ?? "connector")
        if model.browser.showsUnverified && model.browser.searchText.isEmpty {
            return "Type what you are looking for to search other sources."
        }
        if model.browser.searchText.isEmpty {
            return "The deck's trusted catalog has no \(what) yet."
        }
        return "No \(what) match \u{201C}\(model.browser.searchText)\u{201D}."
            + (model.browser.showsUnverified ? "" : " Turn on Other sources to search beyond trusted publishers.")
    }

    private var otherSourcesToggle: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                Text("Other sources (unverified)")
                    .accessibilityHidden(true)
                Toggle("", isOn: Binding(
                    get: { model.browser.showsUnverified },
                    set: { model.browser.showsUnverified = $0 }))
                    .labelsHidden()
                    .toggleStyle(.switch).tint(DeckPalette.working)
                    .controlSize(.small)
                    .accessibilityLabel("Other sources (unverified)")
            }
            if model.browser.showsUnverified {
                FlatLabel(StoreBrowser.unverifiedWarning, systemImage: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(.orange)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 16)
        .padding(.vertical, 10)
    }
}

// MARK: - a card

struct StoreCard: View {
    let item: StoreItem
    let open: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Button(action: open) {
                HStack(alignment: .top, spacing: 12) {
                    StoreKindIcon(kind: item.kind)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Text(item.title).font(.body.weight(.semibold)).lineLimit(1)
                            StoreBadge(trust: item.trust)
                            Spacer(minLength: 0)
                            if !item.installedOn.isEmpty {
                                Text("Installed")
                                    .font(.caption)
                                    .foregroundStyle(.green)
                            }
                        }
                        Text(item.publisher).font(.caption).foregroundStyle(.secondary)
                        Text(item.description)
                            .font(.callout)
                            .foregroundStyle(.primary.opacity(0.85))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityElement(children: .combine)
            .accessibilityHint("Opens the details and the install options")
            StoreMetaRow(item: item)
                .padding(.leading, 44)
        }
        .padding(12)
        .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 10))
    }
}

struct StoreKindIcon: View {
    let kind: String

    var body: some View {
        Image(systemName: StoreKindText.symbol(kind))
            .font(.system(size: 15, weight: .medium))
            .frame(width: 32, height: 32)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
            .foregroundStyle(.secondary)
            .accessibilityHidden(true)
    }
}

/// "Official" / "Verified", or nothing at all.
struct StoreBadge: View {
    let trust: String

    var body: some View {
        if let text = StoreBrowser.badge(forTrust: trust) {
            FlatLabel(text, systemImage: "checkmark.seal.fill")
                .font(.caption2.weight(.semibold))
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(DeckPalette.field, in: Capsule())
                .foregroundStyle(DeckPalette.working)
        }
    }
}

struct StoreMetaRow: View {
    let item: StoreItem

    var body: some View {
        HStack(spacing: 12) {
            if let stars = StoreBrowser.starsText(item.stars) {
                FlatLabel(stars, systemImage: "star")
                    .accessibilityLabel("\(item.stars ?? 0) stars")
            }
            if let updated = StoreBrowser.updatedText(item.updatedAt) {
                Text(updated)
            }
            if let repo = item.repo, let url = URL(string: repo) {
                Link(destination: url) {
                    FlatLabel(url.host.map { "\($0)\(url.path)" } ?? repo, systemImage: "link")
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .accessibilityLabel("Open the repository")
            }
            Spacer(minLength: 0)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }
}

struct StoreNote: View {
    let text: String
    let systemImage: String

    var body: some View {
        FlatLabel(text, systemImage: systemImage)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .fixedSize(horizontal: false, vertical: true)
    }
}

struct StoreFailure: View {
    let error: DeckError
    let retry: () -> Void

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "exclamationmark.triangle")
                .font(.title2)
                .foregroundStyle(.orange)
                .accessibilityHidden(true)
            Text("Could not load the store")
                .font(.headline)
            Text(error.userFacingText)
                .font(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if error.isRetryable {
                Button("Try again", action: retry)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// The words and glyph for a `kind`.
enum StoreKindText {
    static func plural(_ kind: String) -> String {
        switch kind {
        case "skill": return "skills"
        case "plugin": return "plugins"
        default: return "connectors"
        }
    }

    static func singular(_ kind: String) -> String {
        switch kind {
        case "skill": return "Skill"
        case "plugin": return "Plugin"
        default: return "Connector"
        }
    }

    static func symbol(_ kind: String) -> String {
        switch kind {
        case "skill": return "book.closed"
        case "plugin": return "puzzlepiece"
        default: return "powerplug"
        }
    }
}

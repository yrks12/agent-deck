import SwiftUI
import DeckKit

/// **Connectors & Skills, on the phone.** The same catalog and the same
/// decisions as the Mac (`StoreBrowser`), cut down to what a thumb needs:
/// trusted items only, install on every desk, and remove. Anything that needs
/// a typed key, an OAuth sign-in or a choice of desks says to use the Mac.
@MainActor
final class PhoneStoreCatalog: ObservableObject {
    let client: StoreClient
    @Published var browser = StoreBrowser()
    @Published private(set) var pages: [String: LoadState] = [:]
    @Published private(set) var installed: InstalledState = .loading
    @Published private(set) var note: String?
    @Published private(set) var busy: Set<String> = []

    enum LoadState { case loading, failed(String), loaded([StoreItem], total: Int) }
    enum InstalledState { case loading, failed(String), loaded([StoreDeskInstalls]) }

    init(client: StoreClient) { self.client = client }

    func load() async {
        guard let request = browser.catalogRequest else { await loadInstalled(); return }
        if case .loaded? = pages[request.kind] { return }
        pages[request.kind] = .loading
        do {
            let page = try await client.storeCatalog(kind: request.kind, query: nil, trust: .trusted,
                                                     limit: 100, offset: 0)
            pages[request.kind] = .loaded(page.items, total: page.total)
        } catch {
            pages[request.kind] = .failed(Self.text(error))
        }
    }

    func loadInstalled() async {
        do {
            installed = .loaded(try await client.storeInstalled(desk: nil).desks.filter { !$0.items.isEmpty })
        } catch {
            installed = .failed(Self.text(error))
        }
    }

    /// Only trusted items with nothing to type reach this: `StoreInstallDraft`
    /// decides, and a draft that is not ready sends nothing.
    func installEverywhere(_ item: StoreItem) async {
        var draft = StoreInstallDraft(item: item)
        guard let request = draft.takeRequest() else { note = draft.problem; return }
        await run(item.id) { try await $0.storeInstall(request).reload }
    }

    func remove(_ row: StoreInstalled) async {
        await run("\(row.desk)|\(row.id)") {
            try await $0.storeUninstall(id: row.id, desks: .desks([row.desk])).reload
        }
    }

    private func run(_ key: String, _ call: (StoreClient) async throws -> [StoreReload]) async {
        busy.insert(key)
        defer { busy.remove(key) }
        do {
            note = try await call(client).map(StoreBrowser.reloadLine).joined(separator: "\n")
            pages = [:]
            await loadInstalled()
            await load()
        } catch {
            note = Self.text(error)
        }
    }

    static func text(_ error: Error) -> String {
        (error as? DeckError)?.userFacingText ?? error.localizedDescription
    }

    /// Why the phone will not install this one, or nil when it will.
    static func phoneCannotInstall(_ item: StoreItem) -> String? {
        let draft = StoreInstallDraft(item: item)
        if draft.showsOAuthNote { return item.whyNot ?? "OAuth sign-in isn't supported yet." }
        if !draft.secretFields.isEmpty { return "Needs a key — install it from the Mac." }
        return draft.canInstall ? nil : draft.problem
    }
}

struct StoreScreen: View {
    @StateObject private var model: PhoneStoreCatalog
    @Environment(\.dismiss) private var dismiss

    init(client: StoreClient) {
        _model = StateObject(wrappedValue: PhoneStoreCatalog(client: client))
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    Picker("Section", selection: $model.browser.tab) {
                        ForEach(StoreTab.allCases) { Text($0.title).tag($0) }
                    }
                    .pickerStyle(.segmented)
                    .listRowBackground(Color.clear)
                }
                if let note = model.note {
                    Section { Text(note).font(.footnote).foregroundStyle(.secondary) }
                }
                if model.browser.tab == .installed { installedRows } else { catalogRows }
            }
            .searchable(text: $model.browser.searchText, prompt: "Search connectors and skills")
            .navigationTitle("Connectors & Skills")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .confirmationAction) { Button("Done") { dismiss() } }
            }
            .task(id: model.browser.tab) { await model.load() }
        }
    }

    @ViewBuilder
    private var catalogRows: some View {
        switch model.browser.tab.kind.flatMap({ model.pages[$0] }) ?? .loading {
        case .loading:
            ProgressView("Loading the catalog").frame(maxWidth: .infinity)
        case .failed(let text):
            Text(text).foregroundStyle(.red)
        case .loaded(let all, let total):
            let items = model.browser.visible(all)
            if items.isEmpty {
                Text(model.browser.searchText.isEmpty ? "Nothing here yet." : "No matches.")
                    .foregroundStyle(.secondary)
            }
            ForEach(items) { item in row(item) }
            if total > all.count {
                Text("Showing \(all.count) of \(total). Search to narrow it down.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private func row(_ item: StoreItem) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                Text(item.title).font(.body.weight(.semibold))
                if let badge = StoreBrowser.badge(forTrust: item.trust) {
                    Label(badge, systemImage: "checkmark.seal.fill")
                        .font(.caption2.weight(.semibold))
                        .foregroundStyle(Color.accentColor)
                }
            }
            Text(item.publisher).font(.caption).foregroundStyle(.secondary)
            Text(item.description).font(.subheadline).lineLimit(2)
            HStack(spacing: 10) {
                if let stars = StoreBrowser.starsText(item.stars) { Label(stars, systemImage: "star") }
                if let updated = StoreBrowser.updatedText(item.updatedAt) { Text(updated) }
            }
            .font(.caption).foregroundStyle(.secondary)
            if let why = PhoneStoreCatalog.phoneCannotInstall(item) {
                Text(why).font(.caption).foregroundStyle(.secondary)
            } else if model.busy.contains(item.id) {
                ProgressView()
            } else {
                Button(item.installedOn.isEmpty ? "Install on all desks" : "Install again on all desks") {
                    Task { await model.installEverywhere(item) }
                }
                .buttonStyle(.bordered)
            }
        }
        .padding(.vertical, 4)
    }

    @ViewBuilder
    private var installedRows: some View {
        switch model.installed {
        case .loading:
            ProgressView("Loading what is installed").frame(maxWidth: .infinity)
        case .failed(let text):
            Text(text).foregroundStyle(.red)
        case .loaded(let desks):
            if desks.isEmpty { Text("Nothing installed yet.").foregroundStyle(.secondary) }
            ForEach(desks, id: \.desk) { desk in
                Section(desk.desk) {
                    ForEach(desk.items) { row in
                        HStack {
                            VStack(alignment: .leading) {
                                Text(row.title)
                                Text(StoreBrowser.versionText(row))
                                    .font(.caption.monospaced()).foregroundStyle(.secondary)
                            }
                            Spacer()
                            if model.busy.contains("\(row.desk)|\(row.id)") { ProgressView() }
                        }
                        .swipeActions {
                            Button("Remove", role: .destructive) { Task { await model.remove(row) } }
                        }
                        .contextMenu {
                            Button("Remove from \(row.desk)", role: .destructive) {
                                Task { await model.remove(row) }
                            }
                        }
                    }
                }
            }
        }
    }
}

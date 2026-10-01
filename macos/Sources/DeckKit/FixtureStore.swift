import Foundation

// MARK: - Connectors & Skills, canned

/// A store worth looking at with no deck: official connectors and skills, one
/// that needs a key, one behind OAuth, and — only under "Other sources" — an
/// unverified one to warn about. Installs are accepted and answered the way
/// the deck answers them; nothing is kept, and no typed key is looked at.
extension FixtureDeckClient: StoreClient {

    static let storeItems: [StoreItem] = [
        fixtureItem("mcp:com.microsoft/microsoft-learn-mcp", "connector", "Microsoft Learn",
                    "Microsoft", "Search Microsoft's official documentation and code samples.",
                    repo: "https://github.com/MicrosoftDocs/mcp", stars: 1_283,
                    updated: "2026-09-22T10:00:00Z", version: "1.4.0", installedOn: ["chief"]),
        fixtureItem("mcp:io.github.github/github-mcp-server", "connector", "GitHub",
                    "GitHub", "Issues, pull requests, code search and Actions on your repositories.",
                    repo: "https://github.com/github/github-mcp-server", stars: 23_412,
                    updated: "2026-09-28T10:00:00Z", version: "0.19.1", auth: "api_key",
                    secrets: [StoreSecretField(name: "GITHUB_PERSONAL_ACCESS_TOKEN",
                                               label: "Personal access token",
                                               description: "A fine-grained token with repo access.",
                                               required: true)]),
        fixtureItem("mcp:com.notion/mcp", "connector", "Notion",
                    "Notion", "Read and write pages and databases in your workspace.",
                    repo: "https://github.com/makenotion/notion-mcp-server", stars: 3_310,
                    updated: "2026-08-30T10:00:00Z", version: "1.9.0", auth: "oauth",
                    installable: false,
                    whyNot: "Needs an OAuth sign-in in your browser, which the deck cannot run yet."),
        fixtureItem("skill:anthropics/skills:pdf", "skill", "PDF",
                    "Anthropic", "Extract text and tables, fill forms, merge and split PDF files.",
                    repo: "https://github.com/anthropics/skills", stars: 31_870,
                    updated: "2026-09-15T10:00:00Z", version: "9f2a15f", installedOn: ["chief", "hemingway"]),
        fixtureItem("skill:anthropics/skills:xlsx", "skill", "Excel",
                    "Anthropic", "Create and edit spreadsheets with formulas and charts.",
                    repo: "https://github.com/anthropics/skills", stars: 31_870,
                    updated: "2026-09-15T10:00:00Z", version: "9f2a15f"),
        fixtureItem("skill:anthropics/skills:webapp-testing", "skill", "Web app testing",
                    "Anthropic", "Drive a local web app with Playwright and capture what it does.",
                    repo: "https://github.com/anthropics/skills", stars: 31_870,
                    updated: "2026-09-15T10:00:00Z", version: "9f2a15f"),
        fixtureItem("mcp:io.github.someone/postgres-tools", "connector", "postgres-tools",
                    "someone", "Run SQL against any Postgres database you point it at.",
                    repo: "https://github.com/someone/postgres-tools", stars: 42,
                    updated: "2025-11-02T10:00:00Z", version: "0.0.3", trust: "unverified"),
    ]

    private static func fixtureItem(
        _ id: String, _ kind: String, _ title: String, _ publisher: String, _ description: String,
        repo: String?, stars: Int?, updated: String?, version: String?,
        trust: String = "official", auth: String = "none", secrets: [StoreSecretField] = [],
        installable: Bool = true, whyNot: String? = nil, installedOn: [String] = []
    ) -> StoreItem {
        StoreItem(id: id, kind: kind, name: title.lowercased(), title: title,
                  description: description, publisher: publisher,
                  source: kind == "skill" ? "anthropic-skills" : "mcp-registry",
                  repo: repo, stars: stars, updatedAt: updated, trust: trust,
                  trustNote: trust == "official" ? "Published by \(publisher)" : "Unknown publisher",
                  version: version, auth: auth, secrets: secrets, installable: installable,
                  whyNot: whyNot, installedOn: installedOn, readme: nil)
    }

    public func storeCatalog(kind: String?, query: String?, trust: StoreTrustScope,
                             limit: Int, offset: Int) async throws -> StoreCatalogPage {
        var items = Self.storeItems.filter { kind == nil || $0.kind == kind }
        if trust == .trusted { items = items.filter { $0.trust != "unverified" } }
        if let query, !query.isEmpty {
            items = items.filter {
                $0.title.localizedCaseInsensitiveContains(query)
                    || $0.description.localizedCaseInsensitiveContains(query)
            }
        }
        return StoreCatalogPage(items: Array(items.dropFirst(offset).prefix(limit)),
                                total: items.count, refreshedAt: "2026-09-30T06:00:00Z")
    }

    public func storeItem(id: String) async throws -> StoreItem {
        guard let item = Self.storeItems.first(where: { $0.id == id }) else {
            throw DeckError.storeRefused(.unknownItem, detail: id)
        }
        return item
    }

    public func storeInstall(_ request: StoreInstallRequest) async throws -> StoreInstallResult {
        let item = try await storeItem(id: request.id)
        if item.trust == "unverified" && !request.acceptUnverified {
            throw DeckError.storeRefused(.unverified, detail: item.title)
        }
        return try await storeUpdate(id: request.id, desks: request.desks)
    }

    public func storeUpdate(id: String, desks: StoreDesks) async throws -> StoreInstallResult {
        let item = try await storeItem(id: id)
        let names = fixtureDeskNames(desks)
        return StoreInstallResult(
            item: item,
            installed: names.map {
                StoreInstalled(id: item.id, kind: item.kind, name: item.name, title: item.title,
                               desk: $0, version: item.version, commit: nil,
                               installedAt: "2026-09-30T09:00:00Z", trust: item.trust,
                               updateAvailable: false)
            },
            reload: names.map { fixtureReload($0) })
    }

    public func storeUninstall(id: String, desks: StoreDesks) async throws -> StoreUninstallResult {
        let names = fixtureDeskNames(desks)
        return StoreUninstallResult(removed: names.map { StoreRemoved(desk: $0, id: id) },
                                    reload: names.map { fixtureReload($0) })
    }

    public func storeInstalled(desk: String?) async throws -> StoreInstalledPage {
        var byDesk: [String: [StoreInstalled]] = [:]
        for item in Self.storeItems {
            for name in item.installedOn where desk == nil || desk == name {
                byDesk[name, default: []].append(StoreInstalled(
                    id: item.id, kind: item.kind, name: item.name, title: item.title, desk: name,
                    version: item.version, commit: item.kind == "skill" ? "9f2a15f5b1bf0c3e" : nil,
                    installedAt: "2026-09-20T09:00:00Z", trust: item.trust,
                    updateAvailable: item.kind == "connector"))
            }
        }
        return StoreInstalledPage(desks: byDesk.keys.sorted().map {
            StoreDeskInstalls(desk: $0, items: byDesk[$0] ?? [])
        })
    }

    public func storeRefresh() async throws -> Bool { true }

    private func fixtureDeskNames(_ desks: StoreDesks) -> [String] {
        switch desks {
        case .all: return agents.map(\.name)
        case .desks(let names): return names
        }
    }

    private func fixtureReload(_ desk: String) -> StoreReload {
        desk == "chief"
            ? StoreReload(desk: desk, action: "after_turn", detail: "reloads after its current turn")
            : StoreReload(desk: desk, action: "next_wake", detail: "picks it up on its next wake")
    }
}

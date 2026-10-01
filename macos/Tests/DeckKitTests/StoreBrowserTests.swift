import XCTest
@testable import DeckKit

/// **The store's decisions, without a screen.** Which badge is honest, what an
/// install needs before it may go, where a typed key lives and for how long,
/// and how stars and ages read.
final class StoreBrowserTests: XCTestCase {

    private func item(
        id: String = "mcp:com.example/thing", kind: String = "connector",
        title: String = "Thing", publisher: String = "Example",
        description: String = "Does a thing.", trust: String = "official",
        auth: String = "none", secrets: [StoreSecretField] = [],
        installable: Bool = true, whyNot: String? = nil
    ) -> StoreItem {
        StoreItem(id: id, kind: kind, name: title.lowercased(), title: title,
                  description: description, publisher: publisher, source: "mcp-registry",
                  repo: nil, stars: nil, updatedAt: nil, trust: trust, trustNote: "",
                  version: nil, auth: auth, secrets: secrets, installable: installable,
                  whyNot: whyNot, installedOn: [], readme: nil)
    }

    private let apiKey = StoreSecretField(name: "API_KEY", label: "API key",
                                          description: "", required: true)

    // MARK: badges

    func testOnlyOfficialSaysOfficialAndOnlyVerifiedSaysVerified() {
        XCTAssertEqual(StoreBrowser.badge(forTrust: "official"), "Official")
        XCTAssertEqual(StoreBrowser.badge(forTrust: "verified"), "Verified")
        XCTAssertNil(StoreBrowser.badge(forTrust: "unverified"),
                     "an unverified item wears no badge at all")
        XCTAssertNil(StoreBrowser.badge(forTrust: ""))
        XCTAssertNil(StoreBrowser.badge(forTrust: "Official"), "no guessing at near-misses")
        XCTAssertNil(StoreBrowser.badge(forTrust: "trusted"))
    }

    // MARK: tabs, search, other sources

    func testTrustedModeFiltersLocallyAndNeverAsksTheServerWithAQuery() {
        var browser = StoreBrowser()
        browser.searchText = "LEARN"
        let items = [item(id: "a", title: "Microsoft Learn"),
                     item(id: "b", title: "GitHub", description: "Issues and PRs"),
                     item(id: "c", kind: "skill", title: "Learn skill")]

        XCTAssertEqual(browser.visible(items).map(\.id), ["a"], "right tab, matching text")
        XCTAssertEqual(browser.catalogRequest?.trust, .trusted)
        XCTAssertNil(browser.catalogRequest?.query)
        XCTAssertEqual(browser.catalogRequest?.kind, "connector")

        browser.tab = .skills
        XCTAssertEqual(browser.visible(items).map(\.id), ["c"])
        XCTAssertNil(StoreBrowser(tab: .installed).catalogRequest, "Installed has its own route")
    }

    func testOtherSourcesIsOffByDefaultAndHidesUnverifiedUntilOn() {
        var browser = StoreBrowser()
        let items = [item(id: "a"), item(id: "u", trust: "unverified")]

        XCTAssertFalse(browser.showsUnverified)
        XCTAssertEqual(browser.visible(items).map(\.id), ["a"])

        browser.showsUnverified = true
        browser.searchText = "postgres"
        XCTAssertEqual(browser.catalogRequest?.trust, .all)
        XCTAssertEqual(browser.catalogRequest?.query, "postgres",
                       "unverified search is the server's: nothing of it is cached here")
        XCTAssertEqual(browser.visible(items).map(\.id), ["a", "u"],
                       "the server already searched; the Mac does not filter it again")
    }

    // MARK: install

    func testAnUnverifiedInstallNeedsAnExplicitConfirm() {
        var draft = StoreInstallDraft(item: item(trust: "unverified"))

        XCTAssertTrue(draft.needsUnverifiedConfirm)
        XCTAssertFalse(draft.canInstall)
        XCTAssertNil(draft.takeRequest(), "no request leaves before he confirms")

        draft.confirmedUnverified = true
        XCTAssertTrue(draft.canInstall)
        XCTAssertEqual(draft.takeRequest()?.acceptUnverified, true)
    }

    func testATrustedInstallNeverClaimsToAcceptUnverified() {
        var official = StoreInstallDraft(item: item(trust: "official"))
        var verified = StoreInstallDraft(item: item(trust: "verified"))

        XCTAssertFalse(official.needsUnverifiedConfirm)
        XCTAssertFalse(verified.needsUnverifiedConfirm)
        XCTAssertEqual(official.takeRequest()?.acceptUnverified, false)
        XCTAssertEqual(verified.takeRequest()?.acceptUnverified, false)
    }

    func testChosenDesksMustNameAtLeastOneDesk() {
        var draft = StoreInstallDraft(item: item())
        XCTAssertEqual(draft.target, .allDesks, "all desks is the default")

        draft.target = .chosen([])
        XCTAssertFalse(draft.canInstall)
        draft.target = .chosen(["hemingway", "atlas"])
        XCTAssertEqual(draft.takeRequest()?.desks, .desks(["atlas", "hemingway"]))
    }

    func testARequiredKeyMustBeTypedAndOAuthCannotInstall() {
        var keyed = StoreInstallDraft(item: item(auth: "api_key", secrets: [apiKey]))
        XCTAssertFalse(keyed.canInstall)
        XCTAssertEqual(keyed.problem, "Enter API key.")
        keyed.setSecret("   ", for: "API_KEY")
        XCTAssertFalse(keyed.canInstall, "blank is not a key")
        keyed.setSecret("sk-1", for: "API_KEY")
        XCTAssertTrue(keyed.canInstall)

        let oauth = StoreInstallDraft(item: item(
            auth: "oauth", installable: false, whyNot: "Needs an OAuth sign-in"))
        XCTAssertFalse(oauth.canInstall)
        XCTAssertEqual(oauth.problem, "Needs an OAuth sign-in")
        XCTAssertTrue(oauth.showsOAuthNote)
    }

    /// The key is typed on the Mac, sent once, and gone. Nothing that
    /// describes the draft or the request — a log line, `dump`, a crash
    /// report's `String(reflecting:)` — may ever carry it.
    func testSecretsAreClearedAfterTheRequestIsTakenAndNeverDescribed() {
        let secret = "sk-live-DO-NOT-LEAK-9d8f"
        var draft = StoreInstallDraft(item: item(auth: "api_key", secrets: [apiKey]))
        draft.setSecret(secret, for: "API_KEY")

        XCTAssertFalse(describe(draft).contains(secret), describe(draft))

        let request = try? XCTUnwrap(draft.takeRequest())
        XCTAssertEqual(request?.secrets, ["API_KEY": secret], "it does travel, once")
        XCTAssertEqual(draft.secretValue(for: "API_KEY"), "", "and is gone from the draft")
        XCTAssertTrue(draft.secretValues.isEmpty)
        if let request {
            XCTAssertFalse(describe(request).contains(secret), describe(request))
        }
    }

    private func describe(_ value: Any) -> String {
        var dumped = ""
        dump(value, to: &dumped)
        return "\(value)" + String(reflecting: value) + dumped
            + String(describing: Mirror(reflecting: value).children.map { "\($0.value)" })
    }

    // MARK: what the card says

    func testStarsReadTheWayAGitHubPageReadsThem() {
        XCTAssertNil(StoreBrowser.starsText(nil))
        XCTAssertEqual(StoreBrowser.starsText(0), "0")
        XCTAssertEqual(StoreBrowser.starsText(999), "999")
        XCTAssertEqual(StoreBrowser.starsText(1_000), "1k")
        XCTAssertEqual(StoreBrowser.starsText(12_345), "12.3k")
        XCTAssertEqual(StoreBrowser.starsText(999_949), "999.9k")
        XCTAssertEqual(StoreBrowser.starsText(1_250_000), "1.3M")
    }

    func testTheUpdateDateIsRelative() throws {
        let now = try XCTUnwrap(StoreItem.parseDate("2026-09-30T12:00:00Z"))
        func text(_ iso: String?) -> String? { StoreBrowser.updatedText(iso, now: now) }

        XCTAssertNil(text(nil))
        XCTAssertNil(text("not a date"))
        XCTAssertEqual(text("2026-09-30T08:00:00Z"), "updated today")
        XCTAssertEqual(text("2026-09-29T08:00:00Z"), "updated yesterday")
        XCTAssertEqual(text("2026-09-25T12:00:00.123Z"), "updated 5 days ago")
        XCTAssertEqual(text("2026-09-09T12:00:00Z"), "updated 3 weeks ago")
        XCTAssertEqual(text("2026-05-30T12:00:00Z"), "updated 4 months ago")
        XCTAssertEqual(text("2024-08-30T12:00:00Z"), "updated 2 years ago")
    }

    func testAReloadIsOneLinePerDeskWithTheDecksOwnSentence() {
        XCTAssertEqual(
            StoreBrowser.reloadLine(StoreReload(desk: "atlas", action: "after_turn",
                                                detail: "reloads after its current turn")),
            "atlas — reloads after its current turn")
        XCTAssertEqual(
            StoreBrowser.reloadLine(StoreReload(desk: "iris", action: "none", detail: "")),
            "iris — nothing to reload")
    }

    func testAnInstalledRowShowsVersionAndShortCommit() {
        func row(_ version: String?, _ commit: String?) -> StoreInstalled {
            StoreInstalled(id: "x", kind: "skill", name: "x", title: "X", desk: "atlas",
                           version: version, commit: commit, installedAt: "", trust: "official",
                           updateAvailable: false)
        }
        XCTAssertEqual(StoreBrowser.versionText(row("1.2.0", nil)), "1.2.0")
        XCTAssertEqual(StoreBrowser.versionText(row(nil, "9f2a15f5b1bf0c3e")), "9f2a15f")
        XCTAssertEqual(StoreBrowser.versionText(row("9f2a15f", "9f2a15f5b1bf0c3e")), "9f2a15f",
                       "a skill's version is its short sha; saying it twice is noise")
        XCTAssertEqual(StoreBrowser.versionText(row("1.2.0", "9f2a15f5b1bf0c3e")), "1.2.0 · 9f2a15f")
        XCTAssertEqual(StoreBrowser.versionText(row(nil, nil)), "")
    }
}

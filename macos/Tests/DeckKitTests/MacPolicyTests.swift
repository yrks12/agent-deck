import XCTest
@testable import DeckKit

/// **A-1 / A-2: the Mac decides what a desk may do, and a stranger's default
/// is safe.** Ask me, no folders, no grants, "Never touch" enforced on the
/// resolved path; Full access allows everything; Off and Paused refuse. Grants
/// are per desk: an hour ends at +3600, Always survives a reload, Deny holds
/// 24 h and then asks again.
final class MacPolicyTests: XCTestCase {
    var home: String!
    var paths: MacPaths!
    let now: Double = 1_788_222_702

    override func setUpWithError() throws {
        home = NSTemporaryDirectory() + "mac-policy-\(UUID().uuidString)"
        for d in ["/.ssh", "/w/proj", "/Library/Application Support/Google/Chrome/Default", "/outside"] {
            try FileManager.default.createDirectory(atPath: home + d, withIntermediateDirectories: true)
        }
        paths = MacPaths(home: home)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(atPath: home) }

    private func policy(_ mode: MacMode = .ask, folders: [String] = ["~/w"], exceptions: [String] = [],
                        grants: [String: MacGrant] = ["atlas": MacGrant(state: .always, until: nil)],
                        seatbelt: Bool = true) -> MacPolicy {
        MacPolicy(config: MacPolicyConfig(mode: mode, folders: folders, exceptions: exceptions, grants: grants),
                  paths: paths, seatbeltAvailable: seatbelt, tempDirs: ["/private/tmp"])
    }

    private func job(_ kind: MacJobKind, _ path: String? = nil, desk: String = "atlas", cwd: String? = nil) -> MacJobRequest {
        MacJobRequest(id: "mj_1", desk: desk, kind: kind, path: path, cwd: cwd, summary: "\(kind) \(path ?? "")")
    }

    private func reason(_ d: MacDecision) -> String? {
        if case let .refuse(reason, _) = d { return reason }
        return nil
    }

    private func isRun(_ d: MacDecision) -> Bool {
        if case .run = d { return true }
        return false
    }

    // MARK: A-1

    func testDefaultsAreAskNoFoldersNoGrants() {
        let d = MacPolicyConfig.defaults
        XCTAssertEqual(d.mode, .ask)
        XCTAssertTrue(d.folders.isEmpty)
        XCTAssertTrue(d.grants.isEmpty)
        XCTAssertTrue(d.openEnabled)
        XCTAssertFalse(d.screenshotEnabled)
    }

    func testNeverTouchRefusesSshChromeAndDotEnv() {
        let p = policy(folders: ["~"])
        XCTAssertEqual(reason(p.decide(job(.read, "~/.ssh/id_ed25519"), now: now)), "blocked_path")
        XCTAssertEqual(reason(p.decide(job(.read, "~/Library/Application Support/Google/Chrome/Default/Cookies"), now: now)), "blocked_path")
        XCTAssertEqual(reason(p.decide(job(.read, "~/w/proj/.env"), now: now)), "blocked_path")
        XCTAssertEqual(reason(p.decide(job(.run, cwd: "~/.ssh"), now: now)), "blocked_path")
    }

    func testSymlinkIntoSshIsRefused() throws {
        try FileManager.default.createSymbolicLink(atPath: home + "/w/proj/keys", withDestinationPath: home + "/.ssh")
        XCTAssertEqual(reason(policy().decide(job(.read, "~/w/proj/keys/id_ed25519"), now: now)), "blocked_path")
        XCTAssertEqual(reason(policy().decide(job(.write, "~/w/proj/keys/authorized_keys"), now: now)), "blocked_path")
    }

    func testAnExceptionWins() {
        let p = policy(exceptions: ["~/w/proj/.env"])
        XCTAssertTrue(isRun(p.decide(job(.read, "~/w/proj/.env"), now: now)))
    }

    func testThePolicyDirCannotBeExceptedInAskMode() {
        let p = policy(folders: ["~"], exceptions: ["~/Library/Application Support/Agent Deck/**"])
        XCTAssertEqual(reason(p.decide(job(.write, "~/Library/Application Support/Agent Deck/mac-policy.json"), now: now)),
                       "blocked_path")
        XCTAssertEqual(reason(p.decide(job(.write, "~/Library/Preferences/\(DeckIdentity.bundleID).plist"), now: now)),
                       "blocked_path")
    }

    func testAskModeRefusesFileOpsOutsideFolders() {
        let p = policy()
        XCTAssertEqual(reason(p.decide(job(.write, "~/outside/x.txt"), now: now)), "out_of_scope")
        XCTAssertEqual(reason(p.decide(job(.read, "~/outside/x.txt"), now: now)), "out_of_scope")
        XCTAssertEqual(reason(p.decide(job(.run, cwd: "~/outside"), now: now)), "out_of_scope")
        XCTAssertTrue(isRun(p.decide(job(.write, "~/w/proj/new.txt"), now: now)))
    }

    func testFullAllowsAllUnconfined() {
        let p = policy(.full, folders: [], grants: [:])
        XCTAssertEqual(p.decide(job(.read, "~/.ssh/id_ed25519"), now: now),
                       .run(profile: nil, path: paths.home + "/.ssh/id_ed25519", cwd: nil))
        XCTAssertEqual(p.decide(job(.run, desk: "stranger"), now: now), .run(profile: nil, path: nil, cwd: nil))
    }

    func testOffAndPausedRefuse() {
        XCTAssertEqual(reason(policy(.off).decide(job(.run), now: now)), "mac_offline")
        XCTAssertEqual(reason(policy(.paused).decide(job(.run), now: now)), "mac_paused")
    }

    func testCapabilityToggles() {
        var p = policy()
        XCTAssertTrue(isRun(p.decide(job(.open), now: now)))
        XCTAssertEqual(reason(p.decide(job(.screenshot), now: now)), "capability_off")
        p.config.openEnabled = false
        XCTAssertEqual(reason(p.decide(job(.open), now: now)), "capability_off")
    }

    func testRelativePathIsBadPath() {
        XCTAssertEqual(reason(policy().decide(job(.read, "w/proj"), now: now)), "bad_path")
    }

    func testGrantedAskRunIsConfinedAndDefaultsCwdToTheFirstFolder() {
        guard case let .run(profile, _, cwd) = policy().decide(job(.run), now: now) else { return XCTFail() }
        XCTAssertNotNil(profile)
        XCTAssertEqual(cwd, paths.home + "/w")
    }

    // MARK: A-2

    func testUngrantedDeskIsAskedWithTheFirstRequest() {
        let d = policy(grants: [:]).decide(job(.read, "~/w/proj/README.md"), now: now)
        guard case let .ask(req) = d else { return XCTFail("\(d)") }
        XCTAssertEqual(req.desk, "atlas")
        XCTAssertEqual(req.folders, [paths.home + "/w"])
        XCTAssertFalse(req.unconfined)
    }

    func testHourExpiresAtPlus3600() {
        var p = policy(grants: [:])
        p.apply(.hour, desk: "atlas", now: now)
        XCTAssertTrue(isRun(p.decide(job(.list, "~/w"), now: now + 3599)))
        guard case .ask = p.decide(job(.list, "~/w"), now: now + 3600) else { return XCTFail() }
    }

    func testGrantsArePerDesk() {
        var p = policy(grants: [:])
        p.apply(.always, desk: "atlas", now: now)
        XCTAssertTrue(isRun(p.decide(job(.list, "~/w"), now: now)))
        guard case .ask = p.decide(job(.list, "~/w", desk: "nova"), now: now) else { return XCTFail() }
    }

    func testDenyHolds24HoursThenAsksAgain() {
        var p = policy(grants: [:])
        p.apply(.deny, desk: "atlas", now: now)
        XCTAssertEqual(reason(p.decide(job(.list, "~/w"), now: now + 86_399)), "denied")
        guard case .ask = p.decide(job(.list, "~/w"), now: now + 86_400) else { return XCTFail() }
    }

    func testRevokeRemovesTheGrant() {
        var p = policy()
        p.apply(.revoke, desk: "atlas", now: now)
        XCTAssertNil(p.grant(for: "atlas", at: now))
    }

    func testAlwaysSurvivesAReload() throws {
        let store = MacPolicyStore(url: URL(fileURLWithPath: home + "/support/mac-policy.json"))
        var p = policy(grants: [:])
        p.apply(.always, desk: "atlas", now: now)
        try store.save(p.config)
        let reloaded = MacPolicy(config: store.load(), paths: paths, seatbeltAvailable: true, tempDirs: [])
        XCTAssertEqual(reloaded.grant(for: "atlas", at: now + 10 * 86_400)?.state, .always)
    }

    func testAMissingOrCorruptFileIsTheSafeDefault() throws {
        let url = URL(fileURLWithPath: home + "/support/mac-policy.json")
        XCTAssertEqual(MacPolicyStore(url: url).load(), .defaults)
        try FileManager.default.createDirectory(atPath: home + "/support", withIntermediateDirectories: true)
        try Data("{\"mode\":\"full\"".utf8).write(to: url)
        XCTAssertEqual(MacPolicyStore(url: url).load().mode, .ask)
    }

    func testTheFileUsesThePlansKeys() throws {
        let data = try JSONEncoder().encode(MacPolicyConfig.defaults)
        let obj = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        XCTAssertEqual(Set(obj.keys), ["mode", "folders", "never_touch", "exceptions", "open_enabled",
                                       "screenshot_enabled", "grants"])
    }

    // MARK: fallback (Seatbelt unavailable)

    func testWithoutSeatbeltAnAskRunNeedsAnUnconfinedGrant() {
        var p = policy(seatbelt: false)
        guard case let .ask(req) = p.decide(job(.run), now: now) else { return XCTFail() }
        XCTAssertTrue(req.unconfined)
        XCTAssertTrue(isRun(p.decide(job(.read, "~/w/proj/x"), now: now)), "file tools stay scoped, not blocked")
        p.apply(.always, desk: "atlas", now: now, unconfined: true)
        XCTAssertEqual(p.decide(job(.run), now: now), .run(profile: nil, path: nil, cwd: paths.home + "/w"))
    }
}

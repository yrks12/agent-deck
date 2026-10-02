import XCTest
@testable import DeckKit

/// **A path is judged by where it really lands, not by how it was spelled.**
///
/// `~/w/../.ssh/id`, a symlink into `~/.ssh`, or a dangling symlink a write
/// would follow — each must resolve to the real place before "Never touch" is
/// asked. And on this Mac `/var` is `/private/var`, so a temp HOME must match
/// its own patterns.
final class MacPathsTests: XCTestCase {
    var home: String!

    override func setUpWithError() throws {
        home = NSTemporaryDirectory() + "mac-paths-\(UUID().uuidString)"
        try FileManager.default.createDirectory(atPath: home + "/.ssh", withIntermediateDirectories: true)
        try FileManager.default.createDirectory(atPath: home + "/w", withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(atPath: home) }

    func testTildeExpandsAndRelativeIsRefused() {
        let p = MacPaths(home: home)
        XCTAssertEqual(p.expand("~"), p.home)
        XCTAssertEqual(p.expand("~/w"), p.home + "/w")
        XCTAssertNil(p.expand("w/x"))
        XCTAssertNil(p.expand("~root/x"))
        XCTAssertNil(p.expand(""))
        XCTAssertNil(p.expand("/tmp/a\nb"))
    }

    func testHomeIsResolvedSoVarIsPrivateVar() {
        let p = MacPaths(home: home)
        XCTAssertTrue(p.home.hasPrefix("/private/"), p.home)
    }

    func testDotDotCannotWalkIntoSsh() {
        let p = MacPaths(home: home)
        XCTAssertEqual(p.resolve("~/w/../.ssh/id_ed25519"), p.home + "/.ssh/id_ed25519")
    }

    func testSymlinkIntoSshResolvesThere() throws {
        try FileManager.default.createSymbolicLink(atPath: home + "/w/keys", withDestinationPath: home + "/.ssh")
        let p = MacPaths(home: home)
        XCTAssertEqual(p.resolve("~/w/keys/new_key"), p.home + "/.ssh/new_key")
    }

    func testDanglingSymlinkIsFollowed() throws {
        try FileManager.default.createSymbolicLink(atPath: home + "/w/x", withDestinationPath: home + "/.ssh/not_yet")
        let p = MacPaths(home: home)
        XCTAssertEqual(p.resolve("~/w/x"), p.home + "/.ssh/not_yet")
    }

    func testGlobs() {
        let p = MacPaths(home: home)
        XCTAssertTrue(p.matches("~/.ssh/**", p.home + "/.ssh"))
        XCTAssertTrue(p.matches("~/.ssh/**", p.home + "/.ssh/a/b"))
        XCTAssertFalse(p.matches("~/.ssh/**", p.home + "/.sshx"))
        XCTAssertTrue(p.matches("**/.env", "/a/b/.env"))
        XCTAssertTrue(p.matches("**/.env", "/.env"))
        XCTAssertFalse(p.matches("**/.env", "/a/.envrc"))
        XCTAssertTrue(p.matches("**/.env.*", "/a/.env.local"))
        XCTAssertTrue(p.matches("**/*.pem", "/a/b/c.pem"))
        XCTAssertTrue(p.matches("~/Library/Application Support/{Google/Chrome,Arc}/**",
                                p.home + "/Library/Application Support/Google/Chrome/Default/Cookies"))
        XCTAssertFalse(p.matches("~/.netrc", p.home + "/.netrcx"))
    }

    func testIsInside() {
        XCTAssertTrue(MacPaths.isInside("/a/b", folder: "/a"))
        XCTAssertTrue(MacPaths.isInside("/a", folder: "/a"))
        XCTAssertFalse(MacPaths.isInside("/ab", folder: "/a"))
    }
}

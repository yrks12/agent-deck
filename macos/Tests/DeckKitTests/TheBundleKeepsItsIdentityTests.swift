import XCTest

/// **The keychain asks for his password after every single rebuild.**
///
/// He photographed it: *"Agent Deck wants to use your confidential information
/// stored in '<bundle id>' in your keychain."* — and the very next
/// screenshot was an app with an empty sidebar, because the roster call has no
/// token until that dialog is answered.
///
/// MEASURED, on the bundle that produced both:
///
///     codesign -dv "dist/Agent Deck.app"
///       Identifier=DeckApp
///       CodeDirectory ... flags=0x20002(adhoc,linker-signed)
///       Signature=adhoc
///       TeamIdentifier=not set
///
/// A keychain ACL is bound to the **code identity** of the process that made
/// the item, and for an ad-hoc, linker-signed binary that identity is its
/// cdhash — which changes on every compile. So "Always Allow" is granted to a
/// binary that will not exist after the next build, and macOS correctly asks
/// again. It is not a bug in the app; it is the absence of a stable signature.
///
/// Signing with one self-signed identity fixes it because the ACL then names
/// that identity rather than a hash of today's bytes.
///
/// **This test parses the script rather than inspecting a built bundle.** A
/// test that ran `codesign` on `dist/` would pass or fail on whatever happened
/// to be lying there, and would be vacuous on a clean checkout. The property
/// worth protecting is that *the thing which builds the bundle signs it*, so
/// that is what is read. Same shape, and the same reasoning, as
/// `ScreenGateTests` sweeping `Scripts/*.sh` for its declaration.
final class TheBundleKeepsItsIdentityTests: XCTestCase {

    private var script: String {
        get throws {
            let url = URL(fileURLWithPath: #filePath)
                .deletingLastPathComponent()   // Tests/DeckKitTests
                .deletingLastPathComponent()   // Tests
                .deletingLastPathComponent()   // repo root
                .appendingPathComponent("Scripts/make-app-bundle.sh")
            return try String(contentsOf: url, encoding: .utf8)
        }
    }

    /// Without this the assertions below are vacuously true.
    func testTheBundleScriptIsWhereThisCheckLooksForIt() throws {
        XCTAssertFalse(
            try script.isEmpty,
            "Scripts/make-app-bundle.sh is missing or empty — this check would "
            + "pass over anything")
    }

    /// THE test. Asserts the presence of the good signal: the script signs.
    func testTheScriptSignsTheBundleItBuilds() throws {
        let body = try script
        XCTAssertTrue(
            body.contains("codesign"),
            "make-app-bundle.sh never signs the bundle, so it ships ad-hoc "
            + "linker-signed and its code identity is a hash of today's bytes. "
            + "Every rebuild is a different app to the keychain, so he is asked "
            + "for his password again and the roster has no token until he "
            + "answers — which is an app that opens with an empty sidebar.")
    }

    /// And it must sign with a **named** identity. `codesign -s -` is ad-hoc:
    /// it would satisfy the test above and leave the identity exactly as
    /// unstable as it is now, which is the shape of a check that ran and
    /// proved nothing.
    func testItSignsWithAStableIdentityRatherThanAdHoc() throws {
        let body = try script
        let signLines = body.split(separator: "\n").filter { $0.contains("codesign") }
        XCTAssertFalse(signLines.isEmpty, "covered by the assertion above")

        let identity = "DECK_SIGN_IDENTITY"
        XCTAssertTrue(
            body.contains(identity),
            "the script signs, but not with a named identity it can name twice: "
            + "an ad-hoc signature (`-s -`) is a fresh cdhash every build and "
            + "changes nothing about the keychain prompt. Sign with \(identity).")
        XCTAssertFalse(
            signLines.contains { $0.contains("-s -") && !$0.contains(identity) },
            "a bare `codesign -s -` is ad-hoc signing under another name: "
            + "\(signLines.joined(separator: " | "))")
    }

    /// The reason travels with the line. A bare `codesign` reads like a stray
    /// step and gets removed in a tidy-up, and the symptom it prevents shows up
    /// days later as "the app opened empty", which nobody connects back.
    func testTheReasonIsWrittenDownBesideTheSigning() throws {
        let body = try script
        guard let where_ = body.range(of: "codesign") else {
            return XCTFail("covered by the assertion above")
        }
        let context = String(body[body.startIndex..<where_.lowerBound].suffix(1200)).lowercased()
        XCTAssertTrue(
            context.contains("keychain"),
            "the script signs with no comment saying that this is what stops "
            + "the keychain asking for his password after every rebuild")
    }
}

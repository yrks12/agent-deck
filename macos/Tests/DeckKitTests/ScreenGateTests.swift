import XCTest

/// **No test takes the owner's screen unless he has said it is free.**
///
/// Found live, and reported in his own words: *"i need my screen now make
/// these tests when i say i dont need the screen"*. XCUITest launches the real
/// `Agent Deck.app`, brings it to the front and drives it with synthetic
/// clicks and keystrokes for the length of the run. While that happens the Mac
/// is not usable — whatever he was doing loses focus mid-sentence. He was
/// working when it happened.
///
/// So the screen is an owned resource, and the gate is the same shape the
/// Python suite already uses for its `live` and `ui` markers: **off by
/// default, on only when it is explicitly asked for.** `swift test` must never
/// take the screen. `xcodebuild ... test` without the switch must skip rather
/// than seize.
///
/// **Why this test parses source instead of running the UI tests.** The fault
/// is not that three particular classes grab the screen — it is that nothing
/// stops the *fourth* one from doing it. A test that ran the existing three
/// would pass forever while someone adds a new file next month and takes his
/// screen again. Reading every `XCTestCase` in `UITests/` is the only check
/// that still fails then, so it sweeps the class and not the instance.
///
/// It asserts the presence of the good signal — every UI test class *calls the
/// gate* — never the absence of a bad one. "No XCUIApplication was launched"
/// is also what an empty directory looks like, and an empty directory is not
/// the property worth protecting.
final class ScreenGateTests: XCTestCase {

    /// The switch. Absent or anything other than `"1"` means: his screen.
    static let switchName = "YOS_SCREEN_IS_FREE"

    /// The call every screen-taking test must make before it launches an app.
    static let gateCall = "requireTheScreenIsFree"

    private var uiTestsDirectory: URL {
        URL(fileURLWithPath: #filePath)          // .../Tests/DeckKitTests/ScreenGateTests.swift
            .deletingLastPathComponent()         // .../Tests/DeckKitTests
            .deletingLastPathComponent()         // .../Tests
            .deletingLastPathComponent()         // repo root
            .appendingPathComponent("UITests/DeckUITests")
    }

    private func uiTestSources() throws -> [(name: String, body: String)] {
        let files = try FileManager.default.contentsOfDirectory(
            at: uiTestsDirectory, includingPropertiesForKeys: nil)
        return try files
            .filter { $0.pathExtension == "swift" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
            .map { ($0.lastPathComponent, try String(contentsOf: $0, encoding: .utf8)) }
    }

    /// The directory has to be found, or every assertion below is vacuously
    /// true and this whole file becomes decoration.
    func testTheUITestsAreWhereThisCheckLooksForThem() throws {
        let sources = try uiTestSources()
        XCTAssertFalse(
            sources.isEmpty,
            "no UI test sources found at \(uiTestsDirectory.path) — this check "
            + "would pass while every UI test grabbed the screen")
    }

    /// THE test. Every class that drives the real app asks permission first.
    func testEveryTestThatTakesTheScreenAsksWhetherItMay() throws {
        var ungated: [String] = []
        for source in try uiTestSources() {
            guard source.body.contains("XCUIApplication") else { continue }
            guard source.name != "ScreenGate.swift" else { continue }
            if !source.body.contains(Self.gateCall) {
                ungated.append(source.name)
            }
        }
        XCTAssertEqual(
            ungated, [],
            "these tests launch the real app without calling "
            + "\(Self.gateCall)(), so they take his screen whether or not he "
            + "said it was free: \(ungated.joined(separator: ", "))")
    }

    /// The gate itself must read the switch. A `requireTheScreenIsFree()` that
    /// checked nothing would satisfy the test above and still seize the Mac —
    /// which is exactly the shape of "the check ran and proved nothing".
    func testTheGateIsDecidedByTheSwitchAndSkipsRatherThanFails() throws {
        let gate = uiTestsDirectory.appendingPathComponent("ScreenGate.swift")
        let body = try String(contentsOf: gate, encoding: .utf8)

        XCTAssertTrue(
            body.contains(Self.switchName),
            "the gate does not read \(Self.switchName), so nothing he can set "
            + "turns these on or off")
        XCTAssertTrue(
            body.contains("XCTSkip"),
            "the gate must SKIP when the screen is his. A failure would make a "
            + "clean run look broken and train everyone to run it anyway")
    }

    /// **And a script that launches the app is exactly the same problem.**
    ///
    /// The 100% CPU defect turned out to reproduce hands-off — the app pegs a
    /// core on its own within 15 seconds — so the reproduction moved out of
    /// XCUITest and into `Scripts/idle-cpu.sh`, which launches the real bundle
    /// against the real deck and watches it for a minute. Leaving that ungated
    /// would put the screen back on the table by a route this file was not
    /// looking at.
    ///
    /// Sweeps every script rather than naming that one, and does it by a
    /// **declaration** rather than by guessing from the text: every script
    /// carries `# SCREEN: takes` or `# SCREEN: none`, and one that takes the
    /// screen has to read the switch. Guessing was tried first and immediately
    /// mis-read `make-app-bundle.sh`, which writes a path into a bundle it
    /// never runs — a check that cries wolf gets deleted.
    func testEveryScriptDeclaresWhetherItTakesTheScreenAndGatesItselfIfItDoes() throws {
        let scripts = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Scripts")
        let files = try FileManager.default.contentsOfDirectory(
            at: scripts, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "sh" }
            .sorted { $0.lastPathComponent < $1.lastPathComponent }
        XCTAssertFalse(files.isEmpty, "no scripts found at \(scripts.path) — this "
                       + "check would pass over anything")

        var undeclared: [String] = []
        var ungated: [String] = []
        var takers = 0
        for file in files {
            let name = file.lastPathComponent
            let body = try String(contentsOf: file, encoding: .utf8)
            if body.contains("# SCREEN: takes") {
                takers += 1
                if !body.contains(Self.switchName) { ungated.append(name) }
            } else if !body.contains("# SCREEN: none") {
                undeclared.append(name)
            }
        }

        XCTAssertEqual(
            undeclared, [],
            "these scripts say nothing about whether they take the owner's "
            + "screen, so nobody reading them knows and nothing checks: "
            + "\(undeclared.joined(separator: ", ")). Add `# SCREEN: takes` or "
            + "`# SCREEN: none` near the top.")
        XCTAssertEqual(
            ungated, [],
            "these scripts declare that they take the screen and then never "
            + "read \(Self.switchName), so they take it whether or not he said "
            + "it was free: \(ungated.joined(separator: ", "))")
        XCTAssertGreaterThan(
            takers, 0,
            "no script declares that it takes the screen, so the gate above "
            + "asserted nothing — Scripts/idle-cpu.sh, the only reproduction "
            + "this defect has, has been renamed or removed")
    }

    /// Default-deny, stated as a test. A gate that defaulted to "free" would
    /// pass every check above and take his screen on a fresh clone.
    func testTheScreenIsHisUnlessTheSwitchSaysOtherwise() throws {
        let gate = uiTestsDirectory.appendingPathComponent("ScreenGate.swift")
        let body = try String(contentsOf: gate, encoding: .utf8)

        XCTAssertTrue(
            body.contains("== \"1\""),
            "the gate must open only on an explicit value. Anything that opens "
            + "on 'the variable is not set to no' opens by default")
    }
}

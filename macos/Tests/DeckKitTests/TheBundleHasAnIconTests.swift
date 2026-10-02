import XCTest

/// **The Dock has always shown the generic icon.** `make-app-bundle.sh` wraps
/// the SwiftPM executable in a `.app` for exactly this reason -- so it gets a
/// stable bundle id and, the docstring says, "a Dock icon" -- but nothing in
/// the pipeline has ever produced one.
///
/// The icon step (`Scripts/embed-icon.sh`) is run for real here, against a
/// throwaway fake bundle -- unlike `TheBundleKeepsItsIdentityTests` next to it,
/// which only reads `make-app-bundle.sh` as text. A signing flag is true or
/// false by inspection; an `.icns` is only real once `iconutil` has accepted
/// the artwork and produced every required size, and the one way to know that
/// is to build it and hand the result back to `iconutil` to unpack. A text
/// match for `"iconutil"` would pass on a script that called it with the wrong
/// arguments and shipped a corrupt icon.
///
/// It targets `embed-icon.sh` rather than the whole of `make-app-bundle.sh` on
/// purpose: the parent script's icon step runs a `swift build` first, and
/// invoking that from *inside* an already-building `swift test` process
/// self-deadlocks on SwiftPM's own package lock (measured: a nested
/// `swift build` for the same package, launched from a running `swift test`,
/// hangs indefinitely rather than erroring). `embed-icon.sh` needs nothing
/// Swift-related, so it carries none of that risk and runs in well under a
/// second.
final class TheBundleHasAnIconTests: XCTestCase {

    /// The `macos/` package root -- same three hops `TheBundleKeepsItsIdentityTests`
    /// uses to find `Scripts/make-app-bundle.sh` from this same test directory.
    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // Tests/DeckKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // macos
    }

    private var bundleScript: String {
        get throws {
            try String(
                contentsOf: packageRoot.appendingPathComponent("Scripts/make-app-bundle.sh"),
                encoding: .utf8)
        }
    }

    private func run(_ executable: String, _ arguments: [String]) throws -> (Int32, String) {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        try process.run()
        process.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
    }

    // MARK: - the icon step, run for real against a throwaway bundle

    func testEmbedIconProducesAValidIcnsIconutilCanRoundTrip() throws {
        let script = packageRoot.appendingPathComponent("Scripts/embed-icon.sh").path
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("AgentDeckIconTest-\(UUID().uuidString)")
        let fakeApp = scratch.appendingPathComponent("Fake.app")
        try FileManager.default.createDirectory(
            at: fakeApp.appendingPathComponent("Contents/Resources"),
            withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }

        let (status, output) = try run("/bin/bash", [script, fakeApp.path])
        XCTAssertEqual(status, 0, "embed-icon.sh failed:\n\(output)")

        let icon = fakeApp.appendingPathComponent("Contents/Resources/AppIcon.icns")
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: icon.path),
            "embed-icon.sh ran but left no Contents/Resources/AppIcon.icns")

        // The good signal, not just "a file is there": `iconutil` itself will
        // unpack it back into an iconset, which only succeeds for a real `.icns`.
        let roundTrip = scratch.appendingPathComponent("roundtrip.iconset")
        let (rtStatus, rtOutput) = try run(
            "/usr/bin/iconutil", ["-c", "iconset", "-o", roundTrip.path, icon.path])
        XCTAssertEqual(rtStatus, 0, "iconutil could not unpack AppIcon.icns:\n\(rtOutput)")
        let entries = (try? FileManager.default.contentsOfDirectory(atPath: roundTrip.path)) ?? []
        XCTAssertFalse(entries.isEmpty, "iconutil produced an empty iconset from AppIcon.icns")
    }

    func testEmbedIconRefusesWithNoSourceArtworkRatherThanShippingSilently() throws {
        let script = packageRoot.appendingPathComponent("Scripts/embed-icon.sh").path
        let (status, _) = try run("/bin/bash", [script])
        XCTAssertNotEqual(status, 0, "embed-icon.sh with no bundle argument must fail loudly")
    }

    // MARK: - the master artwork this step depends on

    func testTheMasterIconArtworkIsCommitted() throws {
        let png = packageRoot.appendingPathComponent("Resources/AppIcon.png")
        XCTAssertTrue(
            FileManager.default.fileExists(atPath: png.path),
            "macos/Resources/AppIcon.png is missing -- embed-icon.sh has nothing to build from")
    }

    // MARK: - the whole-bundle pipeline wires the step in, in the right order

    func testMakeAppBundleDeclaresTheIconFileInInfoPlist() throws {
        let body = try bundleScript
        XCTAssertTrue(
            body.contains("<key>CFBundleIconFile</key>"),
            "Info.plist has no CFBundleIconFile, so Finder and the Dock have no "
            + "reason to look for AppIcon.icns at all")
        XCTAssertTrue(
            body.contains("<string>AppIcon</string>"),
            "CFBundleIconFile must name AppIcon (the .icns embed-icon.sh writes), "
            + "sans extension, exactly as Info.plist expects it")
    }

    /// Comments and blanks dropped first -- the script's own comment block
    /// above the real `codesign` call quotes a `codesign -dv` invocation for
    /// documentation, which would otherwise satisfy a bare substring search
    /// long before either real step runs.
    func testMakeAppBundleRunsTheIconStepBeforeSigning() throws {
        let codeLines = try bundleScript.split(separator: "\n")
            .map { String($0) }
            .filter { !$0.trimmingCharacters(in: .whitespaces).hasPrefix("#") }
        let code = codeLines.joined(separator: "\n")

        guard let iconRange = code.range(of: "embed-icon.sh") else {
            return XCTFail("make-app-bundle.sh never calls embed-icon.sh")
        }
        guard let signRange = code.range(of: "codesign") else {
            return XCTFail("covered by TheBundleKeepsItsIdentityTests")
        }
        XCTAssertTrue(
            iconRange.lowerBound < signRange.lowerBound,
            "the icon is embedded after signing, so the signature does not cover it")
    }
}

import Foundation

/// Whether this process may make sound or open the mic.
///
/// MEASURED 2026-09-30: a whole `swift test` run spoke through the owner's
/// speakers in the system voice while he was working. Test processes are
/// silenced unconditionally; a hardware test that really needs the speakers
/// opts in with `DECK_ALLOW_TEST_AUDIO=1`.
public enum AudioGate {
    public static var isSilenced: Bool {
        let env = ProcessInfo.processInfo.environment
        if env["DECK_ALLOW_TEST_AUDIO"] == "1" { return false }
        return env["XCTestConfigurationFilePath"] != nil
            || env["XCTestBundlePath"] != nil
            || NSClassFromString("XCTestCase") != nil
    }
}

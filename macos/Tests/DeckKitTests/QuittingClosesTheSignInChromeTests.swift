import XCTest
@testable import DeckKit

/// **Quitting the app mid-sign-in must not leave a signed-in Chrome behind.**
///
/// "Sign in on this Mac" opens Chrome in a throwaway profile that, once he has
/// signed in, holds a live session. If the app quits before Share or Cancel,
/// nothing else would ever close that window or delete that profile. So every
/// open sign-in window is registered, and app termination closes them all —
/// synchronously, because `willTerminate` does not wait for async work.
///
/// No Chrome here: the window wraps `/bin/sleep` and a temp directory.
final class QuittingClosesTheSignInChromeTests: XCTestCase {

    private func window() throws -> (ChromeSignInWindow, Process, URL) {
        let profile = try ChromeSignInBrowser.makeProfile(
            in: FileManager.default.temporaryDirectory
                .appendingPathComponent("signin-quit-test-\(UUID().uuidString)"))
        try Data("session".utf8).write(to: profile.appendingPathComponent("Cookies"))
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sleep")
        process.arguments = ["60"]
        try process.run()
        let window = ChromeSignInWindow(process: process, profile: profile)
        SignInWindows.shared.register(window)
        return (window, process, profile)
    }

    func testAppTerminationClosesTheSignInChromeAndDeletesItsProfile() throws {
        let center = NotificationCenter()
        let token = SignInWindows.closeAllOnTerminate(center: center)
        defer { center.removeObserver(token) }
        let (_, process, profile) = try window()
        defer { if process.isRunning { process.terminate() } }

        center.post(name: SignInWindows.willTerminate, object: nil)

        process.waitUntilExit()
        XCTAssertFalse(process.isRunning, "the sign-in Chrome outlived the app")
        XCTAssertFalse(FileManager.default.fileExists(atPath: profile.path),
                       "the signed-in profile was left on disk")
    }

    func testTheNotificationIsAppKitsWillTerminate() {
        XCTAssertEqual(SignInWindows.willTerminate.rawValue,
                       "NSApplicationWillTerminateNotification")
    }

    func testAWindowClosedNormallyIsNoLongerTracked() async throws {
        let (window, process, _) = try window()
        await window.close()
        process.waitUntilExit()
        XCTAssertFalse(SignInWindows.shared.isTracking(window))
    }
}

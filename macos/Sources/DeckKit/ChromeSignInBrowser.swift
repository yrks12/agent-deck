import Foundation

// Mac-only: Chrome and `Process` exist only there. Fenced so DeckKit still
// builds for the iPhone app, which links the same package.
#if os(macOS)

/// **Google Chrome, in a throwaway profile, with a loopback debugging port.**
///
/// Measured on the owner's Mac (Chrome 154, macOS 26): a fresh
/// `--user-data-dir` launched with `--remote-debugging-port=0` reports
/// `passkeyPlatformAuthenticator` and `hybridTransport` true — Touch ID with
/// his iCloud Keychain passkeys, or his iPhone by QR — and Chrome carries the
/// `web-browser.public-key-credential` entitlement that iCloud passkeys need.
/// Chrome refuses a debugging port on the *default* profile (136+), and his
/// own profile is his anyway, so it is always a new one.
///
/// The profile is created 0700 under the user's temporary directory and
/// deleted by `close()`. The cookies are read once, at his "Share", over the
/// browser-level CDP socket (`Storage.getCookies`), and never logged.
public struct ChromeSignInBrowser: SignInBrowser {
    public static let candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        NSHomeDirectory() + "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]

    /// How long Chrome gets to write its port file.
    public var startTimeout: TimeInterval = 20
    /// Added before the URL. For a live check that must not draw on his screen
    /// (`--window-position=-2400,-2400`); empty in the app.
    public var extraArguments: [String] = []

    public init() {}

    /// Chrome's argv. PURE.
    public static func arguments(profile: URL, url: URL) -> [String] {
        [
            "--user-data-dir=\(profile.path)",
            "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1",
            "--no-first-run",
            "--no-default-browser-check",
            "--new-window",
            url.absoluteString,
        ]
    }

    /// `DevToolsActivePort`: the port, then the browser target's path. PURE.
    public static func activePort(_ text: String) -> (port: Int, path: String)? {
        let lines = text.split(whereSeparator: \.isNewline).map(String.init)
        guard lines.count >= 2, let port = Int(lines[0]), (1...65535).contains(port),
              lines[1].hasPrefix("/devtools/browser/"),
              !lines[1].contains(where: { $0 == " " || $0 == "?" || $0 == "#" })
        else { return nil }
        return (port, lines[1])
    }

    /// A new 0700 directory under `root` for one sign-in.
    public static func makeProfile(in root: URL) throws -> URL {
        let fm = FileManager.default
        try fm.createDirectory(at: root, withIntermediateDirectories: true,
                               attributes: [.posixPermissions: 0o700])
        let profile = root.appendingPathComponent("agent-deck-signin-\(UUID().uuidString)")
        try fm.createDirectory(at: profile, withIntermediateDirectories: false,
                               attributes: [.posixPermissions: 0o700])
        return profile
    }

    /// The `cookies` array out of a `Storage.getCookies` reply. PURE.
    public static func cookies(fromReply data: Data) throws -> Data {
        guard let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let result = reply["result"] as? [String: Any],
              let rows = result["cookies"] as? [Any]
        else { throw MacSignInError.cdp }
        return try JSONSerialization.data(withJSONObject: rows)
    }

    public func open(_ url: URL) async throws -> SignInWindow {
        guard let chrome = Self.candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else { throw MacSignInError.chromeMissing }

        let profile = try Self.makeProfile(in: FileManager.default.temporaryDirectory
            .appendingPathComponent("agent-deck-signin", isDirectory: true))
        let process = Process()
        process.executableURL = URL(fileURLWithPath: chrome)
        var argv = Self.arguments(profile: profile, url: url)
        argv.insert(contentsOf: extraArguments, at: argv.count - 1)
        process.arguments = argv
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        let window = ChromeSignInWindow(process: process, profile: profile)
        SignInWindows.shared.register(window)
        do {
            try process.run()
        } catch {
            await window.close()
            throw MacSignInError.chromeDidNotStart
        }
        let portFile = profile.appendingPathComponent("DevToolsActivePort")
        let deadline = Date().addingTimeInterval(startTimeout)
        while Date() < deadline {
            if let text = try? String(contentsOf: portFile, encoding: .utf8),
               let found = Self.activePort(text) {
                window.endpoint = URL(string: "ws://127.0.0.1:\(found.port)\(found.path)")
                return window
            }
            if !process.isRunning { break }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        await window.close()
        throw MacSignInError.chromeDidNotStart
    }
}

/// One running Chrome and its profile.
final class ChromeSignInWindow: SignInWindow, @unchecked Sendable {
    private let process: Process
    private let profile: URL
    private let lock = NSLock()
    var endpoint: URL?

    init(process: Process, profile: URL) {
        self.process = process
        self.profile = profile
    }

    func cookieJSON() async throws -> Data {
        guard process.isRunning, let endpoint else { throw MacSignInError.chromeClosed }
        do {
            return try ChromeSignInBrowser.cookies(
                fromReply: try await call(endpoint, method: "Storage.getCookies"))
        } catch let error as MacSignInError {
            throw error
        } catch {
            throw process.isRunning ? MacSignInError.cdp : MacSignInError.chromeClosed
        }
    }

    func close() async {
        if process.isRunning, let endpoint {
            _ = try? await call(endpoint, method: "Browser.close", timeout: 3)
        }
        let deadline = Date().addingTimeInterval(3)
        while process.isRunning && Date() < deadline {
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        closeNow()
    }

    /// Synchronous: kill Chrome, delete the profile, stop tracking. For app
    /// termination, which does not wait for async work. Safe to call twice.
    func closeNow() {
        if process.isRunning {
            process.terminate()
            process.waitUntilExit()
        }
        lock.lock(); defer { lock.unlock() }
        try? FileManager.default.removeItem(at: profile)
        SignInWindows.shared.forget(self)
    }

    /// One CDP command on the browser target; the reply with our id.
    private func call(_ endpoint: URL, method: String,
                      timeout: TimeInterval = 10) async throws -> Data {
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let socket = session.webSocketTask(with: endpoint)
        socket.maximumMessageSize = 64 * 1024 * 1024
        socket.resume()
        defer { socket.cancel(with: .normalClosure, reason: nil) }
        let request = try JSONSerialization.data(withJSONObject: ["id": 1, "method": method])
        try await socket.send(.string(String(decoding: request, as: UTF8.self)))
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            let message = try await socket.receive()
            let data: Data
            switch message {
            case .string(let text): data = Data(text.utf8)
            case .data(let bytes): data = bytes
            @unknown default: continue
            }
            if let reply = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
               reply["id"] as? Int == 1 {
                return data
            }
        }
        throw MacSignInError.cdp
    }
}

/// Every sign-in Chrome still open, so quitting the app closes them all.
/// Without this a window left open at quit keeps a signed-in profile on disk
/// and a Chrome on screen that nothing will ever close.
public final class SignInWindows: @unchecked Sendable {
    public static let shared = SignInWindows()
    /// AppKit's `NSApplication.willTerminateNotification`, by name: DeckKit
    /// imports no UI framework.
    public static let willTerminate = Notification.Name("NSApplicationWillTerminateNotification")

    private let lock = NSLock()
    private var open: [ObjectIdentifier: ChromeSignInWindow] = [:]

    func register(_ window: ChromeSignInWindow) {
        lock.lock(); defer { lock.unlock() }
        open[ObjectIdentifier(window)] = window
    }

    func forget(_ window: ChromeSignInWindow) {
        lock.lock(); defer { lock.unlock() }
        open[ObjectIdentifier(window)] = nil
    }

    func isTracking(_ window: ChromeSignInWindow) -> Bool {
        lock.lock(); defer { lock.unlock() }
        return open[ObjectIdentifier(window)] != nil
    }

    /// Closes every open sign-in window now.
    public func closeAllNow() {
        lock.lock()
        let windows = Array(open.values)
        lock.unlock()
        for window in windows { window.closeNow() }
    }

    /// Call once at launch. Returns the observer token.
    @discardableResult
    public static func closeAllOnTerminate(center: NotificationCenter = .default) -> NSObjectProtocol {
        center.addObserver(forName: willTerminate, object: nil, queue: nil) { _ in
            SignInWindows.shared.closeAllNow()
        }
    }
}
#endif

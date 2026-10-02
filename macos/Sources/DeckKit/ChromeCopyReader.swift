import Foundation

// Mac-only: LaunchServices, Chrome and `Process` exist only there. Fenced so
// DeckKit still builds for the iPhone app, which links the same package.
#if os(macOS)
import CoreServices

/// **The live half of "use my Mac's login": find his browser, launch Chrome on
/// a copy of his profile, read the cookies.** Faked in tests through
/// `ProfileCookieReader`; this is what runs against a real Chrome.

public enum MacLoginDiscovery {
    /// His default browser's bundle id (LaunchServices), lowercased.
    public static func defaultBrowserBundleID() -> String? {
        guard let handler = LSCopyDefaultHandlerForURLScheme("https" as CFString)?
            .takeRetainedValue() as String? else { return nil }
        return handler.lowercased()
    }

    static let chromeSupport = NSHomeDirectory() + "/Library/Application Support/Google/Chrome"

    /// The Chrome profile he is actually using: the one whose cookie file was
    /// touched most recently. Returns (profile dir, Local State path, label).
    public static func activeChromeProfile(support: String? = nil)
        -> (profile: URL, localState: URL, name: String)? {
        let root = URL(fileURLWithPath: support ?? chromeSupport)
        let localState = root.appendingPathComponent("Local State")
        guard let data = try? Data(contentsOf: localState) else { return nil }
        let profiles = ChromeProfile.all(fromLocalState: data)
        var best: (URL, String, Date)?
        for p in profiles {
            let cookies = root.appendingPathComponent(p.dir).appendingPathComponent("Cookies")
            guard let m = (try? FileManager.default.attributesOfItem(atPath: cookies.path))?[.modificationDate] as? Date
            else { continue }
            if best == nil || m > best!.2 {
                best = (root.appendingPathComponent(p.dir), p.name, m)
            }
        }
        guard let best else { return nil }
        return (best.0, localState, best.1)
    }

    /// Builds the extractor for whichever browser he uses, or nil if none is
    /// supported/among the ones we can read.
    public static func extractor(reader: ProfileCookieReader = ChromeCopyReader())
        -> (extractor: ProfileCookieExtracting, browserLabel: String)? {
        let bundle = defaultBrowserBundleID()
        switch MacBrowsers.family(forBundleID: bundle) {
        case .chrome:
            guard let (profile, localState, _) = activeChromeProfile() else { return nil }
            return (ChromeProfileExtractor(sourceProfile: profile, localState: localState,
                                           reader: reader), MacBrowsers.label(forBundleID: bundle))
        case .safari:
            let file = URL(fileURLWithPath: NSHomeDirectory()
                + "/Library/Cookies/Cookies.binarycookies")
            return (SafariProfileExtractor(cookieFile: file), "Safari")
        default:
            // Chrome is installed on nearly every Mac; fall back to it even when
            // it is not the default, so a Firefox user still gets a path.
            guard let (profile, localState, _) = activeChromeProfile() else { return nil }
            return (ChromeProfileExtractor(sourceProfile: profile, localState: localState,
                                           reader: reader), "Chrome")
        }
    }
}

/// Launches Google Chrome on a copied profile with a loopback CDP port, reads
/// every cookie (`Network.enable` then a navigation makes the store load, as
/// measured), and quits Chrome. The copy is owned and deleted by
/// `ChromeProfileExtractor`; this only reads.
public struct ChromeCopyReader: ProfileCookieReader {
    var startTimeout: TimeInterval = 25
    public init() {}

    public func readCookies(profileRoot: URL) async throws -> Data {
        guard let chrome = ChromeSignInBrowser.candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else { throw MacSignInError.chromeMissing }

        let process = Process()
        process.executableURL = URL(fileURLWithPath: chrome)
        process.arguments = [
            "--user-data-dir=\(profileRoot.path)", "--profile-directory=Default",
            "--remote-debugging-port=0", "--remote-debugging-address=127.0.0.1",
            "--no-first-run", "--no-default-browser-check",
            "--window-position=-3000,-3000", "--window-size=420,320", "about:blank",
        ]
        process.standardOutput = FileHandle.nullDevice
        process.standardError = FileHandle.nullDevice
        do { try process.run() } catch { throw MacSignInError.chromeDidNotStart }
        defer {
            if process.isRunning { process.terminate(); process.waitUntilExit() }
        }

        let portFile = profileRoot.appendingPathComponent("DevToolsActivePort")
        let deadline = Date().addingTimeInterval(startTimeout)
        var endpoint: URL?
        while Date() < deadline {
            if let text = try? String(contentsOf: portFile, encoding: .utf8),
               let found = ChromeSignInBrowser.activePort(text) {
                endpoint = URL(string: "ws://127.0.0.1:\(found.port)\(found.path)")
                break
            }
            if !process.isRunning { throw MacSignInError.chromeDidNotStart }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        guard let endpoint else { throw MacSignInError.chromeDidNotStart }

        // The store loads lazily; enable Network and touch a page target first.
        let page = try await pageEndpoint(portBase: endpoint)
        _ = try await cdp(page, "Network.enable")
        _ = try await cdp(page, "Page.navigate", ["url": "about:blank"])
        try? await Task.sleep(nanoseconds: 800_000_000)
        let reply = try await cdp(page, "Network.getAllCookies")
        guard let result = (try? JSONSerialization.jsonObject(with: reply)) as? [String: Any],
              let cookies = (result["result"] as? [String: Any])?["cookies"] as? [Any]
        else { throw MacSignInError.cdp }
        return try JSONSerialization.data(withJSONObject: cookies)
    }

    /// The first page target's ws URL, off the HTTP `/json` list.
    private func pageEndpoint(portBase: URL) async throws -> URL {
        guard let host = portBase.host, let port = portBase.port,
              let listURL = URL(string: "http://\(host):\(port)/json") else {
            throw MacSignInError.cdp
        }
        let (data, _) = try await URLSession(configuration: .ephemeral).data(from: listURL)
        guard let targets = (try? JSONSerialization.jsonObject(with: data)) as? [[String: Any]],
              let page = targets.first(where: { ($0["type"] as? String) == "page" }),
              let ws = page["webSocketDebuggerUrl"] as? String, let url = URL(string: ws)
        else { throw MacSignInError.cdp }
        return url
    }

    private func cdp(_ endpoint: URL, _ method: String, _ params: [String: Any] = [:]) async throws -> Data {
        let session = URLSession(configuration: .ephemeral)
        defer { session.invalidateAndCancel() }
        let socket = session.webSocketTask(with: endpoint)
        socket.maximumMessageSize = 64 * 1024 * 1024
        socket.resume()
        defer { socket.cancel(with: .normalClosure, reason: nil) }
        let msg = try JSONSerialization.data(withJSONObject: ["id": 1, "method": method, "params": params])
        try await socket.send(.string(String(decoding: msg, as: UTF8.self)))
        let deadline = Date().addingTimeInterval(15)
        while Date() < deadline {
            switch try await socket.receive() {
            case .string(let text):
                let d = Data(text.utf8)
                if let r = (try? JSONSerialization.jsonObject(with: d)) as? [String: Any],
                   r["id"] as? Int == 1 { return d }
            case .data: continue
            @unknown default: continue
            }
        }
        throw MacSignInError.cdp
    }
}
#endif

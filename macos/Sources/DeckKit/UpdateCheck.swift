import Foundation

/// A dotted numeric version. `0.10.0 > 0.9.9`, and `1.0 == 1.0.0`.
public struct AppVersion: Comparable, Equatable, Sendable {
    private let parts: [Int]

    public init?(_ text: String) {
        let pieces = text.trimmingCharacters(in: .whitespaces).split(separator: ".", omittingEmptySubsequences: false)
        let numbers = pieces.compactMap { Int($0) }
        guard !pieces.isEmpty, numbers.count == pieces.count else { return nil }
        var p = numbers
        while p.count > 1, p.last == 0 { p.removeLast() }
        parts = p
    }

    public static func < (a: AppVersion, b: AppVersion) -> Bool {
        for i in 0..<max(a.parts.count, b.parts.count) {
            let x = i < a.parts.count ? a.parts[i] : 0, y = i < b.parts.count ? b.parts[i] : 0
            if x != y { return x < y }
        }
        return false
    }
}

/// `GET /v1/version`.
public struct VersionInfo: Equatable, Sendable, Decodable {
    public let version: String
    public let minApp: String
    public let api: Int

    public init(version: String, minApp: String, api: Int) {
        self.version = version; self.minApp = minApp; self.api = api
    }

    private enum CodingKeys: String, CodingKey { case version, minApp = "min_app", api }
}

/// `<RELEASE_BASE>/manifest.json` (K8).
public struct ReleaseManifest: Equatable, Sendable, Decodable {
    public struct App: Equatable, Sendable, Decodable {
        public let url: String
        public let sha256: String
        public let minMacos: String?
        private enum CodingKeys: String, CodingKey { case url, sha256, minMacos = "min_macos" }
    }
    public let version: String
    public let released: String?
    public let app: App
    public let minApp: String?
    public let notes: String?
    private enum CodingKeys: String, CodingKey { case version, released, app, minApp = "min_app", notes }
}

/// "Agent Deck 0.9.1 is available — Download". Non-modal; opens the DMG URL.
public struct UpdateNotice: Equatable, Sendable {
    public let version: String
    public let downloadURL: URL
    public let notes: String?
    public var headline: String { "Shaliach \(version) is available" }
}

public enum UpdateCheck {
    public static let interval: TimeInterval = 24 * 3600
    public static let tooOldText = "Your server was updated; update this app to keep working."

    public static func decodeManifest(_ data: Data) throws -> ReleaseManifest {
        try JSONDecoder().decode(ReleaseManifest.self, from: data)
    }

    public static func decodeVersion(_ data: Data) throws -> VersionInfo {
        try JSONDecoder().decode(VersionInfo.self, from: data)
    }

    /// nil when this app is current, newer, or its own version is unreadable (a
    /// dev build must never nag).
    public static func notice(manifest: ReleaseManifest, appVersion: String) -> UpdateNotice? {
        guard let mine = AppVersion(appVersion), let theirs = AppVersion(manifest.version), theirs > mine,
              let url = URL(string: manifest.app.url), url.scheme == "https" || url.scheme == "http"
        else { return nil }
        return UpdateNotice(version: manifest.version, downloadURL: url, notes: manifest.notes)
    }

    /// The blocking case: the server demands a newer app than this one.
    public static func appIsTooOld(server: VersionInfo, appVersion: String) -> Bool {
        guard let mine = AppVersion(appVersion), let need = AppVersion(server.minApp) else { return false }
        return mine < need
    }

    public static func isDue(lastChecked: Date?, now: Date = Date()) -> Bool {
        guard let last = lastChecked else { return true }
        return now.timeIntervalSince(last) >= interval
    }

    /// A bare GET: no bearer, no cookies, no identifiers of any kind.
    public static func fetchManifest(from url: URL, performer: RequestPerformer) async throws -> ReleaseManifest {
        var request = URLRequest(url: url)
        request.httpMethod = "GET"
        request.httpShouldHandleCookies = false
        let (data, response) = try await performer.perform(request)
        guard (200..<300).contains(response.statusCode) else {
            throw DeckError.http(response.statusCode, reason: "")
        }
        return try decodeManifest(data)
    }
}

import Foundation

/// **Reading the login he already has, and giving it to the desks.**
///
/// The primary action on a sign-in card. It reads the requested site's cookies
/// from the browser he is already signed in with, filters to that site, and
/// hands them to the deck's `/v1/logins` — the same route and fan-out the fresh
/// passkey flow uses, so nothing server-side changes.

public enum MacLoginImportError: Error, Equatable, Sendable {
    /// The requested site has no cookies in his browser here — offer passkey.
    case notLoggedInHere
    case fullDiskAccessNeeded   // Safari, no FDA
    case noBrowser
    /// His live session for this site must not leave the Mac (the sentence says why).
    case wouldSignYouOut(String)
}

/// Something that can read a site's cookies from a browser on this Mac and
/// return them as a CDP cookie array (JSON), already scoped to that site.
public protocol ProfileCookieExtracting: Sendable {
    func extract(site host: String) async throws -> Data
}

/// Launches a browser on a copied profile directory and returns its cookies as
/// a CDP array (`Network.getAllCookies`). The one part that needs a real
/// browser; faked in tests.
public protocol ProfileCookieReader: Sendable {
    func readCookies(profileRoot: URL) async throws -> Data
}

/// **Copy the profile, read it, delete the copy — always.**
///
/// Chrome 136+ refuses a debugging port on the live profile, so a copy is the
/// only way in. The copy holds his session, so it is created 0700 and deleted
/// on every exit, success or throw. On the same Mac the copy decrypts with the
/// same Keychain key the running Chrome already holds, so no prompt (measured).
public struct ChromeProfileExtractor: ProfileCookieExtracting {
    let sourceProfile: URL   // …/Chrome/Profile 10
    let localState: URL      // …/Chrome/Local State
    let reader: ProfileCookieReader
    let fileManager: FileManager
    /// Where the throwaway copy lives; overridable for tests.
    var tempParent: URL

    public init(sourceProfile: URL, localState: URL, reader: ProfileCookieReader,
                fileManager: FileManager = .default, tempParent: URL? = nil) {
        self.sourceProfile = sourceProfile
        self.localState = localState
        self.reader = reader
        self.fileManager = fileManager
        self.tempParent = tempParent
            ?? fileManager.temporaryDirectory.appendingPathComponent("deck-uselogin", isDirectory: true)
    }

    public func extract(site host: String) async throws -> Data {
        return try SiteCookies.filterJSON(try await readAll(), toSite: host)
    }

    /// Every cookie in the profile, unfiltered, as a CDP array. The copy is read
    /// and deleted here too. Used by the fresh passkey sign-in, which wants the
    /// whole Google session (accounts.google.com included), not one site.
    public func readAll() async throws -> Data {
        let root = try makeCopy()
        defer { try? fileManager.removeItem(at: root) }   // ALWAYS
        return try await reader.readCookies(profileRoot: root)
    }

    /// A 0700 `user-data-dir` holding `Default/Cookies` and `Local State`.
    func makeCopy() throws -> URL {
        try fileManager.createDirectory(at: tempParent, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
        let root = tempParent.appendingPathComponent("uld-\(UUID().uuidString)")
        let dst = root.appendingPathComponent("Default")
        try fileManager.createDirectory(at: dst, withIntermediateDirectories: true,
                                        attributes: [.posixPermissions: 0o700])
        for f in ["Cookies", "Cookies-wal", "Cookies-journal"] {
            let src = sourceProfile.appendingPathComponent(f)
            if fileManager.fileExists(atPath: src.path) {
                try? fileManager.copyItem(at: src, to: dst.appendingPathComponent(f))
            }
        }
        try fileManager.copyItem(at: localState, to: root.appendingPathComponent("Local State"))
        return root
    }
}

/// Reads Safari's cookie file (needs Full Disk Access) and returns a CDP array.
public struct SafariProfileExtractor: ProfileCookieExtracting {
    let cookieFile: URL
    let fileManager: FileManager
    public init(cookieFile: URL, fileManager: FileManager = .default) {
        self.cookieFile = cookieFile
        self.fileManager = fileManager
    }
    public func extract(site host: String) async throws -> Data {
        let rows: [[String: Any]]
        do {
            rows = try SafariCookies.read(cookieFile)
        } catch SafariCookiesError.accessDenied {
            throw MacLoginImportError.fullDiskAccessNeeded
        }
        return try SiteCookies.filterJSON(
            try JSONSerialization.data(withJSONObject: rows), toSite: host)
    }
}

/// Extract, then share. Returns how many desks got the login.
public struct MacLoginImport: Sendable {
    let extractor: ProfileCookieExtracting
    let sharing: LoginSharingClient
    public init(extractor: ProfileCookieExtracting, sharing: LoginSharingClient) {
        self.extractor = extractor
        self.sharing = sharing
    }

    public func run(site host: String) async throws -> LoginShare {
        if let why = MacSignIn.chromeLoginRefusal(host: host) {
            throw MacLoginImportError.wouldSignYouOut(why)
        }
        let filtered = try await extractor.extract(site: host)
        let rows = (try? JSONSerialization.jsonObject(with: filtered)) as? [Any] ?? []
        guard !rows.isEmpty else { throw MacLoginImportError.notLoggedInHere }
        return try await sharing.shareLogins(cookieJSON: filtered, via: "chrome")
    }

    /// One sentence for him, per way importing an existing login can go.
    public static func sentence(for error: Error) -> String {
        switch error {
        case MacLoginImportError.notLoggedInHere:
            return "You are not signed in to this site in your browser here. Use \"Sign in fresh with passkey\" instead."
        case MacLoginImportError.fullDiskAccessNeeded:
            return SafariAccess.missingMessage
        case MacLoginImportError.noBrowser:
            return "No supported browser was found on this Mac."
        case MacLoginImportError.wouldSignYouOut(let why):
            return why
        default:
            return MacSignIn.sentence(for: error)
        }
    }
}

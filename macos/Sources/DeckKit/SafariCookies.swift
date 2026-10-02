import Foundation

/// **Safari's `Cookies.binarycookies`, and the wall in front of it.**
///
/// If Safari is his browser, his session is in `~/Library/Cookies/Cookies.binarycookies`
/// (and per-container copies). That file is only readable with **Full Disk
/// Access**, which a fresh app does not have — so the card must detect the wall
/// and send him straight to the right Settings pane, not fail silently.
///
/// The parser below is the documented binarycookies layout, and it emits the
/// same CDP-shaped rows the Chrome path does, so both feed one filter and one
/// upload. Values are carried, never logged.
public enum SafariAccess {
    /// The Full Disk Access pane. macOS routes this scheme to System Settings.
    public static let fullDiskAccessURL =
        URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")!

    public static let missingMessage =
        "Safari's logins are locked until you give Shaliach Full Disk Access. "
        + "Open Settings, turn it on for Shaliach, then try again."

    /// A directory is readable when we can list it; the cookie file's own read
    /// is attempted by the parser, which reports the wall as `.accessDenied`.
    public static func canRead(_ path: URL) -> Bool {
        FileManager.default.isReadableFile(atPath: path.path)
    }
}

public enum SafariCookiesError: Error, Equatable, Sendable {
    case accessDenied      // Full Disk Access missing
    case notBinaryCookies  // wrong magic
    case truncated
}

public enum SafariCookies {
    /// Parse a `Cookies.binarycookies` blob into CDP-shaped cookie dictionaries
    /// (`name`, `value`, `domain`, `path`, `secure`, `httpOnly`, `expires`,
    /// `session`). PURE.
    public static func parse(_ data: Data) throws -> [[String: Any]] {
        let bytes = [UInt8](data)
        guard bytes.count >= 8 else { throw SafariCookiesError.truncated }
        guard bytes[0] == 0x63, bytes[1] == 0x6F, bytes[2] == 0x6F, bytes[3] == 0x6B
        else { throw SafariCookiesError.notBinaryCookies }   // "cook"

        func be32(_ o: Int) -> Int {
            Int(bytes[o]) << 24 | Int(bytes[o+1]) << 16 | Int(bytes[o+2]) << 8 | Int(bytes[o+3])
        }
        func le32(_ o: Int) -> Int {
            Int(bytes[o]) | Int(bytes[o+1]) << 8 | Int(bytes[o+2]) << 16 | Int(bytes[o+3]) << 24
        }
        func leDouble(_ o: Int) throws -> Double {
            guard o + 8 <= bytes.count else { throw SafariCookiesError.truncated }
            var v: UInt64 = 0
            for i in 0..<8 { v |= UInt64(bytes[o+i]) << (8*i) }
            return Double(bitPattern: v)
        }
        func cString(_ o: Int) -> String {
            guard o >= 0, o < bytes.count else { return "" }
            var end = o
            while end < bytes.count && bytes[end] != 0 { end += 1 }
            return String(decoding: bytes[o..<end], as: UTF8.self)
        }

        let numPages = be32(4)
        var cursor = 8
        var pageSizes: [Int] = []
        for _ in 0..<numPages {
            guard cursor + 4 <= bytes.count else { throw SafariCookiesError.truncated }
            pageSizes.append(be32(cursor)); cursor += 4
        }

        // Mac absolute time (seconds since 2001-01-01) to Unix seconds.
        let macEpochOffset = 978_307_200.0
        var out: [[String: Any]] = []

        var pageStart = cursor
        for size in pageSizes {
            guard pageStart + size <= bytes.count else { throw SafariCookiesError.truncated }
            let numCookies = le32(pageStart + 4)
            var offsets: [Int] = []
            for i in 0..<numCookies { offsets.append(le32(pageStart + 8 + i*4)) }

            for co in offsets {
                let c = pageStart + co
                guard c + 48 <= bytes.count else { throw SafariCookiesError.truncated }
                let flags = le32(c + 8)
                let domain = cString(c + le32(c + 16))
                let name = cString(c + le32(c + 20))
                let path = cString(c + le32(c + 24))
                let value = cString(c + le32(c + 28))
                let expiry = try leDouble(c + 40)
                var row: [String: Any] = [
                    "name": name, "value": value, "domain": domain,
                    "path": path.isEmpty ? "/" : path,
                    "secure": (flags & 0x1) != 0,
                    "httpOnly": (flags & 0x4) != 0,
                ]
                if expiry > 0 {
                    row["expires"] = expiry + macEpochOffset
                    row["session"] = false
                } else {
                    row["session"] = true
                }
                if !name.isEmpty && !domain.isEmpty { out.append(row) }
            }
            pageStart += size
        }
        return out
    }

    /// Read and parse the cookie file, mapping a permission failure to the
    /// Full-Disk-Access wall.
    public static func read(_ path: URL) throws -> [[String: Any]] {
        let data: Data
        do {
            data = try Data(contentsOf: path)
        } catch {
            throw SafariCookiesError.accessDenied
        }
        return try parse(data)
    }
}

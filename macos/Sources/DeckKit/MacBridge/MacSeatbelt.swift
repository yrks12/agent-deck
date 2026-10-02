import Foundation

/// A Seatbelt (SBPL) profile, ready for `sandbox-exec -p`.
public struct MacSeatbeltProfile: Equatable, Sendable {
    public let text: String
    public init(text: String) { self.text = text }
}

/// **What an Ask-mode command may touch, said to the kernel rather than hoped.**
///
/// Measured on this Mac (Darwin 25.6, 2026-09-30): `/usr/bin/sandbox-exec -p`
/// still enforces an SBPL profile; a denied read or write is `EPERM`; rules
/// are last-match-wins; `regex` filters work; and paths are matched **after**
/// resolution — a rule on `/var/folders/…` does not cover `$TMPDIR`, only
/// `/private/var/folders/…` does. Every path written here is therefore already
/// resolved by `MacPaths`.
///
/// Shape (MB5): allow everything, then deny all writes except the chosen
/// folders and temp; deny reads under `$HOME` except the chosen folders, shell
/// dotfiles and toolchains; finally deny "Never touch" both ways, and re-allow
/// the explicit exceptions — last match wins, so an exception beats it.
public enum MacSeatbelt {

    public static let executable = "/usr/bin/sandbox-exec"

    /// Dotfiles and toolchain roots a login shell needs to find `brew`, `node`,
    /// `git`, `python3`. Relative to home.
    public static let homeReadAllow: [String] = [
        ".zshenv", ".zprofile", ".zshrc", ".zlogin", ".bash_profile", ".bashrc", ".profile",
        ".gitconfig", ".config/git", ".nvm", ".pyenv", ".rbenv", ".cargo", ".rustup",
        ".local", ".bun", ".deno",
    ]

    /// Is Seatbelt usable here? Runs a no-op profile once; cached.
    /// iOS has no `Process` and no `sandbox-exec`: never available there.
    public static let isAvailable: Bool = {
        #if os(macOS)
        guard FileManager.default.isExecutableFile(atPath: executable) else { return false }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: executable)
        p.arguments = ["-p", "(version 1)(allow default)", "/usr/bin/true"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        do { try p.run() } catch { return false }
        p.waitUntilExit()
        return p.terminationStatus == 0
        #else
        return false
        #endif
    }()

    /// The temp directories a command may always write: `/private/tmp`, and
    /// this user's `confstr` temp and cache dirs, resolved.
    public static func tempDirectories() -> [String] {
        var dirs = ["/private/tmp"]
        for name in [_CS_DARWIN_USER_TEMP_DIR, _CS_DARWIN_USER_CACHE_DIR] {
            var buf = [CChar](repeating: 0, count: Int(PATH_MAX))
            if confstr(name, &buf, buf.count) > 0 {
                let raw = String(cString: buf)
                dirs.append(MacPaths.realpathOrSelf(MacPaths.standardise(raw)))
            }
        }
        return dirs
    }

    /// Folders and temp dirs must already be resolved paths; never-touch and
    /// exception entries are patterns as the user wrote them.
    public static func profile(paths: MacPaths, folders: [String], neverTouch: [String],
                               exceptions: [String], tempDirs: [String]) -> MacSeatbeltProfile {
        let home = paths.home
        var s = "(version 1)\n(allow default)\n"

        s += "(deny file-write*)\n(allow file-write*"
        for dir in folders + tempDirs { s += " (subpath \(quote(dir)))" }
        s += " (regex #\"^/dev/(null|zero|tty|dtracehelper|fd/[0-9]+)$\"))\n"

        s += "(deny file-read* (subpath \(quote(home))))\n(allow file-read* (literal \(quote(home)))"
        for rel in homeReadAllow {
            let full = home + "/" + rel
            s += " (literal \(quote(full))) (subpath \(quote(full)))"
        }
        for dir in folders { s += " (subpath \(quote(dir)))" }
        s += ")\n"

        let denied = neverTouch.map { "(regex \(quote("^" + MacPaths.globRegex(paths.resolvePattern($0)) + "$")))" }
        if !denied.isEmpty { s += "(deny file-read* file-write* \(denied.joined(separator: " ")))\n" }
        // An exception undoes "Never touch" only; it must not widen the
        // folders. So it is re-allowed only where it meets a chosen folder.
        if !exceptions.isEmpty && !folders.isEmpty {
            s += "(allow file-read* file-write*"
            for e in exceptions {
                let re = quote("^" + MacPaths.globRegex(paths.resolvePattern(e)) + "$")
                for dir in folders { s += " (require-all (subpath \(quote(dir))) (regex \(re)))" }
            }
            s += ")\n"
        }
        return MacSeatbeltProfile(text: s)
    }

    /// An SBPL string literal. Backslash and quote are escaped (measured: both
    /// work in `subpath` and in a plain-string `regex`).
    public static func quote(_ s: String) -> String {
        "\"" + s.replacingOccurrences(of: "\\", with: "\\\\").replacingOccurrences(of: "\"", with: "\\\"") + "\""
    }
}

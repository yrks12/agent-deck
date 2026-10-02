import Foundation

/// **Every path a desk names is resolved before anything is matched against it.**
///
/// A desk says `~/w/../.ssh/id_ed25519`, or names a symlink that points into
/// `~/.ssh`. Matching the text it sent would let both through. So a path is
/// expanded (`~`), standardised (`..`, `.`), and resolved with `realpath` —
/// the deepest existing ancestor for a file that does not exist yet, so a
/// write target is judged by where it would really land. Patterns get the same
/// treatment on their literal prefix, because on this Mac `/var` is
/// `/private/var` and `/tmp` is `/private/tmp`: Seatbelt matches the resolved
/// path (measured on Darwin 25.6), and so must we.
public struct MacPaths: Equatable, Sendable {

    /// The user's home, resolved.
    public let home: String

    public init(home: String = NSHomeDirectory()) {
        self.home = MacPaths.realpathOrSelf(MacPaths.standardise(home))
    }

    /// `~/Library/Application Support/Agent Deck` — the bridge's own files.
    public var supportDirectory: String { home + "/Library/Application Support/Agent Deck" }

    /// `~` / `~/x` → under home; an absolute path is kept; anything else
    /// (relative, `~other`, empty, control characters) is `nil` — `bad_path`.
    public func expand(_ raw: String) -> String? {
        guard !raw.isEmpty, !raw.unicodeScalars.contains(where: { $0.value < 0x20 || $0.value == 0x7f }) else {
            return nil
        }
        if raw == "~" { return home }
        if raw.hasPrefix("~/") { return home + raw.dropFirst(1) }
        return raw.hasPrefix("/") ? raw : nil
    }

    /// Expanded, standardised and resolved. For a path that does not exist the
    /// deepest existing ancestor is resolved and the rest appended, so
    /// `<symlink-to-.ssh>/new_key` resolves into `~/.ssh`.
    public func resolve(_ raw: String) -> String? {
        guard let expanded = expand(raw) else { return nil }
        return MacPaths.resolveExisting(MacPaths.standardise(expanded))
    }

    /// Is `path` equal to `folder` or below it? Both must already be resolved.
    public static func isInside(_ path: String, folder: String) -> Bool {
        if folder == "/" { return true }
        return path == folder || path.hasPrefix(folder.hasSuffix("/") ? folder : folder + "/")
    }

    /// A pattern with `~` expanded and its literal (glob-free) prefix resolved,
    /// so it can be compared with resolved paths. `**/.env` stays as it is.
    public func resolvePattern(_ pattern: String) -> String {
        let expanded = pattern.hasPrefix("~") ? (expand(pattern) ?? pattern) : pattern
        guard expanded.hasPrefix("/") else { return expanded }
        let parts = expanded.split(separator: "/", omittingEmptySubsequences: false).map(String.init)
        guard let firstGlob = parts.firstIndex(where: { $0.contains(where: { "*?{[".contains($0) }) }) else {
            return MacPaths.resolveExisting(MacPaths.standardise(expanded))
        }
        let literal = parts[..<firstGlob].joined(separator: "/")
        let rest = parts[firstGlob...].joined(separator: "/")
        let resolved = literal.isEmpty ? "" : MacPaths.resolveExisting(MacPaths.standardise(literal))
        return (resolved == "/" ? "" : resolved) + "/" + rest
    }

    /// Does `path` (resolved) match `pattern` (as given by the user)?
    public func matches(_ pattern: String, _ path: String) -> Bool {
        let p = resolvePattern(pattern)
        guard let regex = try? NSRegularExpression(pattern: "^" + MacPaths.globRegex(p) + "$") else { return false }
        return regex.firstMatch(in: path, range: NSRange(path.startIndex..., in: path)) != nil
    }

    /// A glob as a regular expression body (no anchors). `**/` is any number of
    /// directories (including none), a trailing `/**` also matches the
    /// directory itself, `*` and `?` stay inside one component, `{a,b}` is
    /// alternation. Everything else is literal.
    public static func globRegex(_ glob: String) -> String {
        var out = ""
        let chars = Array(glob)
        var i = 0
        var braceDepth = 0
        while i < chars.count {
            let c = chars[i]
            if c == "*" && i + 1 < chars.count && chars[i + 1] == "*" {
                let atEnd = i + 2 == chars.count
                let followedBySlash = i + 2 < chars.count && chars[i + 2] == "/"
                if out.hasSuffix("/") && atEnd {
                    out.removeLast()
                    out += "(/.*)?"
                    i += 2
                } else if followedBySlash {
                    out += "(.*/)?"
                    i += 3
                } else {
                    out += ".*"
                    i += 2
                }
                continue
            }
            switch c {
            case "*": out += "[^/]*"
            case "?": out += "[^/]"
            case "{": braceDepth += 1; out += "("
            case "}" where braceDepth > 0: braceDepth -= 1; out += ")"
            case "," where braceDepth > 0: out += "|"
            default:
                if "\\^$.|+()[]{}".contains(c) { out += "\\" }
                out.append(c)
            }
            i += 1
        }
        return out
    }

    static func standardise(_ path: String) -> String {
        var stack: [String] = []
        for part in path.split(separator: "/") {
            switch part {
            case ".": continue
            case "..": if !stack.isEmpty { stack.removeLast() }
            default: stack.append(String(part))
            }
        }
        return "/" + stack.joined(separator: "/")
    }

    static func realpathOrSelf(_ path: String) -> String {
        guard let r = Darwin.realpath(path, nil) else { return path }
        defer { free(r) }
        return String(cString: r)
    }

    /// Resolve the deepest ancestor that exists and re-append the rest. A
    /// dangling symlink is followed to where it points, so a write through
    /// `x -> ~/.ssh/new_key` is judged as `~/.ssh/new_key`.
    static func resolveExisting(_ standardised: String, depth: Int = 0) -> String {
        var head = standardised
        var tail: [String] = []
        while true {
            if let r = Darwin.realpath(head, nil) {
                defer { free(r) }
                let base = String(cString: r)
                if tail.isEmpty { return base }
                return (base == "/" ? "" : base) + "/" + tail.reversed().joined(separator: "/")
            }
            if head == "/" || head.isEmpty { return standardised }
            if depth < 32, let target = try? FileManager.default.destinationOfSymbolicLink(atPath: head) {
                let parent = (head as NSString).deletingLastPathComponent
                let joined = target.hasPrefix("/") ? target : parent + "/" + target
                let rest = tail.reversed().joined(separator: "/")
                return resolveExisting(standardise(rest.isEmpty ? joined : joined + "/" + rest), depth: depth + 1)
            }
            let ns = head as NSString
            tail.append(ns.lastPathComponent)
            head = ns.deletingLastPathComponent
        }
    }
}

import Darwin
import Foundation

/// A refusal from a file op, in MB3's closed reason set.
public struct MacOpError: Error, Equatable, Sendable {
    public let reason: String
    public let detail: String
    public init(_ reason: String, _ detail: String) {
        self.reason = reason
        self.detail = detail
    }
}

public struct MacReadResult: Equatable, Sendable {
    public var text: String?
    public var base64: String?
    public var size: Int
    public var offset: Int
    public var returned: Int
    public var truncated: Bool
}

public struct MacWriteResult: Equatable, Sendable {
    public var bytes: Int
    public var path: String
}

public struct MacListEntry: Equatable, Sendable {
    public enum Kind: String, Sendable { case dir, file, link, other }
    public var name: String
    public var kind: Kind
    public var size: Int
    /// Epoch seconds.
    public var modified: Double
}

public struct MacListResult: Equatable, Sendable {
    public var path: String
    public var entries: [MacListEntry]
    public var truncated: Bool
}

/// **`read` / `write` / `list`, in-process, on a path the policy already
/// resolved and allowed.** Same caps as MB1: 64 000 bytes per text page,
/// 5 MiB base64 / write, 1 000 entries. A write is a temp file in the same
/// directory then `rename`, so a reader never sees half a file.
public enum MacFileOps {
    public static let textPageMax = 64_000
    public static let binaryMax = 5 << 20
    public static let listMax = 1_000

    public static func read(_ path: String, offset: Int = 0, length: Int = textPageMax, encoding: String = "text",
                            home: String = MacPaths().home) -> Result<MacReadResult, MacOpError> {
        let limit = encoding == "base64" ? binaryMax : textPageMax
        guard offset >= 0, length >= 1, length <= limit, ["text", "base64"].contains(encoding) else {
            return .failure(MacOpError("bad_input", "offset ≥ 0, length 1…\(limit), encoding text or base64."))
        }
        var st = stat()
        guard stat(path, &st) == 0 else { return .failure(errnoError(errno, path, home: home)) }
        if st.st_mode & S_IFMT == S_IFDIR { return .failure(MacOpError("not_a_file", "\(path) is a directory; use list.")) }
        guard let fh = FileHandle(forReadingAtPath: path) else { return .failure(errnoError(errno, path, home: home)) }
        defer { try? fh.close() }
        let size = Int(st.st_size)
        do {
            try fh.seek(toOffset: UInt64(min(offset, size)))
            var data = try fh.read(upToCount: length) ?? Data()
            if encoding == "base64" {
                return .success(MacReadResult(text: nil, base64: data.base64EncodedString(), size: size, offset: offset,
                                              returned: data.count, truncated: offset + data.count < size))
            }
            if data.contains(0) { return .failure(notText(path)) }
            // A page may end inside a multi-byte character: give back up to 3
            // bytes rather than call a UTF-8 file binary.
            var text = String(data: data, encoding: .utf8)
            var trim = 0
            while text == nil && trim < 3 && offset + data.count < size && !data.isEmpty {
                data.removeLast()
                trim += 1
                text = String(data: data, encoding: .utf8)
            }
            guard let text else { return .failure(notText(path)) }
            return .success(MacReadResult(text: text, base64: nil, size: size, offset: offset,
                                          returned: data.count, truncated: offset + data.count < size))
        } catch {
            return .failure(MacOpError("bad_input", "Could not read \(path): \(error.localizedDescription)"))
        }
    }

    public static func write(_ path: String, content: String, encoding: String = "text", mode: String = "overwrite",
                             makeDirs: Bool = false, home: String = MacPaths().home) -> Result<MacWriteResult, MacOpError> {
        guard ["overwrite", "append", "create"].contains(mode), ["text", "base64"].contains(encoding) else {
            return .failure(MacOpError("bad_input", "mode overwrite|append|create, encoding text|base64."))
        }
        let data: Data
        if encoding == "base64" {
            guard let d = Data(base64Encoded: content) else { return .failure(MacOpError("bad_input", "content is not valid base64.")) }
            data = d
        } else {
            data = Data(content.utf8)
        }
        guard data.count <= binaryMax else {
            return .failure(MacOpError("too_large", "Writes are capped at 5 MiB; this was \(data.count) bytes."))
        }
        let dir = (path as NSString).deletingLastPathComponent
        let fm = FileManager.default
        var isDir: ObjCBool = false
        if !fm.fileExists(atPath: dir, isDirectory: &isDir) {
            guard makeDirs else { return .failure(MacOpError("no_such_path", "\(dir) does not exist; pass make_dirs to create it.")) }
            do { try fm.createDirectory(atPath: dir, withIntermediateDirectories: true) } catch {
                return .failure(errnoError(posixCode(error), dir, home: home))
            }
        } else if !isDir.boolValue {
            return .failure(MacOpError("not_a_directory", "\(dir) is not a directory."))
        }
        var st = stat()
        let exists = lstat(path, &st) == 0
        if exists && st.st_mode & S_IFMT == S_IFDIR { return .failure(MacOpError("not_a_file", "\(path) is a directory.")) }
        if mode == "create" && exists { return .failure(MacOpError("bad_input", "\(path) already exists (mode create).")) }

        if mode == "append" && exists {
            let fd = open(path, O_WRONLY | O_APPEND)
            guard fd >= 0 else { return .failure(errnoError(errno, path, home: home)) }
            defer { close(fd) }
            let ok = data.withUnsafeBytes { buf -> Bool in
                var off = 0
                while off < buf.count {
                    let n = Darwin.write(fd, buf.baseAddress! + off, buf.count - off)
                    if n < 0 { if errno == EINTR { continue }; return false }
                    off += n
                }
                return true
            }
            return ok ? .success(MacWriteResult(bytes: data.count, path: path)) : .failure(errnoError(errno, path, home: home))
        }

        let tmp = dir + "/.\((path as NSString).lastPathComponent).agentdeck-\(UUID().uuidString.prefix(8))"
        let flags = O_WRONLY | O_CREAT | O_EXCL
        let perms: mode_t = exists ? st.st_mode & 0o7777 : 0o644
        let fd = open(tmp, flags, perms)
        guard fd >= 0 else { return .failure(errnoError(errno, path, home: home)) }
        let wrote = data.withUnsafeBytes { buf -> Bool in
            var off = 0
            while off < buf.count {
                let n = Darwin.write(fd, buf.baseAddress! + off, buf.count - off)
                if n < 0 { if errno == EINTR { continue }; return false }
                off += n
            }
            _ = fsync(fd)
            return true
        }
        let writeErrno = errno
        close(fd)
        guard wrote else { unlink(tmp); return .failure(errnoError(writeErrno, path, home: home)) }
        if mode == "create" {
            // link() fails if the target appeared meanwhile — create never clobbers.
            guard link(tmp, path) == 0 else {
                let e = errno
                unlink(tmp)
                return .failure(e == EEXIST ? MacOpError("bad_input", "\(path) already exists (mode create).") : errnoError(e, path, home: home))
            }
            unlink(tmp)
        } else if rename(tmp, path) != 0 {
            let e = errno
            unlink(tmp)
            return .failure(errnoError(e, path, home: home))
        }
        return .success(MacWriteResult(bytes: data.count, path: path))
    }

    public static func list(_ path: String, hidden: Bool = false, home: String = MacPaths().home) -> Result<MacListResult, MacOpError> {
        var st = stat()
        guard stat(path, &st) == 0 else { return .failure(errnoError(errno, path, home: home)) }
        guard st.st_mode & S_IFMT == S_IFDIR else { return .failure(MacOpError("not_a_directory", "\(path) is not a directory.")) }
        let names: [String]
        do { names = try FileManager.default.contentsOfDirectory(atPath: path) } catch {
            return .failure(errnoError(posixCode(error), path, home: home))
        }
        let visible = names.filter { hidden || !$0.hasPrefix(".") }.sorted()
        var entries: [MacListEntry] = []
        for name in visible.prefix(listMax) {
            var es = stat()
            guard lstat(path + "/" + name, &es) == 0 else { continue }
            let kind: MacListEntry.Kind
            switch es.st_mode & S_IFMT {
            case S_IFDIR: kind = .dir
            case S_IFREG: kind = .file
            case S_IFLNK: kind = .link
            default: kind = .other
            }
            entries.append(MacListEntry(name: name, kind: kind, size: Int(es.st_size),
                                        modified: Double(es.st_mtimespec.tv_sec) + Double(es.st_mtimespec.tv_nsec) / 1e9))
        }
        return .success(MacListResult(path: path, entries: entries, truncated: visible.count > listMax))
    }

    /// The POSIX errno under a Foundation error (EACCES if there is none).
    static func posixCode(_ error: Error) -> Int32 {
        let ns = error as NSError
        if ns.domain == NSPOSIXErrorDomain { return Int32(ns.code) }
        if let under = ns.userInfo[NSUnderlyingErrorKey] as? NSError, under.domain == NSPOSIXErrorDomain { return Int32(under.code) }
        return EACCES
    }

    static func notText(_ path: String) -> MacOpError {
        MacOpError("not_text", "\(path) is not UTF-8 text; read it with encoding base64.")
    }

    /// errno → reason. `EPERM`/`EACCES` under a TCC folder is macOS privacy, not us.
    static func errnoError(_ e: Int32, _ path: String, home: String) -> MacOpError {
        switch e {
        case ENOENT: return MacOpError("no_such_path", "\(path) does not exist.")
        case ENOTDIR: return MacOpError("not_a_directory", "A part of \(path) is not a directory.")
        case EISDIR: return MacOpError("not_a_file", "\(path) is a directory.")
        case EPERM, EACCES:
            if let f = MacTCC.folder(in: Substring(path), home: home), e == EPERM {
                return MacOpError("tcc_denied", MacTCC.detail(f))
            }
            return MacOpError("bad_path", "Permission denied on \(path).")
        default: return MacOpError("bad_input", "\(path): \(String(cString: strerror(e))).")
        }
    }
}

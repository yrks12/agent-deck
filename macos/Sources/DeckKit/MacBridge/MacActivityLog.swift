import Foundation

/// One row of `mac-activity.jsonl`: a decision, or a job that ran.
public struct MacActivityRow: Codable, Equatable, Sendable {
    /// `ran` · `asked` · `refused` · `granted` · `denied`.
    public enum Decision: String, Codable, Sendable { case ran, asked, refused, granted, denied }

    public var ts: Double
    public var jobId: String
    public var desk: String
    public var kind: String
    public var summary: String
    public var decision: Decision
    public var state: String?
    public var reason: String?
    public var exit: Int32?
    public var durationMs: Int?
    public var outBytes: Int?
    public var sandboxed: Bool?

    enum CodingKeys: String, CodingKey {
        case ts, desk, kind, summary, decision, state, reason, exit, sandboxed
        case jobId = "job_id"
        case durationMs = "duration_ms"
        case outBytes = "out_bytes"
    }

    public init(ts: Double, jobId: String, desk: String, kind: String, summary: String, decision: Decision,
                state: String? = nil, reason: String? = nil, exit: Int32? = nil, durationMs: Int? = nil,
                outBytes: Int? = nil, sandboxed: Bool? = nil) {
        self.ts = ts
        self.jobId = jobId
        self.desk = desk
        self.kind = kind
        self.summary = String(summary.prefix(MacActivityLog.summaryMax))
        self.decision = decision
        self.state = state
        self.reason = reason
        self.exit = exit
        self.durationMs = durationMs
        self.outBytes = outBytes
        self.sandboxed = sandboxed
    }
}

/// **What happened on this Mac, kept on this Mac.**
///
/// The server keeps its own audit copy; this one is the user's, so a
/// compromised server cannot rewrite it (and in Ask mode no desk can reach
/// it — it is under the non-removable "Never touch" directory). One JSON line
/// per decision or job; the last 4 KiB of each job's output in a sibling
/// directory, for "click a row". Rotates at 10 MB keeping one generation;
/// nothing older than 30 days is shown or kept.
public final class MacActivityLog: @unchecked Sendable {
    public static let summaryMax = 500
    public static let outputKeep = 4096

    public let fileURL: URL
    public let outputDirectory: URL
    let maxBytes: Int
    let keepSeconds: Double
    private let lock = NSLock()

    public init(directory: String = MacPaths().supportDirectory, maxBytes: Int = 10_000_000, keepDays: Double = 30) {
        fileURL = URL(fileURLWithPath: directory + "/mac-activity.jsonl")
        outputDirectory = URL(fileURLWithPath: directory + "/mac-activity-output")
        self.maxBytes = maxBytes
        keepSeconds = keepDays * 86_400
    }

    var rotatedURL: URL { fileURL.deletingPathExtension().appendingPathExtension("1.jsonl") }

    public func append(_ row: MacActivityRow) {
        guard var line = try? JSONEncoder().encode(row) else { return }
        line.append(0x0a)
        lock.withLock {
            let fm = FileManager.default
            try? fm.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true,
                                    attributes: [.posixPermissions: 0o700])
            let size = (try? fm.attributesOfItem(atPath: fileURL.path)[.size] as? Int) ?? 0
            if size + line.count > maxBytes && size > 0 {
                try? fm.removeItem(at: rotatedURL)
                try? fm.moveItem(at: fileURL, to: rotatedURL)
            }
            if let h = try? FileHandle(forWritingTo: fileURL) {
                defer { try? h.close() }
                _ = try? h.seekToEnd()
                try? h.write(contentsOf: line)
            } else {
                try? line.write(to: fileURL)
            }
        }
    }

    /// Newest first, nothing older than the keep window.
    public func recent(limit: Int = 200, now: Double = Date().timeIntervalSince1970) -> [MacActivityRow] {
        lock.withLock {
            var rows: [MacActivityRow] = []
            let decoder = JSONDecoder()
            for url in [rotatedURL, fileURL] {
                guard let data = try? Data(contentsOf: url) else { continue }
                for line in data.split(separator: 0x0a) {
                    if let row = try? decoder.decode(MacActivityRow.self, from: line), now - row.ts < keepSeconds {
                        rows.append(row)
                    }
                }
            }
            return Array(rows.reversed().prefix(limit))
        }
    }

    /// Keep the last 4 KiB of a job's output.
    public func recordOutput(jobId: String, _ data: Data) {
        guard let url = outputURL(jobId), !data.isEmpty else { return }
        lock.withLock {
            try? FileManager.default.createDirectory(at: outputDirectory, withIntermediateDirectories: true,
                                                     attributes: [.posixPermissions: 0o700])
            var kept = (try? Data(contentsOf: url)) ?? Data()
            kept.append(data)
            if kept.count > Self.outputKeep { kept = kept.suffix(Self.outputKeep) }
            try? kept.write(to: url, options: .atomic)
        }
    }

    public func output(jobId: String) -> Data {
        guard let url = outputURL(jobId) else { return Data() }
        return lock.withLock { (try? Data(contentsOf: url)) ?? Data() }
    }

    /// Drop output files and a rotated log older than the keep window.
    public func prune(now: Double = Date().timeIntervalSince1970) {
        lock.withLock {
            let fm = FileManager.default
            func old(_ path: String) -> Bool {
                guard let m = (try? fm.attributesOfItem(atPath: path))?[.modificationDate] as? Date else { return false }
                return now - m.timeIntervalSince1970 >= keepSeconds
            }
            for name in (try? fm.contentsOfDirectory(atPath: outputDirectory.path)) ?? [] {
                let p = outputDirectory.path + "/" + name
                if old(p) { try? fm.removeItem(atPath: p) }
            }
            if old(rotatedURL.path) { try? fm.removeItem(at: rotatedURL) }
        }
    }

    /// A job id becomes a file name only if it is plain (`mj_<hex>`), so a
    /// forged id cannot write outside the output directory.
    func outputURL(_ jobId: String) -> URL? {
        guard !jobId.isEmpty, jobId.count <= 64,
              jobId.unicodeScalars.allSatisfy({ CharacterSet.alphanumerics.contains($0) || $0 == "_" || $0 == "-" }),
              jobId.allSatisfy(\.isASCII) else { return nil }
        return outputDirectory.appendingPathComponent(jobId + ".log")
    }
}

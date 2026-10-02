import Foundation
import AVFoundation

// Owner, 2026-10-01: "can the chat send me videos or image files and I will
// see it there?" -- the playing half: a held video or audio clip streams from
// the deck's byte ranges (`server/uploads.py`) with his token, and any held
// file lands on disk for QuickLook, Save and Share.

// MARK: - byte ranges with his token

/// One answer to a ranged `GET /v1/attachments/...`.
public struct MediaRange: Hashable, Sendable {
    public var data: Data
    /// The whole file's size, when the deck said it.
    public var total: Int64?
    public var contentType: String?

    public init(data: Data, total: Int64?, contentType: String?) {
        self.data = data
        self.total = total
        self.contentType = contentType
    }

    /// `bytes 0-1/10240` -> 10240.
    public static func total(fromContentRange header: String) -> Int64? {
        guard header.hasPrefix("bytes "), let slash = header.lastIndex(of: "/") else { return nil }
        return Int64(header[header.index(after: slash)...])
    }
}

/// `GET` part of a held file.
public protocol AttachmentRangeFetching: Sendable {
    func attachmentRange(url: String, offset: Int64, length: Int?) async throws -> MediaRange
}

/// How one ask from AVFoundation is cut into requests: no single request
/// holds more than `chunk` bytes in memory, however long the video.
public enum MediaChunks {
    public static func plan(offset: Int64, length: Int?, total: Int64, chunk: Int) -> [Range<Int64>] {
        let end = length.map { min(total, offset + Int64($0)) } ?? total
        var out: [Range<Int64>] = []
        var at = offset
        while at < end {
            let next = min(end, at + Int64(chunk))
            out.append(at..<next)
            at = next
        }
        return out
    }
}

/// **A held video or audio clip, played straight from the deck.**
///
/// AVPlayer cannot put his bearer token on its own requests, and would not
/// trust a deck pinned by key (`PinnedTrust`). So the asset's URL uses a
/// private scheme and every byte AVFoundation asks for is fetched here, as
/// ranges, through the same client and token as everything else.
public final class DeckMediaLoader: NSObject, AVAssetResourceLoaderDelegate, @unchecked Sendable {
    public typealias Fetch = @Sendable (_ url: String, _ offset: Int64, _ length: Int?) async throws -> MediaRange
    public static let scheme = "deck-media"

    private let fetch: Fetch
    private let chunk: Int
    private let queue = DispatchQueue(label: "deck.media.loader")
    private let lock = NSLock()
    private var tasks: [ObjectIdentifier: Task<Void, Never>] = [:]
    private var totals: [String: (Int64, String?)] = [:]

    public init(chunk: Int = 1 << 20, fetch: @escaping Fetch) {
        self.fetch = fetch
        self.chunk = chunk
    }

    public convenience init(client: AttachmentRangeFetching, chunk: Int = 1 << 20) {
        self.init(chunk: chunk) { url, offset, length in
            try await client.attachmentRange(url: url, offset: offset, length: length)
        }
    }

    /// An asset for `/v1/attachments/{id}/{name}`. Keep this loader alive as
    /// long as the asset: the resource loader holds its delegate weakly.
    public func asset(for path: String) -> AVURLAsset {
        var parts = URLComponents()
        parts.scheme = Self.scheme
        parts.host = "deck"
        parts.path = path
        let asset = AVURLAsset(url: parts.url ?? URL(string: "\(Self.scheme)://deck")!)
        asset.resourceLoader.setDelegate(self, queue: queue)
        return asset
    }

    public func resourceLoader(_ loader: AVAssetResourceLoader,
                               shouldWaitForLoadingOfRequestedResource request: AVAssetResourceLoadingRequest) -> Bool {
        guard let url = request.request.url, url.scheme == Self.scheme else { return false }
        let path = URLComponents(url: url, resolvingAgainstBaseURL: false)?.percentEncodedPath ?? url.path
        let key = ObjectIdentifier(request)
        let task = Task { [weak self] in
            guard let self else { return }
            do {
                try await self.serve(request, path: path)
                request.finishLoading()
            } catch {
                if !request.isCancelled { request.finishLoading(with: error) }
            }
            self.forget(key)
        }
        lock.lock(); tasks[key] = task; lock.unlock()
        return true
    }

    public func resourceLoader(_ loader: AVAssetResourceLoader,
                               didCancel request: AVAssetResourceLoadingRequest) {
        lock.lock(); let task = tasks.removeValue(forKey: ObjectIdentifier(request)); lock.unlock()
        task?.cancel()
    }

    private func forget(_ key: ObjectIdentifier) {
        lock.lock(); tasks.removeValue(forKey: key); lock.unlock()
    }

    private func known(_ path: String) -> (Int64, String?)? {
        lock.lock(); defer { lock.unlock() }
        return totals[path]
    }

    private func serve(_ request: AVAssetResourceLoadingRequest, path: String) async throws {
        var info = known(path)
        if info == nil {
            let probe = try await fetch(path, 0, 2)
            let total = probe.total ?? Int64(probe.data.count)
            info = (total, probe.contentType)
            lock.lock(); totals[path] = info; lock.unlock()
        }
        guard let (total, type) = info else { return }
        if let card = request.contentInformationRequest {
            card.contentLength = total
            card.isByteRangeAccessSupported = true
            if let type, let uti = Self.uti(forMIME: type) { card.contentType = uti }
        }
        guard let ask = request.dataRequest else { return }
        let offset = ask.currentOffset != 0 ? ask.currentOffset : ask.requestedOffset
        let length: Int? = ask.requestsAllDataToEndOfResource ? nil
            : ask.requestedLength - Int(offset - ask.requestedOffset)
        for range in MediaChunks.plan(offset: offset, length: length, total: total, chunk: chunk) {
            try Task.checkCancellation()
            let got = try await fetch(path, range.lowerBound, Int(range.count))
            // A deck that ignored Range sent the whole file: slice it here.
            let data = got.data.count > range.count && got.data.count == Int(total)
                ? got.data.subdata(in: Int(range.lowerBound)..<Int(range.upperBound)) : got.data
            ask.respond(with: data)
        }
    }

    static func uti(forMIME mime: String) -> String? {
        switch mime.split(separator: ";").first.map(String.init) ?? mime {
        case "video/mp4": return "public.mpeg-4"
        case "video/x-m4v": return "com.apple.m4v-video"
        case "video/quicktime": return "com.apple.quicktime-movie"
        case "audio/mpeg": return "public.mp3"
        case "audio/mp4": return "com.apple.m4a-audio"
        case "audio/aac": return "public.aac-audio"
        case "audio/wav": return "com.microsoft.waveform-audio"
        default: return nil
        }
    }
}

// MARK: - a file on disk for QuickLook, Save and Share

/// Where a held file is kept once fetched: `<root>/<att id>/<name>`.
public struct AttachmentFiles: Sendable {
    public let root: URL

    public init(root: URL = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
                    .appendingPathComponent("DeckAttachments", isDirectory: true)) {
        self.root = root
    }

    /// The file on disk, fetched the first time only.
    public func local(_ attachment: Attachment,
                      fetch: @Sendable (String) async throws -> Data) async throws -> URL {
        guard let url = attachment.url else { throw DeckError.transport("not held by the deck") }
        let parts = url.split(separator: "/")
        let folder = parts.count >= 2 ? String(parts[parts.count - 2]) : "misc"
        let file = root.appendingPathComponent(folder, isDirectory: true)
            .appendingPathComponent(attachment.displayName)
        if FileManager.default.fileExists(atPath: file.path) { return file }
        let data = try await fetch(url)
        try FileManager.default.createDirectory(at: file.deletingLastPathComponent(), withIntermediateDirectories: true)
        try data.write(to: file, options: .atomic)
        return file
    }
}

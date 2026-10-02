import Foundation
import ImageIO
import UniformTypeIdentifiers

// Owner, 2026-09-30: "On the phone I can't add or paste screenshots or files."
//
// The Mac's [+] puts a path in the text and the deck derives the chip from it
// (§9). A phone path means nothing on the box, so the bytes travel first:
//
//   POST /v1/threads/{id}/attachments   raw body, Content-Type, X-Deck-Filename
//     -> {id, name, kind, bytes, path, url}
//
// and the message then carries `Attached image: <path>` — an ordinary message,
// which the desk reads like any path (Claude Code's Read shows it the picture)
// and which the Mac draws as the same chip. `server/uploads.py` is the other
// half; `tests/test_owner_attachments.py` pins the same shape.

/// What the deck answered an upload with.
public struct UploadedAttachment: Codable, Hashable, Sendable {
    public var id: String
    public var name: String
    public var kind: Attachment.Kind
    public var bytes: Int
    /// The file on the deck's machine: what the desk opens.
    public var path: String
    /// `/v1/attachments/{id}/{name}`: what his bubble draws.
    public var url: String

    public init(id: String, name: String, kind: Attachment.Kind, bytes: Int, path: String, url: String) {
        self.id = id
        self.name = name
        self.kind = kind
        self.bytes = bytes
        self.path = path
        self.url = url
    }
}

/// `POST /v1/threads/{id}/attachments`.
public protocol AttachmentUploading: Sendable {
    func uploadAttachment(threadID: String, data: Data, filename: String, mimeType: String,
                          progress: @escaping @Sendable (Double) -> Void) async throws -> UploadedAttachment
}

/// A performer that can also say how much of a request body has gone.
/// `URLSessionPerformer` is one; a test stub need not be.
public protocol ProgressRequestPerformer: RequestPerformer {
    func upload(_ request: URLRequest, body: Data,
                progress: @escaping @Sendable (Double) -> Void) async throws -> (Data, HTTPURLResponse)
}

/// The upload body's name, as the deck reads it back (`unquote`).
enum AttachmentWire {
    static let filenameHeader = "X-Deck-Filename"

    static func encodedName(_ name: String) -> String {
        name.addingPercentEncoding(withAllowedCharacters: .alphanumerics.union(CharacterSet(charactersIn: "-._~"))) ?? name
    }
}

// MARK: - the message text

public enum AttachmentLines {
    static let imagePrefix = "Attached image: "
    static let filePrefix = "Attached file: "
    /// What `mcp__deck__send_file` writes for a video or audio clip.
    static let videoPrefix = "Attached video: "
    static let audioPrefix = "Attached audio: "

    /// His words, a blank line, then one line per attachment.
    public static func compose(_ text: String, _ uploaded: [UploadedAttachment]) -> String {
        let words = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let lines = uploaded.map { ($0.kind == .image ? imagePrefix : filePrefix) + $0.path }
        guard !lines.isEmpty else { return words }
        let block = lines.joined(separator: "\n")
        return words.isEmpty ? block : words + "\n\n" + block
    }

    /// The bubble's words: the lines it draws as pictures and chips instead are
    /// taken out. Only a line naming an attachment the deck holds (one with a
    /// `url`) goes; a path he typed himself stays.
    public static func visibleText(_ message: Message) -> String {
        let held = Set(message.attachments.filter { $0.url != nil }.map(\.value))
        guard !held.isEmpty else { return message.text }
        let kept = message.text.components(separatedBy: "\n").filter { line in
            for prefix in [imagePrefix, filePrefix, videoPrefix, audioPrefix] where line.hasPrefix(prefix) {
                if held.contains(String(line.dropFirst(prefix.count))) { return false }
            }
            return true
        }
        return kept.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The pictures he sent, drawable from the deck.
    public static func sentImages(_ message: Message) -> [Attachment] {
        message.attachments.filter { $0.kind == .image && $0.url != nil }
    }

    /// Held files shown as chips: a PDF or any other file. A video or audio
    /// clip gets a player instead (`held`).
    public static func sentFiles(_ message: Message) -> [Attachment] {
        message.attachments.filter { $0.kind == .file && $0.url != nil && ($0.media == .pdf || $0.media == .file) }
    }
}

// MARK: - downscale

/// Bytes ready to upload.
public struct PreparedAttachment: Hashable, Sendable {
    public var data: Data
    public var filename: String
    public var mimeType: String
    public var kind: Attachment.Kind

    public init(data: Data, filename: String, mimeType: String, kind: Attachment.Kind) {
        self.data = data
        self.filename = filename
        self.mimeType = mimeType
        self.kind = kind
    }
}

public enum AttachmentPrep {
    /// Where it came from decides whether it may be changed.
    public enum Origin: Sendable {
        /// Photos or the camera: a picture, re-encodable.
        case photo
        /// The clipboard: a picture, re-encodable.
        case pasted
        /// Files: sent byte for byte, whatever it is.
        case file
    }

    public struct Size: Equatable, Sendable {
        public var width: Int
        public var height: Int
        public init(width: Int, height: Int) { self.width = width; self.height = height }
    }

    /// A long edge past this is more than a desk needs to read a screen, and
    /// a 48 MP photo is 10+ MB over a phone line.
    public static let maxLongEdge = 2048
    public static let jpegQuality = 0.85

    /// The shape kept, the long edge at most `maxLongEdge`.
    public static func fitted(width: Int, height: Int, maxLongEdge: Int = maxLongEdge) -> Size {
        let long = max(width, height)
        guard long > maxLongEdge, long > 0 else { return Size(width: width, height: height) }
        let scale = Double(maxLongEdge) / Double(long)
        return Size(width: max(1, Int((Double(width) * scale).rounded())),
                    height: max(1, Int((Double(height) * scale).rounded())))
    }

    /// A picture (photo or pasted) over `maxLongEdge`, or in a type other than
    /// PNG/JPEG/GIF (HEIC), becomes a JPEG at most `maxLongEdge`. A small PNG
    /// screenshot is sent as it is: re-encoding would only blur its text. A
    /// file is never touched.
    public static func prepare(data: Data, filename: String, typeIdentifier: String?, origin: Origin) -> PreparedAttachment {
        let type = typeIdentifier.flatMap(UTType.init) ?? UTType(filenameExtension: (filename as NSString).pathExtension)
        let mime = type?.preferredMIMEType ?? "application/octet-stream"
        let isImage = type?.conforms(to: .image) ?? false
        let asIs = PreparedAttachment(data: data, filename: filename, mimeType: mime, kind: isImage ? .image : .file)
        guard isImage, origin != .file,
              let source = CGImageSourceCreateWithData(data as CFData, nil),
              let props = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
              let width = props[kCGImagePropertyPixelWidth] as? Int,
              let height = props[kCGImagePropertyPixelHeight] as? Int
        else { return asIs }
        let keepable: [UTType] = [.png, .jpeg, .gif]
        let small = max(width, height) <= maxLongEdge
        if small, let type, keepable.contains(type) { return asIs }
        let options: [CFString: Any] = [
            kCGImageSourceCreateThumbnailFromImageAlways: true,
            kCGImageSourceCreateThumbnailWithTransform: true,
            kCGImageSourceThumbnailMaxPixelSize: min(max(width, height), maxLongEdge),
        ]
        guard let image = CGImageSourceCreateThumbnailAtIndex(source, 0, options as CFDictionary) else { return asIs }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil) else { return asIs }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: jpegQuality] as CFDictionary)
        guard CGImageDestinationFinalize(dest) else { return asIs }
        let stem = (filename as NSString).deletingPathExtension
        return PreparedAttachment(data: out as Data, filename: (stem.isEmpty ? "photo" : stem) + ".jpg",
                                  mimeType: "image/jpeg", kind: .image)
    }
}

// MARK: - paste

public enum PasteClassifier {
    public enum Kind: Equatable, Sendable { case image, file, text, nothing }

    /// A file wins (Finder also puts the file's icon there as a picture);
    /// then a picture (copying one in Safari also copies its address); then
    /// words. A bare link is words.
    public static func classify(_ typeIdentifiers: [String]) -> Kind {
        let types = typeIdentifiers.compactMap(UTType.init)
        if types.contains(where: { $0 == .fileURL }) { return .file }
        if types.contains(where: { $0.conforms(to: .image) }) { return .image }
        if types.contains(where: { $0.conforms(to: .text) || $0.conforms(to: .url) }) { return .text }
        if types.contains(where: { $0.conforms(to: .data) || $0.conforms(to: .content) }) { return .file }
        return .nothing
    }
}

// MARK: - the tray above the composer

/// One attachment on its way: drawn as a thumbnail or a chip with a ×.
public struct OutgoingAttachment: Identifiable, Equatable, Sendable {
    public enum State: Equatable, Sendable {
        case uploading(Double)
        case uploaded(UploadedAttachment)
        case failed(String)
    }

    public let id: UUID
    public let prepared: PreparedAttachment
    public var state: State

    public var filename: String { prepared.filename }
    public var isImage: Bool { prepared.kind == .image }
}

/// **What he has attached and not sent yet.** Each upload starts the moment
/// it is added, so by the time he has typed the words it has usually gone.
/// Removing one cancels its upload. Send waits for `isReady`.
@MainActor
public final class AttachmentTray: ObservableObject {
    @Published public private(set) var items: [OutgoingAttachment] = []
    public let threadID: String
    private let uploader: AttachmentUploading
    private var tasks: [UUID: Task<Void, Never>] = [:]

    public init(threadID: String, uploader: AttachmentUploading) {
        self.threadID = threadID
        self.uploader = uploader
    }

    /// True when nothing is uploading and nothing failed: send may go.
    public var isReady: Bool {
        items.allSatisfy { if case .uploaded = $0.state { return true }; return false }
    }

    public var isEmpty: Bool { items.isEmpty }

    /// The uploads, in the order he added them.
    public var uploaded: [UploadedAttachment] {
        items.compactMap { if case .uploaded(let up) = $0.state { return up }; return nil }
    }

    @discardableResult
    public func add(_ prepared: PreparedAttachment) -> UUID {
        let id = UUID()
        items.append(OutgoingAttachment(id: id, prepared: prepared, state: .uploading(0)))
        let uploader = self.uploader
        let threadID = self.threadID
        let report: @Sendable (Double) -> Void = { [weak self] fraction in
            Task { @MainActor in self?.set(id, .uploading(min(max(fraction, 0), 1))) }
        }
        tasks[id] = Task { [weak self] in
            do {
                let up = try await uploader.uploadAttachment(
                    threadID: threadID, data: prepared.data, filename: prepared.filename,
                    mimeType: prepared.mimeType, progress: report)
                self?.set(id, .uploaded(up), final: true)
            } catch is CancellationError {
                // Removed: nothing to say.
            } catch {
                self?.set(id, .failed(Self.why(error)), final: true)
            }
        }
        return id
    }

    public func remove(_ id: UUID) {
        tasks.removeValue(forKey: id)?.cancel()
        items.removeAll { $0.id == id }
    }

    /// After the message went: the tray is empty again.
    public func clear() {
        tasks.values.forEach { $0.cancel() }
        tasks = [:]
        items = []
    }

    private func set(_ id: UUID, _ state: OutgoingAttachment.State, final: Bool = false) {
        guard let index = items.firstIndex(where: { $0.id == id }) else { return }
        // A late progress tick must not undo the finish.
        if case .uploading = state, case .uploading = items[index].state {} else if case .uploading = state { return }
        items[index].state = state
        if final { tasks[id] = nil }
    }

    static func why(_ error: Error) -> String {
        if case DeckError.http(413, _) = error { return "Too big to send (25 MB at most)" }
        if let deck = error as? DeckError { return deck.userFacingText }
        return error.localizedDescription
    }
}

// MARK: - loading from a provider (Photos, paste, drop)

public enum AttachmentLoading {
    /// The provider's own bytes in its own type (a HEIC stays HEIC until the
    /// prep turns it into a JPEG), named after what it suggests. A file URL is
    /// read as that file.
    public static func load(_ provider: NSItemProvider, origin: AttachmentPrep.Origin,
                            fallbackName: String) async -> PreparedAttachment? {
        if provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier),
           let url = await fileURL(provider), let item = file(at: url) {
            return item
        }
        let types = provider.registeredTypeIdentifiers
        let type = types.first { UTType($0)?.conforms(to: .image) == true }
            ?? types.first { UTType($0).map { $0.conforms(to: .data) && !$0.conforms(to: .text) } == true }
        guard let type, let data = await data(provider, type) else { return nil }
        return named(data, type: type, suggested: provider.suggestedName, fallbackName: fallbackName, origin: origin)
    }

    /// Bytes of a known type, given a file name with the right extension.
    public static func named(_ data: Data, type: String, suggested: String?, fallbackName: String,
                             origin: AttachmentPrep.Origin) -> PreparedAttachment {
        let ext = UTType(type)?.preferredFilenameExtension ?? "bin"
        let base = (suggested?.isEmpty == false ? suggested! : fallbackName)
        let name = (base as NSString).pathExtension.isEmpty ? "\(base).\(ext)" : base
        let isImage = UTType(type)?.conforms(to: .image) == true
        return AttachmentPrep.prepare(data: data, filename: name, typeIdentifier: type,
                                      origin: isImage ? origin : .file)
    }

    /// A file from Files, Finder or a drop: byte for byte. A folder is nil.
    public static func file(at url: URL) -> PreparedAttachment? {
        let scoped = url.startAccessingSecurityScopedResource()
        defer { if scoped { url.stopAccessingSecurityScopedResource() } }
        var isDirectory: ObjCBool = false
        guard FileManager.default.fileExists(atPath: url.path, isDirectory: &isDirectory), !isDirectory.boolValue,
              let data = try? Data(contentsOf: url) else { return nil }
        let type = UTType(filenameExtension: url.pathExtension)?.identifier
        return AttachmentPrep.prepare(data: data, filename: url.lastPathComponent, typeIdentifier: type, origin: .file)
    }

    private static func data(_ provider: NSItemProvider, _ type: String) async -> Data? {
        await withCheckedContinuation { continuation in
            _ = provider.loadDataRepresentation(forTypeIdentifier: type) { data, _ in continuation.resume(returning: data) }
        }
    }

    private static func fileURL(_ provider: NSItemProvider) async -> URL? {
        await withCheckedContinuation { continuation in
            _ = provider.loadObject(ofClass: URL.self) { url, _ in continuation.resume(returning: url) }
        }
    }
}

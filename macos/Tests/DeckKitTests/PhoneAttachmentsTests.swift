import XCTest
import ImageIO
import UniformTypeIdentifiers
@testable import DeckKit

/// Owner, 2026-09-30: **"On the phone I can't add or paste screenshots or
/// files."**
///
/// The bytes travel to the box (`POST /v1/threads/{id}/attachments`, raw body,
/// `X-Deck-Filename`), the deck answers the box path, and the message carries
/// `Attached image: <path>` so the desk opens it with Read. These tests pin the
/// wire (it must match `tests/test_owner_attachments.py`), the downscale rule,
/// the paste rule, the composed text, and the tray's remove/cancel state.
final class PhoneAttachmentsTests: XCTestCase {

    // MARK: the wire

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    /// The deck's answer, verbatim in shape from the server test.
    private let answer = Data(#"""
    {"id":"att_0123456789abcdef","name":"Screenshot-1.png","kind":"image","bytes":4,
     "path":"/home/agentdeck/.claude/agent-bus/attachments/att_0123456789abcdef/Screenshot-1.png",
     "url":"/v1/attachments/att_0123456789abcdef/Screenshot-1.png"}
    """#.utf8)

    func testTheUploadIsTheRawFileWithItsTypeAndEncodedName() async throws {
        let performer = StubPerformer()
        performer.defaultBody = answer
        performer.status = 201
        let up = try await client(performer).uploadAttachment(
            threadID: "direct:atlas", data: Data("png!".utf8),
            filename: "Screenshot 1.png", mimeType: "image/png", progress: { _ in })
        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/v1/threads/direct:atlas/attachments")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "image/png")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-Deck-Filename"), "Screenshot%201.png")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sekret")
        XCTAssertEqual(request.httpBody, Data("png!".utf8))
        XCTAssertEqual(up, UploadedAttachment(
            id: "att_0123456789abcdef", name: "Screenshot-1.png", kind: .image, bytes: 4,
            path: "/home/agentdeck/.claude/agent-bus/attachments/att_0123456789abcdef/Screenshot-1.png",
            url: "/v1/attachments/att_0123456789abcdef/Screenshot-1.png"))
    }

    func testARefusedUploadIsTheDecksReason() async throws {
        let performer = StubPerformer()
        performer.status = 413
        performer.defaultBody = Data(#"{"ok":false,"reason":"too_large","detail":"an attachment is at most 25 MB"}"#.utf8)
        do {
            _ = try await client(performer).uploadAttachment(
                threadID: "direct:atlas", data: Data("x".utf8), filename: "a.bin",
                mimeType: "application/octet-stream", progress: { _ in })
            XCTFail("a 413 must throw")
        } catch let error as DeckError {
            XCTAssertEqual(error, DeckError.http(413, reason: "too_large"))
        }
    }

    func testHisBubbleFetchesTheImageWithHisToken() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data("png!".utf8)
        let data = try await client(performer).attachmentData(url: "/v1/attachments/att_0123456789abcdef/Screenshot-1.png")
        XCTAssertEqual(data, Data("png!".utf8))
        XCTAssertEqual(performer.paths, ["/v1/attachments/att_0123456789abcdef/Screenshot-1.png"])
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
    }

    func testADerivedAttachmentCarriesItsURL() throws {
        let json = Data(#"""
        {"id":"m1","author":"owner","role":"owner","text":"x",
         "attachments":[{"kind":"image","value":"/b/att_1/a.png","url":"/v1/attachments/att_1/a.png"},
                        {"kind":"file","value":"~/notes.md"}]}
        """#.utf8)
        let message = try DeckCoding.decoder.decode(Message.self, from: json)
        XCTAssertEqual(message.attachments.map(\.url), ["/v1/attachments/att_1/a.png", nil])
    }

    // MARK: the text the desk reads

    private func uploaded(_ kind: Attachment.Kind, _ name: String) -> UploadedAttachment {
        UploadedAttachment(id: "att_\(name)", name: name, kind: kind, bytes: 1,
                           path: "/box/attachments/att_\(name)/\(name)",
                           url: "/v1/attachments/att_\(name)/\(name)")
    }

    func testTheMessageCarriesOneLinePerAttachmentAfterHisWords() {
        let text = AttachmentLines.compose("what is wrong here?",
                                           [uploaded(.image, "s.png"), uploaded(.file, "r.pdf")])
        XCTAssertEqual(text, """
        what is wrong here?

        Attached image: /box/attachments/att_s.png/s.png
        Attached file: /box/attachments/att_r.pdf/r.pdf
        """)
        XCTAssertEqual(AttachmentLines.compose("  ", [uploaded(.image, "s.png")]),
                       "Attached image: /box/attachments/att_s.png/s.png")
        XCTAssertEqual(AttachmentLines.compose("hi", []), "hi")
    }

    func testHisBubbleHidesTheLinesItDrawsAsPictures() {
        let text = AttachmentLines.compose("look", [uploaded(.image, "s.png"), uploaded(.file, "r.pdf")])
        let message = Message(id: "m", cursor: "m", threadID: "direct:a", author: "owner", role: .owner,
                              sentAt: Date(), text: text, attachments: [
                                Attachment(kind: .image, value: "/box/attachments/att_s.png/s.png",
                                           url: "/v1/attachments/att_s.png/s.png"),
                                Attachment(kind: .file, value: "/box/attachments/att_r.pdf/r.pdf",
                                           url: "/v1/attachments/att_r.pdf/r.pdf"),
                              ])
        XCTAssertEqual(AttachmentLines.visibleText(message), "look")
        XCTAssertEqual(AttachmentLines.sentImages(message).map(\.url), ["/v1/attachments/att_s.png/s.png"])
        XCTAssertEqual(AttachmentLines.sentFiles(message).map(\.value), ["/box/attachments/att_r.pdf/r.pdf"])
        // A path he typed himself, with no url, stays in the words.
        let typed = Message(id: "n", cursor: "n", threadID: "direct:a", author: "owner", role: .owner,
                            sentAt: Date(), text: "Attached image: /tmp/x.png",
                            attachments: [Attachment(kind: .image, value: "/tmp/x.png")])
        XCTAssertEqual(AttachmentLines.visibleText(typed), "Attached image: /tmp/x.png")
    }

    // MARK: downscale

    func testTheLongEdgeIsCappedAndTheShapeKept() {
        XCTAssertEqual(AttachmentPrep.fitted(width: 4032, height: 3024), .init(width: 2048, height: 1536))
        XCTAssertEqual(AttachmentPrep.fitted(width: 1179, height: 2556), .init(width: 945, height: 2048))
        XCTAssertEqual(AttachmentPrep.fitted(width: 800, height: 600), .init(width: 800, height: 600))
        XCTAssertEqual(AttachmentPrep.fitted(width: 2048, height: 10), .init(width: 2048, height: 10))
    }

    private func image(_ width: Int, _ height: Int, type: UTType) throws -> Data {
        let space = CGColorSpaceCreateDeviceRGB()
        let ctx = try XCTUnwrap(CGContext(data: nil, width: width, height: height, bitsPerComponent: 8,
                                          bytesPerRow: 0, space: space,
                                          bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue))
        ctx.setFillColor(CGColor(red: 0.2, green: 0.5, blue: 0.9, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: width, height: height))
        let cg = try XCTUnwrap(ctx.makeImage())
        let out = NSMutableData()
        let dest = try XCTUnwrap(CGImageDestinationCreateWithData(out, type.identifier as CFString, 1, nil))
        CGImageDestinationAddImage(dest, cg, nil)
        XCTAssertTrue(CGImageDestinationFinalize(dest))
        return out as Data
    }

    private func pixels(_ data: Data) -> (Int, Int, String) {
        let src = CGImageSourceCreateWithData(data as CFData, nil)!
        let props = CGImageSourceCopyPropertiesAtIndex(src, 0, nil) as! [CFString: Any]
        return (props[kCGImagePropertyPixelWidth] as! Int, props[kCGImagePropertyPixelHeight] as! Int,
                CGImageSourceGetType(src)! as String)
    }

    func testAHugePhotoBecomesAJPEGAtMost2048() throws {
        let big = try image(4032, 3024, type: .png)
        let ready = AttachmentPrep.prepare(data: big, filename: "IMG_0001.png", typeIdentifier: UTType.png.identifier,
                                           origin: .photo)
        XCTAssertEqual(ready.mimeType, "image/jpeg")
        XCTAssertEqual(ready.filename, "IMG_0001.jpg")
        XCTAssertEqual(ready.kind, .image)
        let (w, h, type) = pixels(ready.data)
        XCTAssertEqual([w, h], [2048, 1536])
        XCTAssertEqual(type, UTType.jpeg.identifier)
    }

    func testAHEICPhotoIsAlwaysAJPEG() throws {
        // Encoding HEIC needs hardware support some CI hosts lack; the rule is
        // about the type, so a small JPEG declared HEIC proves it either way.
        let small = try image(300, 200, type: .jpeg)
        let ready = AttachmentPrep.prepare(data: small, filename: "IMG_0002.HEIC",
                                           typeIdentifier: UTType.heic.identifier, origin: .photo)
        XCTAssertEqual(ready.mimeType, "image/jpeg")
        XCTAssertEqual(ready.filename, "IMG_0002.jpg")
        XCTAssertEqual(pixels(ready.data).2, UTType.jpeg.identifier)
    }

    func testASmallScreenshotIsSentAsItIs() throws {
        let shot = try image(900, 1600, type: .png)
        let ready = AttachmentPrep.prepare(data: shot, filename: "shot.png", typeIdentifier: UTType.png.identifier,
                                           origin: .pasted)
        XCTAssertEqual(ready.data, shot)
        XCTAssertEqual(ready.mimeType, "image/png")
        XCTAssertEqual(ready.filename, "shot.png")
    }

    func testAFileIsNeverTouchedEvenIfItIsAHugeImage() throws {
        let big = try image(4032, 3024, type: .png)
        let ready = AttachmentPrep.prepare(data: big, filename: "plan.png", typeIdentifier: UTType.png.identifier,
                                           origin: .file)
        XCTAssertEqual(ready.data, big)
        XCTAssertEqual(ready.mimeType, "image/png")
        XCTAssertEqual(ready.kind, .image)
        let pdf = AttachmentPrep.prepare(data: Data("%PDF".utf8), filename: "a.pdf",
                                         typeIdentifier: UTType.pdf.identifier, origin: .file)
        XCTAssertEqual(pdf.mimeType, "application/pdf")
        XCTAssertEqual(pdf.kind, .file)
    }

    // MARK: paste

    func testPasteIsAnImageAFileOrText() {
        XCTAssertEqual(PasteClassifier.classify([UTType.png.identifier]), .image)
        XCTAssertEqual(PasteClassifier.classify(["public.heic"]), .image)
        // Copying a picture in Safari puts the picture AND its address there.
        XCTAssertEqual(PasteClassifier.classify([UTType.url.identifier, UTType.jpeg.identifier]), .image)
        XCTAssertEqual(PasteClassifier.classify([UTType.fileURL.identifier]), .file)
        XCTAssertEqual(PasteClassifier.classify([UTType.fileURL.identifier, UTType.tiff.identifier]), .file)
        XCTAssertEqual(PasteClassifier.classify([UTType.pdf.identifier]), .file)
        XCTAssertEqual(PasteClassifier.classify([UTType.utf8PlainText.identifier]), .text)
        XCTAssertEqual(PasteClassifier.classify([UTType.url.identifier]), .text)
        XCTAssertEqual(PasteClassifier.classify(["public.rtf", UTType.utf8PlainText.identifier]), .text)
        XCTAssertEqual(PasteClassifier.classify([]), .nothing)
    }

    // MARK: the tray above the composer

    @MainActor
    func testAnAttachmentUploadsAtOnceAndReportsProgress() async throws {
        let gate = UploadGate()
        let tray = AttachmentTray(threadID: "direct:atlas", uploader: gate)
        let id = tray.add(PreparedAttachment(data: Data("a".utf8), filename: "a.png", mimeType: "image/png", kind: .image))
        await gate.waitForStart(count: 1)
        gate.report(0.5)
        try await Task.sleep(nanoseconds: 50_000_000)
        XCTAssertEqual(tray.items.first?.state, .uploading(0.5))
        XCTAssertFalse(tray.isReady)
        gate.finish(name: "a.png")
        try await waitUntil { tray.isReady }
        XCTAssertEqual(tray.items.map(\.id), [id])
        XCTAssertEqual(tray.uploaded.map(\.name), ["a.png"])
    }

    @MainActor
    func testRemovingOneWhileItUploadsCancelsItAndLeavesTheRest() async throws {
        let gate = UploadGate()
        let tray = AttachmentTray(threadID: "direct:atlas", uploader: gate)
        let first = tray.add(PreparedAttachment(data: Data("a".utf8), filename: "a.png", mimeType: "image/png", kind: .image))
        _ = tray.add(PreparedAttachment(data: Data("b".utf8), filename: "b.pdf", mimeType: "application/pdf", kind: .file))
        await gate.waitForStart(count: 2)
        tray.remove(first)
        try await waitUntil { gate.cancelled.contains("a.png") }
        gate.finish(name: "b.pdf")
        try await waitUntil { tray.isReady }
        XCTAssertEqual(tray.items.map(\.filename), ["b.pdf"])
        XCTAssertEqual(tray.uploaded.map(\.name), ["b.pdf"])
    }

    @MainActor
    func testAFailedUploadSaysWhyAndBlocksSendUntilRemoved() async throws {
        let gate = UploadGate()
        let tray = AttachmentTray(threadID: "direct:atlas", uploader: gate)
        let id = tray.add(PreparedAttachment(data: Data("a".utf8), filename: "a.png", mimeType: "image/png", kind: .image))
        await gate.waitForStart(count: 1)
        gate.fail(name: "a.png")
        try await waitUntil { if case .failed = tray.items.first?.state { return true }; return false }
        XCTAssertFalse(tray.isReady)
        tray.remove(id)
        XCTAssertTrue(tray.items.isEmpty)
        XCTAssertTrue(tray.isReady)
    }

    @MainActor
    func testSendingClearsTheTray() async throws {
        let gate = UploadGate()
        let tray = AttachmentTray(threadID: "direct:atlas", uploader: gate)
        _ = tray.add(PreparedAttachment(data: Data("a".utf8), filename: "a.png", mimeType: "image/png", kind: .image))
        await gate.waitForStart(count: 1)
        gate.finish(name: "a.png")
        try await waitUntil { tray.isReady }
        XCTAssertFalse(tray.items.isEmpty)
        tray.clear()
        XCTAssertTrue(tray.items.isEmpty)
        XCTAssertTrue(tray.uploaded.isEmpty)
    }

    @MainActor
    private func waitUntil(_ condition: @MainActor () -> Bool, timeout: TimeInterval = 2) async throws {
        let end = Date().addingTimeInterval(timeout)
        while !condition() {
            if Date() > end { XCTFail("timed out"); return }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }
}

/// An uploader the test drives by hand: each upload waits for `finish`/`fail`.
final class UploadGate: AttachmentUploading, @unchecked Sendable {
    private let lock = NSLock()
    private var waiting: [String: CheckedContinuation<UploadedAttachment, Error>] = [:]
    private var progress: [String: @Sendable (Double) -> Void] = [:]
    private(set) var started: [String] = []
    private var _cancelled: [String] = []
    var cancelled: [String] { lock.lock(); defer { lock.unlock() }; return _cancelled }

    func uploadAttachment(threadID: String, data: Data, filename: String, mimeType: String,
                          progress report: @escaping @Sendable (Double) -> Void) async throws -> UploadedAttachment {
        try await withTaskCancellationHandler {
            try await withCheckedThrowingContinuation { continuation in
                lock.lock()
                waiting[filename] = continuation
                progress[filename] = report
                started.append(filename)
                lock.unlock()
            }
        } onCancel: {
            lock.lock()
            _cancelled.append(filename)
            let c = waiting.removeValue(forKey: filename)
            lock.unlock()
            c?.resume(throwing: CancellationError())
        }
    }

    func waitForStart(count: Int) async {
        while true {
            lock.lock(); let n = started.count; lock.unlock()
            if n >= count { return }
            try? await Task.sleep(nanoseconds: 5_000_000)
        }
    }

    func report(_ value: Double) {
        lock.lock(); let all = Array(progress.values); lock.unlock()
        all.forEach { $0(value) }
    }

    func finish(name: String) {
        lock.lock(); let c = waiting.removeValue(forKey: name); lock.unlock()
        c?.resume(returning: UploadedAttachment(id: "att_\(name)", name: name, kind: .image, bytes: 1,
                                                path: "/box/\(name)", url: "/v1/attachments/att_x/\(name)"))
    }

    func fail(name: String) {
        lock.lock(); let c = waiting.removeValue(forKey: name); lock.unlock()
        c?.resume(throwing: DeckError.http(413, reason: "too_large"))
    }
}

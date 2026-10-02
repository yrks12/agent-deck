import XCTest
import AVFoundation
@testable import DeckKit

/// Owner, 2026-10-01: **"can the chat send me videos or image files and I
/// will see it there?"** A held video STREAMS through the deck's byte ranges
/// with his token -- AVPlayer cannot add a bearer header itself, and a deck
/// pinned by key (`PinnedTrust`) is not one it would trust -- and a held file
/// lands on disk once for QuickLook, Save and Share.
final class DeckMediaLoaderTests: XCTestCase {

    // MARK: byte ranges with his token

    private func client(_ performer: StubPerformer) -> HTTPDeckClient {
        let store = InMemoryTokenStore()
        try? store.setToken("sekret")
        return HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: store, performer: performer)
    }

    func testARangeIsAskedForWithHisTokenAndTheTotalIsRead() async throws {
        let performer = StubPerformer()
        performer.status = 206
        performer.defaultBody = Data([1, 2])
        performer.headers = ["Content-Range": "bytes 0-1/10240", "Content-Type": "video/mp4"]
        let answer = try await client(performer).attachmentRange(url: "/v1/attachments/att_1/demo.mp4",
                                                                 offset: 0, length: 2)
        let request = try XCTUnwrap(performer.requests.first)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Range"), "bytes=0-1")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer sekret")
        XCTAssertEqual(request.url?.path, "/v1/attachments/att_1/demo.mp4")
        XCTAssertEqual(answer, MediaRange(data: Data([1, 2]), total: 10240, contentType: "video/mp4"))
    }

    func testADeckThatIgnoresRangeStillGivesTheWholeFileAndItsSize() async throws {
        let performer = StubPerformer()
        performer.defaultBody = Data(repeating: 7, count: 50)
        performer.headers = ["Content-Type": "video/mp4"]
        let answer = try await client(performer).attachmentRange(url: "/v1/attachments/a/b.mp4", offset: 0, length: 2)
        XCTAssertEqual(answer.total, 50)
        XCTAssertEqual(answer.data.count, 50)
    }

    func testContentRangeParses() {
        XCTAssertEqual(MediaRange.total(fromContentRange: "bytes 0-1/10240"), 10240)
        XCTAssertEqual(MediaRange.total(fromContentRange: "bytes 5-9/*"), nil)
        XCTAssertEqual(MediaRange.total(fromContentRange: "nonsense"), nil)
    }

    func testABigAskIsCutIntoChunks() {
        XCTAssertEqual(MediaChunks.plan(offset: 0, length: 2, total: 100, chunk: 10), [0..<2])
        XCTAssertEqual(MediaChunks.plan(offset: 95, length: 50, total: 100, chunk: 10), [95..<100])
        XCTAssertEqual(MediaChunks.plan(offset: 0, length: nil, total: 25, chunk: 10), [0..<10, 10..<20, 20..<25])
        XCTAssertEqual(MediaChunks.plan(offset: 100, length: nil, total: 100, chunk: 10), [])
    }

    /// The real thing: AVFoundation opens an MP4 that only exists behind the
    /// fetch closure, asking for byte ranges as it goes -- which is how a
    /// 200 MB video starts in a second and seeks on his phone.
    func testAVFoundationPlaysAVideoThroughTheDecksRanges() async throws {
        let ffmpeg = ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/usr/bin/ffmpeg"]
            .first { FileManager.default.isExecutableFile(atPath: $0) }
        guard let ffmpeg else { throw XCTSkip("no ffmpeg to make a test video") }
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let file = dir.appendingPathComponent("t.mp4")
        let make = Process()
        make.executableURL = URL(fileURLWithPath: ffmpeg)
        make.arguments = ["-v", "error", "-f", "lavfi", "-i", "testsrc=size=160x120:rate=10:duration=2",
                          "-pix_fmt", "yuv420p", "-movflags", "+faststart", file.path]
        try make.run(); make.waitUntilExit()
        let bytes = try Data(contentsOf: file)

        let asked = AskLog()
        let loader = DeckMediaLoader(chunk: 4096) { _, offset, length in
            await asked.add(offset)
            let end = min(bytes.count, Int(offset) + (length ?? bytes.count))
            return MediaRange(data: bytes.subdata(in: Int(offset)..<end), total: Int64(bytes.count),
                              contentType: "video/mp4")
        }
        let asset = loader.asset(for: "/v1/attachments/att_1/t.mp4")
        XCTAssertNotEqual(asset.url.scheme, "https", "a deck url would bypass the token")
        let duration = try await asset.load(.duration)
        XCTAssertEqual(duration.seconds, 2, accuracy: 0.2)
        let tracks = try await asset.loadTracks(withMediaType: .video)
        XCTAssertEqual(tracks.count, 1)
        let offsets = await asked.offsets
        XCTAssertFalse(offsets.isEmpty, "the bytes came through the deck's fetch")
    }

    // MARK: a file on disk for QuickLook, Save and Share

    func testAFileIsFetchedOnceToItsOwnNameInTheCache() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        let files = AttachmentFiles(root: root)
        let fetches = AskLog()
        let fetch: @Sendable (String) async throws -> Data = { _ in await fetches.add(0); return Data("%PDF".utf8) }
        let att = Attachment(kind: .file, value: "/b/att_1/q3.pdf", url: "/v1/attachments/att_0123456789abcdef/q3.pdf")
        let first = try await files.local(att, fetch: fetch)
        let again = try await files.local(att, fetch: fetch)
        XCTAssertEqual(first, again)
        XCTAssertEqual(first.lastPathComponent, "q3.pdf")
        XCTAssertEqual(try Data(contentsOf: first), Data("%PDF".utf8))
        let count = await fetches.offsets.count
        XCTAssertEqual(count, 1, "the second open reads the cache")
    }
}

private actor AskLog {
    var offsets: [Int64] = []
    func add(_ offset: Int64) { offsets.append(offset) }
}

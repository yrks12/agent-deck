import XCTest
@testable import DeckKit

/// Owner, 2026-10-01: **"can the chat send me videos or image files and I
/// will see it there?"**
///
/// A desk now sends him a file with `mcp__deck__send_file`; the deck holds the
/// bytes and the message's attachment says what it is
/// (`tests/test_attachment_media.py` pins the same JSON). These tests pin the
/// app half: the fields decode, an older deck's file is judged by its name,
/// the size reads like a phone says it, and the line naming the file leaves
/// the words while the right player or chip draws it.
final class HeldAttachmentsTests: XCTestCase {

    private let videoJSON = Data(#"""
    {"id":"m1","author":"atlas","role":"agent","text":"the cut\n\nAttached video: /b/attachments/att_1/demo.mp4",
     "attachments":[{"kind":"file","value":"/b/attachments/att_1/demo.mp4",
                     "url":"/v1/attachments/att_1/demo.mp4","name":"demo.mp4",
                     "media":"video","mime":"video/mp4","bytes":12582912,
                     "preview_url":"/v1/attachments/att_1/demo.mp4.poster.jpg"}]}
    """#.utf8)

    // MARK: the wire

    func testAHeldVideoDecodesWithItsMediaSizeAndPoster() throws {
        let message = try DeckCoding.decoder.decode(Message.self, from: videoJSON)
        let video = try XCTUnwrap(message.attachments.first)
        XCTAssertEqual(video.kind, .file)
        XCTAssertEqual(video.media, .video)
        XCTAssertEqual(video.mime, "video/mp4")
        XCTAssertEqual(video.bytes, 12_582_912)
        XCTAssertEqual(video.previewURL, "/v1/attachments/att_1/demo.mp4.poster.jpg")
        XCTAssertEqual(video.displayName, "demo.mp4")
        XCTAssertEqual(video.sizeLabel, "12 MB")
    }

    func testAnOlderDeckWithoutMediaIsJudgedByTheName() {
        XCTAssertEqual(Attachment(kind: .file, value: "/x/a.MOV", url: "/v1/attachments/a/a.MOV").media, .video)
        XCTAssertEqual(Attachment(kind: .file, value: "/x/a.m4a", url: "/u").media, .audio)
        XCTAssertEqual(Attachment(kind: .file, value: "/x/a.pdf", url: "/u").media, .pdf)
        XCTAssertEqual(Attachment(kind: .image, value: "/x/a.png", url: "/u").media, .image)
        XCTAssertEqual(Attachment(kind: .file, value: "/x/a.zip", url: "/u").media, .file)
    }

    func testSizesReadLikeAPhoneSaysThem() {
        func label(_ n: Int) -> String? { Attachment(kind: .file, value: "/a", bytes: n).sizeLabel }
        XCTAssertEqual(label(900), "900 bytes")
        XCTAssertEqual(label(2048), "2 KB")
        XCTAssertEqual(label(1_572_864), "1.5 MB")
        XCTAssertEqual(label(209_715_200), "200 MB")
        XCTAssertNil(Attachment(kind: .file, value: "/a").sizeLabel)
    }

    // MARK: the bubble

    func testTheLineNamingAVideoLeavesTheWordsAndTheVideoIsDrawn() throws {
        let message = try DeckCoding.decoder.decode(Message.self, from: videoJSON)
        XCTAssertEqual(AttachmentLines.visibleText(message), "the cut")
        XCTAssertEqual(AttachmentLines.held(message).map(\.media), [.video])
        XCTAssertEqual(AttachmentLines.sentFiles(message), [], "a video is not a file chip")
        XCTAssertEqual(AttachmentLines.sentImages(message), [])
    }

    func testEveryKindOfHeldFileIsDrawnOnceInOrder() {
        let atts = ["a.png", "b.mp4", "c.m4a", "d.pdf", "e.zip"].map {
            Attachment(kind: $0.hasSuffix(".png") ? .image : .file, value: "/b/\($0)", url: "/v1/attachments/x/\($0)")
        }
        let text = "Attached image: /b/a.png\nAttached video: /b/b.mp4\nAttached audio: /b/c.m4a\n"
            + "Attached file: /b/d.pdf\nAttached file: /b/e.zip"
        let message = Message(id: "m", cursor: "m", threadID: "direct:a", author: "atlas", role: .agent,
                              sentAt: Date(), text: text, attachments: atts + [Attachment(kind: .file, value: "~/x.md")])
        XCTAssertEqual(AttachmentLines.visibleText(message), "")
        XCTAssertEqual(AttachmentLines.sentImages(message).map(\.displayName), ["a.png"])
        XCTAssertEqual(AttachmentLines.held(message).map(\.displayName), ["b.mp4", "c.m4a", "d.pdf", "e.zip"])
        XCTAssertEqual(AttachmentLines.sentFiles(message).map(\.displayName), ["d.pdf", "e.zip"])
    }
}

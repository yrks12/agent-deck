import XCTest
import SwiftUI
import AppKit
@testable import DeckKit
@testable import DeckUI

/// Owner, 2026-10-01: **"can the chat send me videos or image files and I
/// will see it there?"**
///
/// MEASURED before this slice: a video a desk sent drew as a one-line file
/// chip that did nothing when clicked, and an audio clip the same. The bubble
/// must now draw a poster tile with a play button for a video, a player row
/// for audio, and a chip that opens QuickLook for a PDF or any other file --
/// the same view on the Mac and the phone (`HeldAttachmentViews.swift` is
/// compiled into both).
@MainActor
final class HeldAttachmentBubbleTests: XCTestCase {

    private func message(_ attachments: [Attachment], text: String = "the cut") -> Message {
        let lines = attachments.map { "Attached file: \($0.value)" }.joined(separator: "\n")
        return Message(id: "m", cursor: "m", threadID: "direct:atlas", author: "atlas", role: .agent,
                       sentAt: Date(), text: text + (lines.isEmpty ? "" : "\n\n" + lines),
                       attachments: attachments)
    }

    private func held(_ name: String, bytes: Int = 12_582_912) -> Attachment {
        Attachment(kind: .file, value: "/b/attachments/att_1/\(name)",
                   url: "/v1/attachments/att_1/\(name)", bytes: bytes, name: name)
    }

    private func height(_ message: Message) -> CGFloat {
        let host = NSHostingView(rootView: MessageBubble(message: message).frame(width: 600))
        host.layoutSubtreeIfNeeded()
        return host.fittingSize.height
    }

    func testAVideoDrawsAPosterTileNotAOneLineChip() {
        let plain = height(message([]))
        let video = height(message([held("demo.mp4")]))
        XCTAssertGreaterThan(video - plain, 140, "a video must draw a tile he can press play on")
    }

    func testAudioDrawsAPlayerRow() {
        let plain = height(message([]))
        let audio = height(message([held("memo.m4a")]))
        XCTAssertGreaterThan(audio - plain, 30)
        XCTAssertLessThan(audio - plain, 140, "audio is a row, not a video tile")
    }

    func testAPDFIsAChipAndItsLineLeavesTheWords() {
        let pdf = message([held("q3.pdf", bytes: 2048)])
        XCTAssertEqual(AttachmentLines.visibleText(pdf), "the cut")
        XCTAssertEqual(AttachmentLines.held(pdf).map(\.displayName), ["q3.pdf"])
        XCTAssertGreaterThan(height(pdf) - height(message([])), 20)
    }

    func testEveryHeldKindHasItsOwnView() {
        XCTAssertEqual(HeldAttachmentView.style(of: held("a.mp4")), .video)
        XCTAssertEqual(HeldAttachmentView.style(of: held("a.mov")), .video)
        XCTAssertEqual(HeldAttachmentView.style(of: held("a.mp3")), .audio)
        XCTAssertEqual(HeldAttachmentView.style(of: held("a.pdf")), .document)
        XCTAssertEqual(HeldAttachmentView.style(of: held("a.zip")), .document)
    }
}

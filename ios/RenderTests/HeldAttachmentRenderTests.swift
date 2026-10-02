import XCTest
import SwiftUI
import UIKit
import DeckKit
@testable import AgentDeckPhone

/// Owner, 2026-10-01: **"can the chat send me videos or image files and I
/// will see it there?"** MEASURED before: on the phone a video a desk sent was
/// a one-line file chip that did nothing on tap. The phone bubble now draws
/// the shared `HeldAttachmentView` -- a poster tile with a play button for a
/// video, a player row for audio, a chip that opens QuickLook for a file.
/// Nothing here plays a sound (`AudioGate`): nothing is tapped.
@MainActor
final class HeldAttachmentRenderTests: XCTestCase {

    private func message(_ attachments: [Attachment]) -> Message {
        let lines = attachments.map { "Attached file: \($0.value)" }.joined(separator: "\n")
        return Message(id: "m", cursor: "m", threadID: "direct:chief", author: "chief", role: .agent,
                       sentAt: Date(), text: "the cut" + (lines.isEmpty ? "" : "\n\n" + lines),
                       attachments: attachments)
    }

    private func held(_ name: String) -> Attachment {
        Attachment(kind: .file, value: "/b/attachments/att_1/\(name)", url: "/v1/attachments/att_1/\(name)",
                   bytes: 12_582_912, name: name)
    }

    private func height(_ message: Message) -> CGFloat {
        let host = UIHostingController(rootView: Bubble(message: message))
        return host.sizeThatFits(in: CGSize(width: 390, height: 2000)).height
    }

    func testAVideoIsAPlayTileOnThePhone() {
        XCTAssertGreaterThan(height(message([held("demo.mp4")])) - height(message([])), 140)
    }

    func testAudioIsAPlayerRowOnThePhone() {
        let grew = height(message([held("memo.m4a")])) - height(message([]))
        XCTAssertGreaterThan(grew, 30)
        XCTAssertLessThan(grew, 140)
    }

    func testAPDFIsAChipAndItsLineLeavesTheWords() {
        let pdf = message([held("q3.pdf")])
        XCTAssertEqual(AttachmentLines.visibleText(pdf), "the cut")
        XCTAssertGreaterThan(height(pdf) - height(message([])), 20)
    }
}

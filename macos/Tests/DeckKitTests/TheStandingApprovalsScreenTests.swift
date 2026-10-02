import XCTest
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The Standing approvals screen**, one SwiftUI file for the Mac and the
/// iPhone: it draws the model's groups with live usage, one-tap Approve /
/// Dismiss on proposals and Revoke on live ones, add and edit, and the log.
@MainActor
final class TheStandingApprovalsScreenTests: XCTestCase {
    private func source(_ path: String) throws -> String {
        let repo = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: repo.appendingPathComponent(path), encoding: .utf8)
    }

    func testTheScreenOffersEveryActionThroughTheModel() throws {
        let view = try source("macos/Sources/DeckUI/StandingApprovalsView.swift")
        for call in ["model.approve(", "model.revoke(", "model.save(", "model.loadAudit()", "model.load()"] {
            XCTAssertTrue(view.contains(call), "the screen never calls \(call)")
        }
        for word in ["\"Approve\"", "\"Dismiss\"", "\"Revoke\"", "\"Add\"", "\"Edit\"", "\"Log\""] {
            XCTAssertTrue(view.contains(word), "no \(word) control")
        }
        XCTAssertTrue(view.contains("StandingPresentation.usage("), "usage must be DeckKit's words")
    }

    func testTheFileCompilesForTheIPhoneToo() throws {
        let view = try source("macos/Sources/DeckUI/StandingApprovalsView.swift")
        for appKit in ["import AppKit", "NSColor", "nsColor", "NSView"] {
            XCTAssertFalse(view.contains(appKit), "\(appKit) would break the iPhone build")
        }
    }

    func testItLaysOutAgainstTheFixtureDeck() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let probe = NSHostingView(rootView: StandingApprovalsView(client: FixtureDeckClient(), desks: ["atlas"])
            .frame(width: 520, height: 600))
        let window = NSWindow(contentRect: NSRect(x: -40_000, y: -40_000, width: 520, height: 600),
                              styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = probe
        window.orderBack(nil)
        probe.layoutSubtreeIfNeeded()
        XCTAssertGreaterThan(probe.fittingSize.height, 100)
        window.orderOut(nil)
        window.contentView = nil
    }
}

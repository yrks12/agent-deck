import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// D-1 captures for the thread (D2): the conversation pane from the fixture
/// deck, dark, at the reference shots' height. Written to
/// `UITests/Artifacts/overhaul/d2-*.png` only when `DECK_CAPTURE=1`, so an
/// ordinary `swift test` leaves the checked-in pictures alone.
@MainActor
final class ThreadCaptureTests: XCTestCase {

    private func capture(_ name: String, width: CGFloat, height: CGFloat = 962,
                         prepare: (DeckStore) async -> Void = { _ in }) async throws {
        guard ProcessInfo.processInfo.environment["DECK_CAPTURE"] == "1" else {
            throw XCTSkip("set DECK_CAPTURE=1 to write the D2 captures")
        }
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        await prepare(store)
        let size = CGSize(width: width, height: height)
        let (host, window) = RightPaneFixture.host(
            ThreadView(store: store).frame(width: width, height: height), size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: 1_500_000_000)
        let rep = try XCTUnwrap(RightPaneFixture.render(host))
        try RightPaneFixture.saveCapture(rep, named: name)
    }

    /// The conversation column of a 1000pt window with both side panes open.
    func testCaptureTheThread() async throws {
        try await capture("d2-thread.png", width: 440)
    }

    /// The narrowest column the window allows (D-2).
    func testCaptureTheThreadAtTheNarrowestColumn() async throws {
        try await capture("d2-thread-narrow.png", width: 380)
    }

    /// The same thread with the card answered.
    func testCaptureAnAnsweredDecision() async throws {
        try await capture("d2-decision-answered.png", width: 440) { store in
            await self.waitForCard(store)
            guard case .loaded(let screen) = store.thread,
                  let card = screen.messages.compactMap(\.decision).first else { return }
            await store.answer(decision: card, with: card.options[1].value)
        }
    }

    /// Tall enough to hold the whole fixture thread: date row, routine and
    /// deck captions, his grey bubble, the folded peer traffic, NEW, the
    /// decision card and the working character.
    func testCaptureTheWholeThread() async throws {
        try await capture("d2-thread-whole.png", width: 440, height: 1500)
    }

    private func waitForCard(_ store: DeckStore) async {
        let began = Date()
        while Date().timeIntervalSince(began) < 5 {
            if case .loaded(let screen) = store.thread, screen.messages.contains(where: { $0.decision != nil }) {
                return
            }
            try? await Task.sleep(nanoseconds: 20_000_000)
        }
    }
}

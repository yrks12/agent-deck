import XCTest
import SwiftUI
import UIKit
import DeckKit
@testable import AgentDeckPhone

/// **"Checking in with your desks" broke one word per line, beside the title.**
///
/// The owner's screenshot (00:13, iPhone, Agents tab, first load): the
/// loading state was drawn in a ~150pt column to the right of the large
/// "Agents" title — "Check-/ing in/ with/ your/ desks". It must be laid out
/// full-width, under the title, at iPhone width. Measured from the laid-out
/// accessibility frames, not from a picture.
@MainActor
final class RosterLoadingLayoutTests: XCTestCase {

    private var windows: [UIWindow] = []

    override func tearDown() {
        windows.forEach { $0.isHidden = true }
        windows = []
    }

    func testTheFirstLoadStateIsFullWidthUnderTheTitle() throws {
        let store = PhoneStore(preview: .empty, agents: [], attention: [], usage: nil, hasLoaded: false)
        try assertFullWidthUnderTheTitle(store, title: "Checking in with your desks")
    }

    func testTheEmptyRosterIsFullWidthUnderTheTitleToo() throws {
        let store = PhoneStore(preview: .empty, agents: [], attention: [], usage: nil)
        try assertFullWidthUnderTheTitle(store, title: "No desks here yet")
    }

    // MARK: helpers

    private final class Frames { var byTitle: [String: CGRect] = [:] }

    private func assertFullWidthUnderTheTitle(_ store: PhoneStore, title: String,
                                              file: StaticString = #filePath, line: UInt = #line) throws {
        let frames = Frames()
        let view = NavigationStack { RosterView() }
            .environmentObject(store)
            .onPreferenceChange(CharacterStateFrames.self) { frames.byTitle = $0 }
        let window = try host(view)
        let width = window.bounds.width
        let state = try XCTUnwrap(frames.byTitle[title], "'\(title)' is not on screen", file: file, line: line)
        let bar = try XCTUnwrap(navigationBar(in: window), "no navigation bar", file: file, line: line)
        let titleBottom = bar.convert(bar.bounds, to: nil).maxY

        XCTAssertGreaterThanOrEqual(state.width, width - 100,
            "'\(title)' is \(state.width)pt wide on a \(width)pt screen: it wraps a word per line",
            file: file, line: line)
        XCTAssertGreaterThanOrEqual(state.minY, titleBottom - 1,
            "'\(title)' starts at y=\(state.minY), beside the title (which ends at y=\(titleBottom)), not under it",
            file: file, line: line)
        XCTAssertEqual(state.midX, width / 2, accuracy: 8, "'\(title)' is not centred", file: file, line: line)
    }

    private func host<V: View>(_ view: V) throws -> UIWindow {
        let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.first)
        let window = UIWindow(windowScene: scene)
        window.rootViewController = UIHostingController(rootView: view)
        window.makeKeyAndVisible()
        windows.append(window)
        RunLoop.main.run(until: Date().addingTimeInterval(1.0))
        return window
    }

    private func navigationBar(in view: UIView) -> UINavigationBar? {
        if let bar = view as? UINavigationBar { return bar }
        for sub in view.subviews { if let bar = navigationBar(in: sub) { return bar } }
        return nil
    }
}

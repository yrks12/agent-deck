import XCTest
import SwiftUI
import AppKit
import AVFoundation
@testable import DeckKit
@testable import DeckUI

/// Owner, 2026-10-01: the Mac app died ~5 s after he opened a thread holding a
/// video and tapped play. Crash: SIGABRT in `swift_initClassMetadata` for
/// `_AVKit_SwiftUI`'s player view, reached from `NSViewRepresentable._makeView`
/// while the poster was swapped for the player.
///
/// MEASURED: the SwiftPM binary linked `_AVKit_SwiftUI` but not AVKit, so
/// `AVPlayerView` (the superclass of SwiftUI's `VideoPlayerView`) was missing
/// at runtime: "failed to demangle superclass of VideoPlayerView from mangled
/// name 'So12AVPlayerViewC'". This test process links the same DeckUI the app
/// does, so it sees the same gap.
@MainActor
final class HeldVideoPlaysWithoutCrashingTests: XCTestCase {

    func testAVKitIsLinkedSoThePlayerViewExists() {
        XCTAssertNotNil(NSClassFromString("AVPlayerView"),
                        "AVKit is not linked: mounting a video player aborts the app")
    }

    func testTheBubbleDoesNotUseSwiftUIsVideoPlayer() throws {
        let source = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI/HeldAttachmentViews.swift")
        let text = try String(contentsOf: source, encoding: .utf8)
        XCTAssertNil(text.range(of: #"(?<![A-Za-z])VideoPlayer\(player:"#, options: .regularExpression),
                       "SwiftUI's VideoPlayer aborted the Mac app on play; use AVKit's own view")
    }

    /// The exact moment that crashed: the poster is on screen, he taps play,
    /// and the stage swaps the poster for the player inside a laid-out window.
    /// Muted and never started -- this test must make no sound.
    func testTappingPlaySwapsThePosterForAPlayerWithoutAborting() {
        final class Tap: ObservableObject { @Published var player: AVPlayer? }
        struct Host: View {
            @ObservedObject var tap: Tap
            var body: some View {
                HeldVideoStage(player: tap.player, poster: nil, label: "demo.mp4",
                               caption: "demo.mp4 · 12 MB", canPlay: true, play: {})
                    .frame(width: 300, height: 169)
                    .transition(.opacity)
            }
        }
        let tap = Tap()
        let host = NSHostingView(rootView: Host(tap: tap))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 360, height: 240),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.2))

        let player = AVPlayer(url: URL(fileURLWithPath: "/nonexistent/demo.mp4"))
        player.isMuted = true
        player.volume = 0
        withAnimation { tap.player = player }
        host.layoutSubtreeIfNeeded()
        RunLoop.main.run(until: Date().addingTimeInterval(0.5))

        XCTAssertEqual(player.rate, 0, "the test must never start playback")
        XCTAssertNotNil(Self.firstSubview(of: host, named: "AVPlayerView"),
                        "after play the tile must hold AVKit's player view")
        tap.player = nil
        host.layoutSubtreeIfNeeded()
        window.contentView = nil
    }

    private static func firstSubview(of view: NSView, named name: String) -> NSView? {
        if NSStringFromClass(type(of: view)).contains(name) { return view }
        for sub in view.subviews { if let hit = firstSubview(of: sub, named: name) { return hit } }
        return nil
    }
}

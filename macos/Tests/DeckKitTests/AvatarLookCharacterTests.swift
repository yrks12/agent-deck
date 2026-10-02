import XCTest
@testable import DeckKit

/// **Characters, not letters.** The owner: "the ui, its too shit ... give them
/// personality." A desk is drawn as a shape with two dot eyes and a mood, never
/// as initials on a coloured tile, and its eyes move only while it is working.
///
/// Pinned values are FNV-1a results written out by hand, so a change to the
/// derivation fails here and not in a screenshot (acceptance D-3: same desk,
/// same character, every launch).
final class AvatarLookCharacterTests: XCTestCase {

    func testThereAreEightDistinctSilhouettes() {
        XCTAssertEqual(
            Set(AvatarShape.allCases.map(\.rawValue)),
            ["blob", "hexagon", "wedge", "tablet", "pebble", "teardrop", "cloud", "squircle"])
    }

    /// Shape, tint and personality all come off one hash, so all of them are
    /// stable across launches.
    func testTheWholeCharacterIsStableAcrossLaunches() {
        let cos = AvatarLook.forName("cos")
        XCTAssertEqual(cos.shape, .pebble)
        XCTAssertEqual(cos.tintIndex, 1)
        XCTAssertEqual(cos.gaze, .centre)
        XCTAssertEqual(cos.eyes, .wide)
        XCTAssertTrue(cos.blush)

        let chief = AvatarLook.forName("chief")
        XCTAssertEqual(chief.gaze, .right)
        XCTAssertEqual(chief.eyes, .normal)

        let seeker = AvatarLook.forName("seeker")
        XCTAssertEqual(seeker.shape, .wedge)
        XCTAssertEqual(seeker.gaze, .upLeft)
        XCTAssertEqual(seeker.eyes, .close)
        XCTAssertFalse(seeker.blush)
    }

    func testCharactersDifferInMoreThanColour() {
        let names = ["chief", "hemingway", "seeker", "ledger", "larder", "Acme", "Venture", "Umbrella Sales"]
        let looks = names.map(AvatarLook.forName)
        XCTAssertGreaterThanOrEqual(Set(looks.map(\.shape)).count, 5)
        XCTAssertGreaterThanOrEqual(Set(looks.map(\.gaze)).count, 3)
        XCTAssertGreaterThanOrEqual(Set(looks.map(\.eyes)).count, 3)
    }

    // MARK: eyes and CPU

    /// The eyes are the only thing that moves, and a permanently on-screen
    /// sidebar must not burn CPU for nothing. Only a working desk animates, and
    /// Reduce Motion turns even that off.
    func testOnlyAWorkingDeskEverAnimatesItsEyes() {
        for attention in RowAttention.allCases {
            XCTAssertEqual(
                EyeMotion.animates(attention: attention, reduceMotion: false),
                attention == .working, "\(attention)")
            XCTAssertFalse(
                EyeMotion.animates(attention: attention, reduceMotion: true),
                "Reduce Motion must stop the eyes for \(attention)")
        }
    }

    func testTheEyeTimelineTicksSlowlyNotAtTheDisplayRate() {
        XCTAssertGreaterThanOrEqual(EyeMotion.interval, 0.5,
            "eyes redraw once per interval; a display-rate timeline would keep the sidebar hot")
    }

    func testTheEyesLookAroundAndBlinkWithoutLockstep() {
        let poses = (0..<16).map { EyeMotion.pose(step: $0, seed: 3) }
        XCTAssertGreaterThanOrEqual(Set(poses.map { "\($0.dx),\($0.dy)" }).count, 3, "eyes must wander")
        XCTAssertTrue(poses.contains { $0.blink }, "eyes must blink")
        XCTAssertFalse(poses.allSatisfy(\.blink), "eyes must not be shut all the time")
        XCTAssertNotEqual(
            (0..<16).map { EyeMotion.pose(step: $0, seed: 1) },
            (0..<16).map { EyeMotion.pose(step: $0, seed: 2) },
            "two working desks must not move in unison")
        for pose in poses {
            XCTAssertLessThanOrEqual(abs(pose.dx), 1)
            XCTAssertLessThanOrEqual(abs(pose.dy), 1)
        }
    }

    // MARK: the view draws no letters

    func testTheAvatarViewNeverDrawsInitials() throws {
        let source = try Self.source("Sources/DeckUI/AvatarView.swift")
        XCTAssertFalse(source.contains("Text(look.initials)"),
            "AvatarView draws initials. Characters have eyes, not letters.")
    }

    func testTheEyeAnimationIsPeriodicAndNeverAForeverLoop() throws {
        let source = try Self.source("Sources/DeckUI/AvatarView.swift")
        XCTAssertFalse(source.contains("repeatForever"))
        XCTAssertFalse(source.contains("TimelineView(.animation"))
        // Measured: an implicit animation on the eye step made the inspector
        // lay out 1,434 times in 0.5s while a desk was working.
        XCTAssertFalse(source.contains(".animation("),
            "an implicit animation on the eyes drives continuous layout in a hosted Form")
        XCTAssertFalse(source.contains("withAnimation"))
        XCTAssertTrue(source.contains("TimelineView(.periodic"),
            "the working eyes must run on a slow periodic timeline")
        XCTAssertTrue(source.contains("EyeMotion.animates"),
            "the timeline must be gated on the desk actually working")
    }

    private static func source(_ path: String) throws -> String {
        let root = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        return try String(contentsOf: root.appendingPathComponent(path), encoding: .utf8)
    }
}

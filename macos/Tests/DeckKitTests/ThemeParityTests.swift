import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **The Mac and the iPhone draw from one palette.** The owner asked for the
/// Mac to feel exactly like the phone. Two checks make that a fact rather than
/// a promise:
///
/// 1. What the Mac actually resolves for each surface, in the light and the
///    dark appearance, is `DeckTokens`' number — read off the resolved
///    `NSColor`, not off the source.
/// 2. The iPhone target keeps no palette of its own: `PhoneTheme` is the
///    shared `DeckPalette`, and the shared files are compiled into it.
final class ThemeParityTests: XCTestCase {

    private var repo: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
    }

    private func white(_ token: DeckTokens.Grey, dark: Bool) -> CGFloat {
        let color = DeckPalette.greyColor(token)
        var level: CGFloat = -1
        NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance {
            level = color.usingColorSpace(.genericGamma22Gray)?.whiteComponent ?? -1
        }
        return level
    }

    func testTheMacResolvesEverySurfaceToTheSharedNumbersInBothAppearances() {
        let surfaces: [(String, DeckTokens.Grey)] = [
            ("canvas", DeckTokens.canvas), ("ownerBubble", DeckTokens.ownerBubble),
            ("agentBubble", DeckTokens.agentBubble), ("card", DeckTokens.card),
            ("cardStroke", DeckTokens.cardStroke), ("field", DeckTokens.field),
        ]
        for (name, token) in surfaces {
            XCTAssertEqual(white(token, dark: false), CGFloat(token.light), accuracy: 0.005, "\(name) light")
            XCTAssertEqual(white(token, dark: true), CGFloat(token.dark), accuracy: 0.005, "\(name) dark")
        }
    }

    /// The conversation's own colours are the shared ones — the Mac used to
    /// keep `ThreadColors` at 0.26 / 0.165, a different grey from the phone's.
    func testTheMacThreadColoursAreThePalette() {
        func level(_ color: Color, dark: Bool) -> CGFloat {
            var out: CGFloat = -1
            NSAppearance(named: dark ? .darkAqua : .aqua)!.performAsCurrentDrawingAppearance {
                out = NSColor(color).usingColorSpace(.genericGamma22Gray)?.whiteComponent ?? -1
            }
            return out
        }
        for (name, color, token) in [("ownerBubble", ThreadColors.ownerBubble, DeckTokens.ownerBubble),
                                     ("agentBubble", ThreadColors.agentBubble, DeckTokens.agentBubble),
                                     ("composer", ThreadColors.composer, DeckTokens.field)] {
            XCTAssertEqual(level(color, dark: false), CGFloat(token.light), accuracy: 0.005, "\(name) light")
            XCTAssertEqual(level(color, dark: true), CGFloat(token.dark), accuracy: 0.005, "\(name) dark")
        }
        XCTAssertEqual(Theme.bubbleCornerRadius, DeckPalette.bubbleRadius)
    }

    func testThePhoneKeepsNoPaletteOfItsOwn() throws {
        let theme = try String(contentsOf: repo.appendingPathComponent("ios/AgentDeckPhone/PhoneTheme.swift"),
                               encoding: .utf8)
        XCTAssertTrue(theme.contains("typealias PhoneTheme = DeckPalette"), "PhoneTheme is its own palette again")
        XCTAssertFalse(theme.contains("UIColor(white:"), "the phone declares its own greys again")

        let project = try String(contentsOf: repo.appendingPathComponent("ios/project.yml"), encoding: .utf8)
        for shared in ["DeckPalette.swift", "RosterViews.swift", "AvatarView.swift", "Theme.swift"] {
            XCTAssertTrue(project.contains("../macos/Sources/DeckUI/\(shared)"),
                          "\(shared) is not compiled into the iPhone app")
        }
        let roster = try String(contentsOf: repo.appendingPathComponent("ios/AgentDeckPhone/RosterView.swift"),
                                encoding: .utf8)
        for type in ["struct ChiefHero", "struct AgentRowView", "struct UnreadBadge", "struct AttentionBanner"] {
            XCTAssertFalse(roster.contains(type), "the phone re-implements \(type) instead of sharing it")
        }
    }
}

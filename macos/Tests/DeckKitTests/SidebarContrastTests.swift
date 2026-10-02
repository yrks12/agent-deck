import XCTest
import AppKit
@testable import DeckKit
@testable import DeckUI

/// **The one row state the product is managed by, in a colour he can read.**
///
/// "Waiting for you: …" is drawn in orange. System orange is an *accent*
/// colour: fine as a dot, and as **type on a light background** it lands near
/// 2.2:1, which is a WCAG failure on the row that matters most.
///
/// This is arithmetic, not a screenshot: AppKit resolves each colour in each
/// appearance, and the ratio is computed from the sRGB components with the
/// WCAG 2.1 relative-luminance formula. No window, no app, no screen — and,
/// unlike every rendering probe in this repo, no blind spot, because a
/// contrast ratio is a number about two colours and nothing else.
///
/// **The approximation, stated rather than hidden:** the sidebar's real
/// backdrop is a translucent material whose colour depends on the desktop
/// behind the window, so it cannot be resolved here. `textBackgroundColor` is
/// used as the stand-in — white in the light appearance, near-black in the
/// dark one — which is the ordinary case and not a worst case. A material
/// tinted by a bright wallpaper could still be worse in light mode than this
/// says.
final class SidebarContrastTests: XCTestCase {

    /// WCAG 2.1 relative luminance, from sRGB components.
    private func luminance(_ color: NSColor) throws -> Double {
        let srgb = try XCTUnwrap(
            color.usingColorSpace(.sRGB), "\(color) has no sRGB representation")
        func channel(_ raw: CGFloat) -> Double {
            let value = Double(raw)
            return value <= 0.040_45 ? value / 12.92 : pow((value + 0.055) / 1.055, 2.4)
        }
        return 0.2126 * channel(srgb.redComponent)
            + 0.7152 * channel(srgb.greenComponent)
            + 0.0722 * channel(srgb.blueComponent)
    }

    private func ratio(_ foreground: NSColor, on background: NSColor) throws -> Double {
        let first = try luminance(foreground)
        let second = try luminance(background)
        return (max(first, second) + 0.05) / (min(first, second) + 0.05)
    }

    /// Resolves a colour the way the screen will, in one appearance.
    private func resolved(_ color: NSColor, _ name: NSAppearance.Name) throws -> NSColor {
        let appearance = try XCTUnwrap(NSAppearance(named: name))
        var out = color
        appearance.performAsCurrentDrawingAppearance { out = color.usingColorSpace(.sRGB) ?? color }
        return out
    }

    private let appearances: [(String, NSAppearance.Name)] =
        [("light", .aqua), ("dark", .darkAqua)]

    /// **The claim.** 4.5:1 in both appearances, because this is body text.
    func testWaitingForYouIsReadableInBothAppearances() throws {
        var failures: [String] = []
        for (label, name) in appearances {
            let text = try resolved(AttentionPalette.waitingText, name)
            let backdrop = try resolved(.textBackgroundColor, name)
            let measured = try ratio(text, on: backdrop)
            if measured < 4.5 {
                failures.append(String(format: "%@ %.2f:1", label, measured))
            }
        }
        XCTAssertEqual(
            failures, [],
            "\u{201c}Waiting for you\u{201d} is unreadable in: "
            + failures.joined(separator: ", ")
            + " — and it is the one row state he manages by")
    }

    /// The dot is a status indicator, not type: WCAG 1.4.11 asks 3:1. Swept
    /// across every attention that draws one, so a colour added later is held
    /// to the same bar.
    func testEveryStatusDotClearsTheNonTextBar() throws {
        var failures: [String] = []
        for attention in RowAttention.allCases {
            guard let dot = AttentionPalette.dot(for: attention) else { continue }
            for (label, name) in appearances {
                let measured = try ratio(
                    try resolved(dot, name), on: try resolved(.textBackgroundColor, name))
                if measured < 3 {
                    failures.append(String(format: "%@ in %@ %.2f:1", attention.rawValue, label, measured))
                }
            }
        }
        XCTAssertEqual(failures, [], failures.joined(separator: ", "))
    }

    /// **The good signal, and this file's calibration.** The darkening is only
    /// worth having if plain system orange really does fail the same test — a
    /// green here against a bar nothing could fail would prove nothing.
    func testTheBarIsRealBecausePlainSystemOrangeFailsIt() throws {
        let plain = try resolved(.systemOrange, .aqua)
        let white = try resolved(.textBackgroundColor, .aqua)
        let measured = try ratio(plain, on: white)

        XCTAssertLessThan(
            measured, 4.5,
            String(format:
                "plain system orange measured %.2f:1 on a light sidebar, so it would "
                + "have passed and the darkening above is unnecessary — check the "
                + "formula before deleting it", measured))
    }

    /// And the colour must still *be* orange: darkened past recognition it
    /// stops matching the dot beside it and the two read as different states.
    func testTheDarkenedOrangeIsStillOrange() throws {
        let text = try resolved(AttentionPalette.waitingText, .aqua)
        XCTAssertGreaterThan(
            text.redComponent, text.blueComponent,
            "the waiting colour is no longer warm — it will not read as the dot's colour")
        XCTAssertGreaterThan(text.redComponent, text.greenComponent)
    }
}

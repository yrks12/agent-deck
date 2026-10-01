import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif
import DeckKit

/// Deliberately thin. This is a Mac app, so the palette is Apple's: semantic
/// colours and system materials adapt to light, dark, increased contrast and
/// the user's accent colour without a second set of values to maintain.
public enum Theme {

    /// Character colours: saturated, friendly, and distinct at 32pt. Bespoke
    /// rather than system colours because a roster of `.systemBlue` variants
    /// reads as a form, not a cast. Twelve, to match `AvatarLook.tintCount`.
    public static let avatarTints: [Color] = [
        Color(red: 0.55, green: 0.36, blue: 0.95),  // violet
        Color(red: 1.00, green: 0.60, blue: 0.10),  // orange
        Color(red: 0.10, green: 0.72, blue: 0.56),  // mint
        Color(red: 0.98, green: 0.38, blue: 0.22),  // coral
        Color(red: 0.20, green: 0.52, blue: 0.98),  // blue
        Color(red: 0.36, green: 0.33, blue: 0.88),  // indigo
        Color(red: 0.96, green: 0.40, blue: 0.64),  // pink
        Color(red: 0.55, green: 0.38, blue: 0.27),  // cocoa
        Color(red: 0.98, green: 0.78, blue: 0.16),  // sun
        Color(red: 0.42, green: 0.76, blue: 0.26),  // lime
        Color(red: 0.10, green: 0.72, blue: 0.88),  // cyan
        Color(red: 0.86, green: 0.26, blue: 0.36),  // berry
    ]

    public static func avatarTint(_ index: Int) -> Color {
        avatarTints[abs(index) % avatarTints.count]
    }

    /// The phone's bubble radius, shared (`DeckTokens`).
    public static let bubbleCornerRadius = CGFloat(DeckTokens.bubbleRadius)
    public static let rowVerticalPadding: CGFloat = 6
    public static let sidebarWidth: ClosedRange<CGFloat> = 260...360
    public static let inspectorWidth: ClosedRange<CGFloat> = 260...340

    /// Short and relative, the way a chat list reads: "4m", "Yesterday".
    public static func shortTimestamp(_ date: Date, now: Date = Date()) -> String {
        let calendar = Calendar.current
        if calendar.isDateInToday(date) {
            let minutes = Int(now.timeIntervalSince(date) / 60)
            if minutes < 1 { return "now" }
            if minutes < 60 { return "\(minutes)m" }
            return date.formatted(date: .omitted, time: .shortened)
        }
        if calendar.isDateInYesterday(date) { return "Yesterday" }
        return date.formatted(.dateTime.day().month(.abbreviated))
    }

    /// The full form, for VoiceOver and tooltips — "4m" is no use read aloud.
    public static func spokenTimestamp(_ date: Date) -> String {
        date.formatted(date: .abbreviated, time: .shortened)
    }
}

extension Color {
    /// This colour moved toward `other` by `fraction` (sRGB). macOS 14 has no
    /// `Color.mix`, and the characters' gradients need it.
    func mix(with other: Color, by fraction: CGFloat) -> Color {
        #if os(macOS)
        let from = NSColor(self).usingColorSpace(.sRGB) ?? .gray
        let to = NSColor(other).usingColorSpace(.sRGB) ?? .gray
        return Color(nsColor: from.blended(withFraction: fraction, of: to) ?? from)
        #else
        // Shared with the iPhone app, which compiles this file into its own
        // target: the same sRGB blend, done by hand since UIColor has none.
        var a = (r: CGFloat(0), g: CGFloat(0), b: CGFloat(0), o: CGFloat(0))
        var z = (r: CGFloat(0), g: CGFloat(0), b: CGFloat(0), o: CGFloat(0))
        UIColor(self).getRed(&a.r, green: &a.g, blue: &a.b, alpha: &a.o)
        UIColor(other).getRed(&z.r, green: &z.g, blue: &z.b, alpha: &z.o)
        let t = min(max(fraction, 0), 1)
        return Color(.sRGB, red: a.r + (z.r - a.r) * t, green: a.g + (z.g - a.g) * t,
                     blue: a.b + (z.b - a.b) * t, opacity: a.o + (z.o - a.o) * t)
        #endif
    }
}

import SwiftUI
import DeckKit

/// The phone's names for the shared look. The palette, the haptics and the
/// card are `DeckPalette`, `DeckHaptics` and `CardBackground` in
/// `macos/Sources/DeckUI/DeckPalette.swift`, compiled into this target, so the
/// Mac and the iPhone draw from the same file and cannot drift
/// (`ThemeParityTests`).
typealias PhoneTheme = DeckPalette
typealias Haptics = DeckHaptics

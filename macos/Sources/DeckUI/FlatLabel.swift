import SwiftUI

/// **An icon and a sentence, with no alignment for SwiftUI to resolve.**
///
/// This is what a `Label` is for, minus the thing that cost the owner a day.
/// `Label` lines its icon up with its title *inside SwiftUI*, where the source
/// rule in `LayoutSettlesTests` cannot look — that test greps this repo for
/// `firstTextBaseline` and the source has been clean throughout. Four measured
/// runs against his real deck settled it: the app pegs a core within 15 seconds
/// with nobody touching it, and the build that drew these rows as plain
/// `.top`-aligned stacks instead read **0.0-1.8% CPU across 110 seconds**, with
/// zero `FallbackAlignment` / `setFont` / `_invalidateEffectiveFont` frames in
/// the sample where every previous sample had 54-63 of them.
///
/// The mechanism, as far as it can be seen from outside: resolving a text
/// baseline for a child that has none — a glyph, a control, a shape — goes
/// through a hidden `NSTextField` whose font SwiftUI sets on every pass, which
/// invalidates that field's intrinsic size, dirties the window's constraints
/// and schedules the next pass. It never converges.
///
/// **It still reads as one thing.** A `Label` is a single element to VoiceOver
/// that says its title and nothing about the glyph, and so is this: the symbol
/// is hidden, the children are combined, and the words are exactly the words
/// that are drawn. Call sites that need to say something longer than the line
/// on screen put their own `.accessibilityLabel` on the outside, which
/// overrides this one — that is unchanged from `Label` too.
struct FlatLabel: View {
    let text: String
    let symbol: String

    init(_ text: String, systemImage: String) {
        self.text = text
        self.symbol = systemImage
    }

    var body: some View {
        HStack(alignment: .top, spacing: 5) {
            Image(systemName: symbol)
                // A symbol's box sits a touch above the cap height of the line
                // beside it. One point is what the baseline used to buy, and
                // it costs a constant instead of a layout query.
                .padding(.top, 1)
                // Decoration: the sentence beside it already says the thing,
                // and an SF Symbol name read out loud says nothing useful.
                .accessibilityHidden(true)
            Text(text)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(text)
    }
}

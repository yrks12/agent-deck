import SwiftUI
import DeckKit

/// The bar's sentence in the three pieces it is drawn in, so the command can be
/// set in monospace and the clock can tick without re-deriving the rest.
/// `nil` when nothing is running: an idle bar is not drawn at all.
struct MacInUseParts: Equatable {
    /// "Atlas is using your Mac — "
    let lead: String
    /// The command, for exactly one job; `nil` when several are running.
    let command: String?
    /// " · 0:14"
    let trail: String
    let stopLabel: String
    /// The desks whose jobs Stop kills (the plan: "Stop kills that desk's jobs").
    let stopDesks: [String]

    init?(_ state: MacBridgeState, now: Date) {
        guard let line = state.inUseLine(now: now) else { return nil }
        let desks = state.running.map(\.desk).reduce(into: [String]()) { if !$0.contains($1) { $0.append($1) } }
        stopDesks = desks
        if state.running.count == 1 {
            // "<lead>`<cmd>`<trail>" — split on the backticks the state put there.
            let open = line.firstIndex(of: "`")!
            let close = line.lastIndex(of: "`")!
            lead = String(line[..<open])
            command = String(line[line.index(after: open)..<close])
            trail = String(line[line.index(after: close)...])
            stopLabel = "Stop"
        } else {
            let split = line.range(of: " — ")!
            let rest = line[split.upperBound...]
            let dot = rest.range(of: " · ")!
            lead = String(line[..<split.upperBound]) + String(rest[..<dot.lowerBound])
            command = nil
            trail = " · " + String(rest[dot.upperBound...])
            stopLabel = "Stop all"
        }
    }
}

/// **"Atlas is using your Mac — `npm test` · 0:14  [Stop]".** Mounted above
/// the conversation while any job runs, and absent — not hidden, absent —
/// when none does, so it costs the conversation no height at rest.
///
/// The clock is a `TimelineView` that exists only while a job runs: a second
/// per tick, on this bar alone, and nothing at all once the job ends.
public struct MacInUseBar: View {
    let state: MacBridgeState
    /// Stop one desk's jobs, now.
    var onStop: (String) -> Void
    /// A fixed clock for captures; the app passes nothing and the bar ticks.
    var fixedNow: Date?

    public init(state: MacBridgeState, now: Date? = nil, onStop: @escaping (String) -> Void) {
        self.state = state
        self.fixedNow = now
        self.onStop = onStop
    }

    public var body: some View {
        if state.isInUse {
            TimelineView(.periodic(from: .now, by: 1)) { context in
                if let parts = MacInUseParts(state, now: fixedNow ?? context.date) {
                    bar(parts)
                }
            }
        }
    }

    private func bar(_ parts: MacInUseParts) -> some View {
        HStack(spacing: 10) {
            Circle().fill(AttentionPalette.workingDot.swiftUIColor)
                .frame(width: 8, height: 8)
                .accessibilityHidden(true)
            sentence(parts)
                .font(.callout)
                .lineLimit(1)
                .truncationMode(.middle)
            Spacer(minLength: 8)
            Button(parts.stopLabel) { parts.stopDesks.forEach(onStop) }
                .buttonStyle(.deckDestructive)
                .controlSize(.small)
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .frame(maxWidth: .infinity)
        .background(Color.primary.opacity(0.06))
        .overlay(alignment: .bottom) { Divider() }
        .accessibilityElement(children: .contain)
    }

    private func sentence(_ parts: MacInUseParts) -> Text {
        if let command = parts.command {
            return Text(parts.lead) + Text(command).font(.system(.callout, design: .monospaced))
                + Text(parts.trail).foregroundStyle(.secondary)
        }
        return Text(parts.lead) + Text(parts.trail).foregroundStyle(.secondary)
    }
}

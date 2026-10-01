import SwiftUI
import DeckKit

/// **The Claude plan meter** (`GET /v1/usage`). Only drawn when the deck
/// serves the route; a deck that cannot read the plan says so instead of 0%.
///
/// One file for both apps: compiled into DeckUI for the Mac and into the
/// iPhone target (`ios/project.yml`). `compact` is the Mac's short window —
/// the same bars, one line each, without the card's heading.
struct UsageMeterView: View {
    let usage: ClaudeUsage
    var compact = false

    var body: some View {
        if compact { compactBody } else { fullBody }
    }

    private var compactBody: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(spacing: 6) {
                Text("Claude usage").font(.caption2.weight(.semibold)).foregroundStyle(.secondary)
                if let plan = usage.plan, !plan.isEmpty {
                    Text(plan).font(.caption2.weight(.semibold))
                        .padding(.horizontal, 5).padding(.vertical, 1)
                        .background(.quaternary, in: Capsule())
                }
                Spacer(minLength: 0)
                if usage.stale { Text("stale").font(.caption2).foregroundStyle(.secondary) }
            }
            ForEach(usage.available ? Array(usage.windows.prefix(2)) : []) { window in
                HStack(spacing: 8) {
                    Text(window.label).font(.caption2.weight(.medium)).lineLimit(1)
                        .frame(width: 84, alignment: .leading)
                    bar(window).frame(height: 5)
                    Text(window.percent.map { "\(Int($0.rounded()))%" } ?? "—")
                        .font(.caption2.weight(.semibold)).monospacedDigit()
                        .frame(width: 34, alignment: .trailing)
                }
                .accessibilityElement(children: .combine)
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .card(radius: CGFloat(DeckTokens.bannerRadius))
        .help(usage.plan.map { "Claude usage · \($0)" } ?? "Claude usage")
    }

    private func bar(_ window: ClaudeUsage.Window) -> some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.1))
                Capsule().fill(tint(window))
                    .frame(width: max(4, geo.size.width * window.fraction))
            }
        }
    }

    private var fullBody: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Claude usage").font(.subheadline.weight(.semibold))
                if let plan = usage.plan, !plan.isEmpty {
                    Text(plan).font(.caption2.weight(.semibold))
                        .padding(.horizontal, 6).padding(.vertical, 2)
                        .background(.quaternary, in: Capsule())
                }
                Spacer()
                if usage.stale { Text("stale").font(.caption2).foregroundStyle(.secondary) }
            }
            if !usage.available || usage.windows.isEmpty {
                Text(usage.reason.map { "Unavailable (\($0.replacingOccurrences(of: "_", with: " ")))" } ?? "Unavailable")
                    .font(.caption).foregroundStyle(.secondary)
            } else {
                ForEach(usage.windows.prefix(3)) { window in
                    VStack(alignment: .leading, spacing: 5) {
                        HStack {
                            Text(window.label).font(.caption.weight(.medium))
                            Spacer()
                            Text(window.percent.map { "\(Int($0.rounded()))%" } ?? "—")
                                .font(.caption.weight(.semibold)).monospacedDigit()
                            if let reset = window.resetsAt, reset > Date() {
                                Text("· resets \(reset, format: .relative(presentation: .numeric))")
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        bar(window).frame(height: 6)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
        }
        .padding(14)
        .card(radius: 16)
    }

    private func tint(_ w: ClaudeUsage.Window) -> Color {
        if w.fraction >= 0.9 { return .red }
        if w.fraction >= 0.7 { return DeckPalette.waiting }
        return DeckPalette.ink
    }
}

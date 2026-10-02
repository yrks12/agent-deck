import SwiftUI
import DeckKit

/// How an activity row is worded. Pure, so the words are testable.
enum MacActivityCopy {
    static let empty = "Nothing yet. When a desk uses this Mac, it shows up here."

    static func decision(_ row: MacActivityRow) -> String {
        switch row.decision {
        case .ran: return "Ran"
        case .asked: return "Asked you"
        case .granted: return "Allowed"
        case .denied: return "Denied"
        case .refused:
            guard let reason = row.reason, !reason.isEmpty else { return "Refused" }
            return "Refused — " + reason.replacingOccurrences(of: "_", with: " ")
        }
    }

    /// "exit 0 · 2.1 s · fenced in". Empty when the row carries none of it.
    static func detail(_ row: MacActivityRow) -> String {
        var parts: [String] = []
        if let exit = row.exit { parts.append("exit \(exit)") }
        if let ms = row.durationMs { parts.append(duration(ms)) }
        if let fenced = row.sandboxed { parts.append(fenced ? "fenced in" : "not fenced in") }
        return parts.joined(separator: " · ")
    }

    static func duration(_ ms: Int) -> String {
        if ms < 1000 { return "\(ms) ms" }
        if ms < 60_000 { return String(format: "%.1f s", Double(ms) / 1000) }
        return MacBridgeState.elapsed(Double(ms) / 1000)
    }

    /// The clock today, the day and minute before that.
    static func time(_ ts: Double, now: Date = Date(), calendar: Calendar = .current,
                     locale: Locale = .current) -> String {
        let date = Date(timeIntervalSince1970: ts)
        var style: Date.FormatStyle
        if calendar.isDate(date, inSameDayAs: now) {
            style = .dateTime.hour().minute().second()
        } else {
            style = .dateTime.day().month(.abbreviated).hour().minute()
        }
        style.calendar = calendar
        style.timeZone = calendar.timeZone
        style.locale = locale
        return date.formatted(style)
    }

    static func symbol(kind: String) -> String {
        switch kind {
        case "run": return "terminal"
        case "read": return "doc.text"
        case "write": return "square.and.pencil"
        case "list": return "folder"
        case "open": return "arrow.up.forward.app"
        case "screenshot": return "camera.viewfinder"
        default: return "questionmark.circle"
        }
    }
}

/// **What the desks did on this Mac**, newest first: when, which desk, what
/// (the command or path), what he or the policy decided, and how it ended.
/// Click a row for its live or last 4 KB of output. It is the Mac's own record,
/// not the server's copy.
public struct MacActivityView: View {
    let rows: [MacActivityRow]
    let runningIds: Set<String>
    /// The live or last output for a job, read from the Mac's own ring.
    let output: (String) -> String?
    var now: Date

    @State private var selected: String?

    public init(rows: [MacActivityRow], runningIds: Set<String> = [], now: Date = Date(),
                output: @escaping (String) -> String? = { _ in nil }) {
        self.rows = rows
        self.runningIds = runningIds
        self.now = now
        self.output = output
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            VStack(alignment: .leading, spacing: 2) {
                Text("Activity").font(.title2.weight(.semibold))
                Text("What desks did on this Mac. This record stays on this Mac.")
                    .font(.callout).foregroundStyle(.secondary)
            }
            .padding(.horizontal, 20).padding(.top, 18).padding(.bottom, 12)
            Divider()
            if rows.isEmpty {
                Text(MacActivityCopy.empty)
                    .font(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                    .padding(40)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 0) {
                        ForEach(rows, id: \.id) { row in
                            rowView(row)
                            Divider().padding(.leading, 20)
                        }
                    }
                }
            }
        }
        .frame(minWidth: 520, minHeight: 320)
    }

    private func rowView(_ row: MacActivityRow) -> some View {
        let open = selected == row.id
        return VStack(alignment: .leading, spacing: 8) {
            Button { selected = open ? nil : row.id } label: {
                HStack(alignment: .center, spacing: 12) {
                    Text(MacActivityCopy.time(row.ts, now: now))
                        .font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        .frame(width: 92, alignment: .leading)
                    MacDeskFace(desk: row.desk, size: 24)
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 6) {
                            Image(systemName: MacActivityCopy.symbol(kind: row.kind))
                                .foregroundStyle(.secondary).accessibilityHidden(true)
                            Text(row.summary.isEmpty ? row.kind : row.summary)
                                .font(.system(.callout, design: .monospaced))
                                .lineLimit(1).truncationMode(.tail)
                        }
                        let detail = MacActivityCopy.detail(row)
                        if !detail.isEmpty {
                            Text(detail).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                    Spacer(minLength: 8)
                    decisionPill(row)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if open { outputBox(row) }
        }
        .padding(.horizontal, 20).padding(.vertical, 10)
        .background(open ? Color.primary.opacity(0.04) : .clear)
    }

    private func decisionPill(_ row: MacActivityRow) -> some View {
        let live = runningIds.contains(row.jobId) && row.decision == .ran
        return Text(live ? "Running" : MacActivityCopy.decision(row))
            .font(.caption.weight(.medium))
            .padding(.horizontal, 8).padding(.vertical, 3)
            .foregroundStyle(Self.tone(row, live: live))
            .background(Capsule().fill(Self.tone(row, live: live).opacity(0.14)))
    }

    private func outputBox(_ row: MacActivityRow) -> some View {
        let text = output(row.jobId)
        return ScrollView {
            Text(text?.isEmpty == false ? text! : "No output kept for this one.")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(text?.isEmpty == false ? .primary : .secondary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
        }
        .frame(maxHeight: 150)
        .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Color.primary.opacity(0.06)))
    }

    static func tone(_ row: MacActivityRow, live: Bool) -> Color {
        if live { return AttentionPalette.workingDot.swiftUIColor }
        switch row.decision {
        case .ran: return (row.exit ?? 0) == 0 ? .secondary : .red
        case .granted: return .green
        case .asked: return AttentionPalette.waitingText.swiftUIColor
        case .refused, .denied: return .red
        }
    }
}

extension MacActivityRow {
    /// Rows of one job share a `jobId` (asked, then granted, then ran), so the
    /// list identifies a row by the job and what happened.
    var id: String { "\(jobId)-\(decision.rawValue)-\(ts)" }
}

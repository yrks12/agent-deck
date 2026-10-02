import SwiftUI
import DeckKit

/// Says out loud what the stream is doing. Silence on a dropped connection is
/// how a chat app ends up showing a stale transcript that looks current.
public struct ConnectionIndicator: View {
    let state: ConnectionState

    public init(state: ConnectionState) {
        self.state = state
    }

    public var body: some View {
        HStack(spacing: 5) {
            // Nothing here moves. This used to spin for `.connecting` and
            // `.reconnecting`, which looked transient while the live stream
            // delivered nothing and the app never noticed a drop. Now that the
            // stream works, `.reconnecting` is a resting state — the backoff
            // caps at 30s and retries for ever — so a deck that is down left an
            // AppKit animation running in the toolbar indefinitely. That is the
            // same permanent animation that put the owner's Mac at 99% CPU.
            switch state {
            case .live:
                Circle().fill(.green).frame(width: 7, height: 7)
            case .connecting:
                Image(systemName: "antenna.radiowaves.left.and.right")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            case .reconnecting:
                Image(systemName: "antenna.radiowaves.left.and.right.slash")
                    .font(.caption2)
                    .foregroundStyle(.orange)
            case .idle:
                Circle().fill(.secondary).frame(width: 7, height: 7)
            }
            Text(state.label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 3)
        .background(.quaternary.opacity(state.isHealthy ? 0 : 1), in: Capsule())
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("Connection: \(state.label)")
        // Announced rather than only drawn, so it is not a colour-only signal.
        .help("Connection: \(state.label)")
    }
}

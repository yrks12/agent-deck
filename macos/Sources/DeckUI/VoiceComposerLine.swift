import SwiftUI
import DeckKit

/// **The line above the composer that belongs to the mic.** What he is saying
/// while he says it, or — when he cannot be heard — the one plain sentence
/// that says why. Typing is untouched either way.
public struct VoiceComposerLine: View {
    @ObservedObject var session: VoiceSession

    public init(session: VoiceSession) {
        self.session = session
    }

    public var body: some View {
        if let notice = session.notice {
            line(notice, systemImage: "mic.slash")
        } else if session.state == .listening, !session.heard.isEmpty {
            line("“\(session.heard)”", systemImage: "waveform")
        }
    }

    private func line(_ text: String, systemImage: String) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Image(systemName: systemImage)
                .accessibilityHidden(true)
            Text(text)
                .lineLimit(2)
                .fixedSize(horizontal: false, vertical: true)
        }
        .font(.caption)
        .foregroundStyle(.secondary)
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.horizontal, 22)
        .padding(.top, 6)
        .accessibilityElement(children: .combine)
    }
}

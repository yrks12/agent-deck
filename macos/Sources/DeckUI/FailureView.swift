import SwiftUI
import DeckKit

/// The one failure screen. The sidebar and the conversation pane both draw it,
/// which is what stops them describing the same fault in two different ways.
///
/// The action is whatever `FailurePresentation` decided, and `SettingsLink` is
/// the real Settings scene — not a hint to go and find it.
struct FailureView: View {
    let failure: FailurePresentation
    /// Only called for `.retry`. A screen with no retry never gets one.
    let onRetry: () -> Void
    @State private var connecting = false

    var body: some View {
        ContentUnavailableView {
            // Drawn by hand rather than as a `Label`. This screen is a resting
            // state — a deck that is away leaves it up indefinitely — and a
            // `Label` is the construct four live runs against the real deck
            // named as the driver of the 100% CPU spin. The composition is
            // vertical either way, so the picture is the same one.
            VStack(spacing: 8) {
                Image(systemName: failure.symbol)
                    .font(.system(size: 38))
                    .foregroundStyle(.secondary)
                Text(failure.heading)
                    .font(.title2.weight(.semibold))
            }
        } description: {
            Text(failure.detail)
        } actions: {
            switch failure.action {
            case .retry(let title):
                Button(title, action: onRetry)
            case .openSettings(let title):
                SettingsLink {
                    Text(title)
                }
                .help("Open Settings to store a deck API token in your Keychain")
            case .none:
                EmptyView()
            }
            Button("Connect with a pairing code…") { connecting = true }
        }
        .sheet(isPresented: $connecting) {
            ConnectDeckView(onDone: { connecting = false })
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        // Read as one announcement rather than three fragments, so the heading
        // and the sentence under it are never heard apart.
        .accessibilityElement(children: .contain)
        .accessibilityLabel("\(failure.heading). \(failure.detail)")
    }
}

import SwiftUI
import DeckKit

/// The conversation "+" opens, before there is an agent to have it with.
///
/// It is the ordinary thread pane with one message already in it — the
/// question — and the ordinary composer under it. What he types and sends is
/// the hire; on a refusal the reason appears here, in the thread, with his
/// sentence still sitting in the box.
struct PendingThreadView: View {
    @ObservedObject var store: DeckStore
    let pending: PendingThread
    @FocusState private var composerFocused: Bool

    private var typed: Binding<String> {
        Binding(get: { pending.typedText }, set: { store.typePending($0) })
    }

    var body: some View {
        VStack(spacing: 0) {
            // In the pane, not in the titlebar. This said the same words from
            // `.navigationSubtitle`, which is an NSTextField written from a
            // view body — the loop that took a whole core on the conversation
            // pane. There is no stream behind this thread yet, so there is no
            // connection dot to draw beside it.
            ThreadHeader(
                title: PendingThread.rowTitle,
                subtitle: PendingThread.rowPrompt,
                connection: nil
            )
            Divider()
            transcript
            Divider()
            composer
        }
        // Escape leaves without creating anything.
        .onExitCommand { store.cancelPending() }
        .onAppear { composerFocused = true }
    }

    private var transcript: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                openingBubble

                // A still picture, not a spinner. This wait is a POST to the
                // deck on `URLSession.shared`, which sets no
                // `timeoutIntervalForRequest` — so against a deck that is not
                // answering it is on screen for the default **60 seconds**, and
                // a deck that is not answering is exactly the state this app is
                // in when it looks wedged. An indeterminate ProgressView is an
                // AppKit animation: it drives the window's display cycle at the
                // refresh rate and re-measures everything sharing the window on
                // every frame, which is the defect that took a whole core on
                // the thread pane. Same rule as DeskStatusStrip and
                // ConnectionIndicator, both of which already draw their resting
                // states still.
                if let status = pending.statusLine {
                    FlatLabel(status, systemImage: "hourglass")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel(status)
                }

                if let problem = pending.problem {
                    FlatLabel(problem, systemImage: "exclamationmark.triangle")
                        .font(.callout)
                        .foregroundStyle(.red)
                        .fixedSize(horizontal: false, vertical: true)
                        .accessibilityLabel("Could not create this agent. \(problem) What you typed is still below.")
                }

                // The second door, quiet and out of the way of the composer.
                Button(PendingThread.manualDoorTitle) { store.showManualSetup() }
                    .buttonStyle(.link)
                    .help("Fill in a name, title, folder, engine and boss yourself")
                    .padding(.top, 4)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, 16)
            .padding(.vertical, 14)
        }
    }

    private var openingBubble: some View {
        HStack {
            Text(pending.openingLine)
                .font(.body)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 11)
                .padding(.vertical, 8)
                .background(Color(nsColor: .controlBackgroundColor))
                .clipShape(
                    RoundedRectangle(cornerRadius: Theme.bubbleCornerRadius, style: .continuous)
                )
                .textSelection(.enabled)
            Spacer(minLength: 60)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel("New agent asks: \(pending.openingLine)")
    }

    private var composer: some View {
        HStack(alignment: .bottom, spacing: 8) {
            TextField(pending.composerPlaceholder, text: typed, axis: .vertical)
                .textFieldStyle(.plain)
                .lineLimit(1...6)
                .padding(.horizontal, 10)
                .padding(.vertical, 7)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 10))
                .focused($composerFocused)
                .disabled(!pending.composerIsEnabled)
                .onSubmit(submit)
                .accessibilityLabel(pending.composerPlaceholder)

            Button(action: submit) {
                Image(systemName: "arrow.up.circle.fill")
                    .font(.title2)
            }
            .buttonStyle(.plain)
            .keyboardShortcut(.return, modifiers: [])
            .disabled(!pending.canSend)
            .accessibilityLabel("Send and create this agent")
        }
        .padding(12)
        .background(.bar)
    }

    private func submit() {
        guard pending.canSend else { return }
        Task { await store.submitPending() }
    }
}

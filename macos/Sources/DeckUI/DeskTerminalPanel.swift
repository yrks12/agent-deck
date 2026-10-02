import SwiftUI
import DeckKit

/// **"i don't have its file syste, and terminal we need it to be like anydesk."**
///
/// The route has been on the deck the whole time. This is the box he types
/// into: the inspector's terminal for the selected desk, under the screen.
///
/// Four rules, three of them the same ones the screen panel lives under and
/// one of them this panel's own.
///
/// **1. It says what it is, in the panel, before he finds out.** A box with a
/// prompt in it promises a shell, and this is not one — every command is a
/// fresh `bash -lc` on the far side, nothing stays open, and there is a
/// twenty-second ceiling. So `DeskShellModel.natureLine` is drawn above the
/// field rather than left for him to infer from `vim` hanging and dying. It is
/// the empty state too: a panel with nothing run in it yet is a panel
/// explaining itself, never a blank box.
///
/// **2. Nothing here animates while it waits.** No indeterminate
/// `ProgressView`, anywhere — an AppKit animation drives the window's display
/// cycle and re-measures everything sharing that window on every frame, which
/// is how this app once pegged a core. A command that is out says **Running…**
/// in words and the field goes quiet.
///
/// **3. Every outcome is a line, and no outcome is a code.** A command that
/// exited non-zero shows its exit and its stderr — it *ran*, and hiding that
/// behind an error banner would lose the only thing that says why. A command
/// the deck refused shows a sentence. Output the deck cut shows where it was
/// cut. Nothing ends with an empty box.
///
/// **4. The prompt is the truth about where the next command runs.** The route
/// keeps no directory, so `DeskShellModel` does — see its `directoryProbe`.
/// What is drawn beside the field is that model's `cwd`, which is the deck's
/// own `pwd` and never this app's arithmetic.
public struct DeskTerminalPanel: View {
    /// Its own model, deliberately not the store: publishing a transcript onto
    /// `DeckStore` would rebuild every view observing it, and this one is in an
    /// always-open inspector.
    @StateObject private var model: DeskShellModel
    @State private var typed = ""

    /// No workspace parameter: the desk's host workspace does not exist inside
    /// its computer, so the prompt opens at `DeskShellModel.computerHome`.
    public init(desk: String, displayName: String, client: DeskShellClient) {
        _model = StateObject(wrappedValue: Self.makeModel(
            desk: desk, displayName: displayName, client: client))
    }

    /// The one place a panel's model is built, so what it opens at is tested.
    static func makeModel(desk: String, displayName: String,
                          client: DeskShellClient) -> DeskShellModel {
        DeskShellModel(desk: desk, displayName: displayName, client: client,
                       startingCwd: DeskShellModel.computerHome)
    }

    private var canRun: Bool {
        !model.isBusy && !typed.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("\(model.displayName)'s terminal")
                .font(.caption)
                .foregroundStyle(.secondary)
                .accessibilityAddTraits(.isHeader)

            // Drawn always, not only when the transcript is empty. What this
            // panel is does not stop being true after the first command, and
            // the twenty-second ceiling is most useful to read *before* he
            // types the install that will hit it.
            FlatLabel(DeskShellModel.natureLine, systemImage: "info.circle")
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)

            if !model.transcript.isEmpty {
                DeskTerminalTranscript(lines: model.transcript).equatable()
            }

            // A visible caption plus a labelled field: the words are on screen
            // for a sighted glance and attached to the control for VoiceOver.
            // The caption carries the directory, so where the next command
            // runs is never something he has to remember.
            Text(model.isBusy ? "Running…" : "Command, in \(model.cwd)")
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(2)
                // The path can be long and the inspector is 300pt: keep the
                // end of it, which is the part that says where you are.
                .truncationMode(.head)
                .accessibilityHidden(true)

            HStack(spacing: 6) {
                TextField("Command", text: $typed)
                    .deckField()
                    .labelsHidden()
                    .font(.system(.body, design: .monospaced))
                    .disabled(model.isBusy)
                    .onSubmit(run)
                    .accessibilityLabel("Command to run in \(model.cwd)")
                    .accessibilityHint(DeskShellModel.natureLine)

                Button("Run", action: run)
                    .controlSize(.small)
                    .disabled(!canRun)
                    .accessibilityLabel("Run this command on \(model.displayName)'s computer")
            }
        }
        .padding(.vertical, 2)
    }

    private func run() {
        guard canRun else { return }
        let command = typed
        typed = ""
        Task { await model.run(command) }
    }
}

/// The lines, as values. Nothing in here observes anything, and the closures
/// that would break `==` are not in it at all.
struct DeskTerminalTranscript: View, Equatable {
    let lines: [DeskShellLine]

    /// The height the scroller stops growing at. A transcript is unbounded and
    /// the inspector is one column of a window he is allowed to make 560
    /// points tall — a pane that asks for the height of its content is the
    /// fault this repo has already fixed twice.
    static let maxHeight: CGFloat = 200

    static func == (a: DeskTerminalTranscript, b: DeskTerminalTranscript) -> Bool {
        a.lines == b.lines
    }

    var body: some View {
        ScrollViewReader { scroller in
            ScrollView {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(lines) { line in
                        row(line).id(line.id)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(6)
            }
            .frame(maxHeight: Self.maxHeight)
            .background(.quaternary, in: RoundedRectangle(cornerRadius: 6, style: .continuous))
            .onChange(of: lines.count) { _, _ in
                // The newest line is the one he is waiting for. No animation:
                // see this file's rule 2.
                guard let last = lines.last else { return }
                scroller.scrollTo(last.id, anchor: .bottom)
            }
        }
    }

    @ViewBuilder
    private func row(_ line: DeskShellLine) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            // What he typed, echoed where it ran. Per line rather than once at
            // the top, because the prompt moves and a transcript that only
            // shows the current directory misreads its own history.
            Text("\(line.cwd) $ \(line.typed)")
                .font(.system(.caption, design: .monospaced))
                .foregroundStyle(.secondary)
                .textSelection(.enabled)

            if line.refusalSentence != nil {
                // A refusal is a sentence, and it carries a glyph as well as a
                // colour — colour alone is not a signal.
                FlatLabel(line.transcriptBody, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            } else {
                Text(line.transcriptBody)
                    .font(.system(.caption, design: .monospaced))
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }

            if let status = line.statusLine {
                // A command that ran and disagreed. Not an error banner: the
                // output above it is the answer, and this says how it ended.
                Text(status)
                    .font(.caption2)
                    .foregroundStyle(.orange)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        // One element per command, saying the command, how it ended and what
        // it printed. A transcript read out as forty unlabelled fragments is
        // not readable, and the output is the whole reason he ran it.
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(line.spokenLabel) \(line.transcriptBody)")
    }
}

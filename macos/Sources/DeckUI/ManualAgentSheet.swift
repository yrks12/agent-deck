import SwiftUI
import DeckKit

/// The second door, and only the second.
///
/// "+" no longer opens anything modal: it lands him in a conversation and his
/// first sentence is the hire. This sheet is what "Set it up myself" opens, for
/// the times he already knows the name, the engine and the boss — and it keeps
/// its folder field, because pinning a desk to a repo on purpose is exactly
/// what this door is for.
struct ManualAgentSheet: View {
    @ObservedObject var store: DeckStore

    var body: some View {
        ManualAgentForm(store: store, onBack: nil)
    }
}

/// The second door: `POST /v1/agents` with every field named. Unchanged in
/// substance — it is still the only way to pick a name, an engine and a boss
/// yourself, and the deck still refuses it with its own reasons.
struct ManualAgentForm: View {
    @ObservedObject var store: DeckStore
    /// `nil` when the sheet was opened directly, which is now the only way in.
    let onBack: (() -> Void)?
    @Environment(\.dismiss) private var dismiss
    @State private var draft = AgentDraft()
    @State private var hasTried = false
    @FocusState private var nameFocused: Bool

    /// Every desk already on the deck, as candidate bosses.
    private var bosses: [Agent] {
        store.agents.values.sorted { $0.name < $1.name }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("New agent")
                .font(.title3.weight(.semibold))
                .padding(.horizontal, 20)
                .padding(.top, 20)

            Form {
                Section {
                    LabelledField("Name", text: $draft.name, prompt: "scribe")
                        .focused($nameFocused)
                    Text("Lowercase. It is the identity every conversation and org edge here is keyed on, and it cannot be changed later.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                    LabelledField("Title", text: $draft.title, prompt: "Researcher")
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Description").font(.caption).foregroundStyle(.secondary)
                        TextField("Description", text: $draft.detail, axis: .vertical)
                            .textFieldStyle(.roundedBorder)
                            .labelsHidden()
                            .lineLimit(2...5)
                            .accessibilityLabel("Description")
                    }
                }

                Section {
                    LabelledField(
                        "Working directory", text: $draft.directory, prompt: "~/Projects/acme"
                    )
                    Picker("Engine", selection: $draft.engine) {
                        ForEach(AgentDraft.engines, id: \.self) { engine in
                            Text(engine).tag(engine)
                        }
                    }
                    Picker("Reports to", selection: bossBinding) {
                        Text("Nobody — top level").tag("")
                        ForEach(bosses) { agent in
                            Text(agent.displayName).tag(agent.name)
                        }
                    }
                }

                if let problem = shownProblem {
                    Section {
                        FlatLabel(problem, systemImage: "exclamationmark.triangle")
                            .font(.callout)
                            .foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                            .accessibilityLabel("Cannot create this agent. \(problem)")
                    }
                }
            }
            .formStyle(.grouped)

            Divider()

            HStack {
                if let onBack {
                    Button("Back") { onBack() }
                        .buttonStyle(.link)
                        .accessibilityLabel("Back to describing what you want")
                }
                Text("Creating a desk does not start a session at it.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Create") { create() }
                    .keyboardShortcut(.defaultAction)
                    .buttonStyle(.borderedProminent)
                    .disabled(store.isCreating)
            }
            .padding(16)
        }
        .frame(width: 460)
        .onAppear { nameFocused = true }
    }

    private var shownProblem: String? {
        store.createProblem ?? (hasTried ? draft.problem : nil)
    }

    private var bossBinding: Binding<String> {
        Binding(get: { draft.boss ?? "" }, set: { draft.boss = $0.isEmpty ? nil : $0 })
    }

    private func create() {
        hasTried = true
        Task {
            if await store.createAgent(draft) { dismiss() }
        }
    }

    private struct LabelledField: View {
        let caption: String
        @Binding var text: String
        let prompt: String

        init(_ caption: String, text: Binding<String>, prompt: String) {
            self.caption = caption
            self._text = text
            self.prompt = prompt
        }

        var body: some View {
            VStack(alignment: .leading, spacing: 4) {
                Text(caption).font(.caption).foregroundStyle(.secondary)
                TextField(caption, text: $text, prompt: Text(prompt))
                    .textFieldStyle(.roundedBorder)
                    .labelsHidden()
                    .accessibilityLabel(caption)
            }
        }
    }
}

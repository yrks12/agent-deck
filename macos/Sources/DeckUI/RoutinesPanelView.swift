import SwiftUI
import DeckKit

/// Routines for the agent the inspector is showing: what it is asked, when,
/// and whether it is running at all.
///
/// The client states a cron spec and a timezone. `next_run_at` is the deck's
/// answer and is only ever read — it is the value that has to survive a
/// restart, a clock change and a Mac that was asleep.
/// It takes its four facts as values rather than observing `DeckStore`. It is
/// drawn inside the inspector, which is on screen the whole time the app is,
/// and every publish on that store — one per character typed into the
/// conversation composer — would otherwise re-run this body and rebuild every
/// row, however little the routines had changed. Observing here would defeat
/// the `.equatable()` the panel above it hands to SwiftUI: an
/// `@ObservedObject` subscribes from *inside* the subtree, so it invalidates
/// itself whether or not the parent was skipped.
///
/// The store is still here, for the actions the rows perform and for nothing
/// else.
struct RoutinesPanelView: View {
    let agent: Agent
    let routines: [Routine]
    let problem: String?
    let emptyMessage: String?
    let store: DeckStore
    @State private var isAdding = false

    /// A plain stack, not a `Section` in a `Form`: this list lives in the right
    /// pane under the screen, where a grouped form's inset rows and section
    /// chrome would make it look like a settings page again.
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .center, spacing: 6) {
                Text("Routines")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                    .accessibilityAddTraits(.isHeader)
                Spacer(minLength: 0)
                Button {
                    isAdding = true
                } label: {
                    Image(systemName: "plus")
                        .font(.body.weight(.medium))
                        .frame(width: 24, height: 24)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .help("Add a routine")
                .accessibilityLabel("Add a routine")
            }
            .padding(.bottom, 2)

            if let problem {
                FlatLabel(problem, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            } else if let emptyMessage {
                Text(emptyMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            ForEach(routines) { routine in
                RoutineRow(
                    routine: routine,
                    setEnabled: { enabled in
                        Task { await store.setRoutine(id: routine.id, enabled: enabled) }
                    },
                    delete: { Task { await store.deleteRoutine(id: routine.id) } }
                )
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .sheet(isPresented: $isAdding) {
            NewRoutineSheet(store: store, agentName: agent.name)
        }
        .task(id: agent.name) { await store.loadRoutines() }
    }
}

/// One routine, the way the reference product draws it: a clock, what it is,
/// when it fires. Paused is a grey clock and the word; a run that failed is
/// said in red, because a routine that fails every morning must not look like
/// one that works. The switch is still here, small, at the end of the row.
struct RoutineRow: View {
    let routine: Routine
    let setEnabled: (Bool) -> Void
    let delete: () -> Void

    /// The name of the thing, not the whole instruction: "Morning briefing:
    /// what moved overnight" is listed as "Morning briefing", and the full
    /// text stays in the tooltip.
    static func title(of routine: Routine) -> String {
        let prompt = routine.prompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if prompt.isEmpty { return routine.scheduleText }
        let head = prompt.split(separator: ":", maxSplits: 1).first.map(String.init) ?? prompt
        return head.count >= 3 ? head : prompt
    }

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: routine.isEnabled ? "clock.badge.checkmark" : "pause.circle")
                .font(.system(size: 15))
                .foregroundStyle(routine.isEnabled ? Color.green : Color.secondary)
                .frame(width: 18, height: 20)
                .accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 1) {
                // What it does is the name; the schedule is the detail.
                Text(Self.title(of: routine))
                    .font(.callout)
                    .lineLimit(1)
                    .truncationMode(.tail)
                Text(routine.isEnabled ? routine.scheduleText : "Paused - \(routine.scheduleText)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                if routine.runs.first?.ok == false {
                    Text(routine.lastRunText)
                        .font(.caption2)
                        .foregroundStyle(.red)
                        .lineLimit(2)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
            Toggle("", isOn: Binding(get: { routine.isEnabled }, set: setEnabled))
                .labelsHidden()
                .toggleStyle(.switch).tint(DeckPalette.working)
                .controlSize(.mini)
                .accessibilityLabel("\(routine.scheduleText), enabled")
        }
        .padding(.vertical, 5)
        .contentShape(Rectangle())
        .help("\(routine.prompt)\n\(routine.nextRunText)\n\(routine.lastRunText)")
        .contextMenu {
            Button("Delete routine", role: .destructive, action: delete)
        }
        .accessibilityElement(children: .contain)
        .accessibilityLabel(
            "\(routine.scheduleText). \(routine.prompt). \(routine.nextRunText). \(routine.lastRunText)"
        )
    }
}

/// The "+" in the routines panel. It asks for a prompt and a schedule; the
/// timezone comes from this Mac and the next run comes from the deck.
struct NewRoutineSheet: View {
    @ObservedObject var store: DeckStore
    let agentName: String
    @Environment(\.dismiss) private var dismiss
    @State private var draft: RoutineDraft
    @State private var hasTried = false

    init(store: DeckStore, agentName: String) {
        self.store = store
        self.agentName = agentName
        _draft = State(initialValue: RoutineDraft(agentName: agentName))
    }

    /// The shapes the schedule line can actually phrase, offered rather than
    /// asking someone to write cron from memory. The field stays editable.
    private static let presets: [(String, String)] = [
        ("Weekdays at 8:00 AM", "0 8 * * 1-5"),
        ("Every day at 9:00 AM", "0 9 * * *"),
        ("Every 3 hours on weekdays", "0 */3 * * 1-5"),
        ("Every 15 minutes", "*/15 * * * *"),
        ("Weekends at 10:00 AM", "0 10 * * 0,6"),
    ]

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("New routine")
                .font(.title3.weight(.semibold))
                .padding([.horizontal, .top], 20)

            Form {
                Section {
                    VStack(alignment: .leading, spacing: 4) {
                        Text("What it should do").font(.caption).foregroundStyle(.secondary)
                        TextField("What it should do", text: $draft.prompt, axis: .vertical)
                            .deckField()
                            .labelsHidden()
                            .lineLimit(2...6)
                            .accessibilityLabel("What this routine asks the agent to do")
                    }
                }
                Section {
                    Picker("Schedule", selection: $draft.spec) {
                        ForEach(Self.presets, id: \.1) { preset in
                            Text(preset.0).tag(preset.1)
                        }
                        if !Self.presets.contains(where: { $0.1 == draft.spec }) {
                            Text(CronSchedule.describe(draft.spec)).tag(draft.spec)
                        }
                    }
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Cron spec").font(.caption).foregroundStyle(.secondary)
                        TextField("Cron spec", text: $draft.spec)
                            .deckField()
                            .labelsHidden()
                            .font(.system(.body, design: .monospaced))
                            .accessibilityLabel("Cron spec")
                    }
                    LabelledRow(caption: "Runs as", value: CronSchedule.describe(draft.spec))
                    LabelledRow(caption: "Timezone", value: draft.timezone)
                }
                if let problem = shownProblem {
                    Section {
                        FlatLabel(problem, systemImage: "exclamationmark.triangle")
                            .font(.callout)
                            .foregroundStyle(.red)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .formStyle(.grouped)

            Divider()
            HStack {
                Text("The deck works out when it next runs.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Spacer()
                Button("Cancel", role: .cancel) { dismiss() }
                    .keyboardShortcut(.cancelAction)
                Button("Add") { add() }
                    .buttonStyle(.deckPrimary)
                    .keyboardShortcut(.defaultAction)
            }
            .padding(16)
        }
        .frame(width: 420)
    }

    private var shownProblem: String? {
        store.routinesProblem ?? (hasTried ? draft.problem : nil)
    }

    private func add() {
        hasTried = true
        Task {
            if await store.addRoutine(draft) { dismiss() }
        }
    }

    private struct LabelledRow: View {
        let caption: String
        let value: String

        var body: some View {
            HStack {
                Text(caption).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(value).font(.caption)
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("\(caption): \(value)")
        }
    }
}

import SwiftUI
import DeckKit

/// **Calls from desks** — who may ring him, and when (`GET`/`PATCH
/// /v1/calls/settings`). Each change is sent on its own, only what changed;
/// a refusal says why in one line and puts the control back.
struct CallSettingsView: View {
    let client: IncomingCallClient?
    @State private var saved: CallOwnerSettings?
    @State private var draft = CallOwnerSettings.deckDefault
    @State private var problem: String?
    @FocusState private var editingQuiet: Bool

    var body: some View {
        Form {
            if let problem {
                Text(problem).font(.footnote).foregroundStyle(.red).lineLimit(1)
            }
            if saved != nil { controls } else if problem == nil { ProgressView() }
        }
        .navigationTitle("Calls from desks")
        .navigationBarTitleDisplayMode(.inline)
        .task { await load() }
        // A short pause first, so a run of stepper taps is one change.
        .task(id: draft) {
            try? await Task.sleep(nanoseconds: 600_000_000)
            if !Task.isCancelled, !editingQuiet { await save() }
        }
        .onChange(of: editingQuiet) { _, editing in if !editing { Task { await save() } } }
    }

    @ViewBuilder private var controls: some View {
        Section {
            Picker("Who", selection: $draft.who) {
                ForEach(CallOwnerSettings.Who.allCases, id: \.self) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
        } header: { Text("Who may call") } footer: {
            Text("Atlas only: your chief of staff. Any desk: every desk may ring you.")
        }
        Section {
            Picker("When", selection: $draft.when) {
                ForEach(CallOwnerSettings.When.allCases, id: \.self) { Text($0.label).tag($0) }
            }
            .pickerStyle(.segmented)
        } header: { Text("When") } footer: {
            Text("Off: nobody rings. Urgent only: just what a desk marks urgent. Anytime: whenever it's allowed.")
        }
        Section {
            TextField("22:00-08:00", text: $draft.quietHours)
                .focused($editingQuiet)
                .keyboardType(.numbersAndPunctuation)
                .autocorrectionDisabled()
                .submitLabel(.done)
            Stepper("At most \(draft.maxPerDay) a day", value: $draft.maxPerDay, in: 0...20)
        } header: { Text("Quiet hours") } footer: {
            Text("Inside quiet hours (\(draft.tz)) only urgent calls ring.")
        }
        Section {
            Toggle("Call me now", isOn: $draft.callMeNow)
        } footer: {
            Text("For the next hour (or one answered call) any allowed desk may ring, and Atlas is told to call you.")
        }
    }

    private func load() async {
        guard let client else { problem = "This deck can't set calls from desks."; return }
        do {
            let now = try await client.callSettings()
            saved = now
            draft = now
            problem = nil
        } catch {
            problem = Self.line(error)
        }
    }

    private func save() async {
        guard let client, let before = saved else { return }
        let changes = draft.changes(from: before)
        guard !changes.isEmpty else { return }
        let sent = draft
        do {
            let now = try await client.updateCallSettings(changes)
            saved = now
            if draft == sent { draft = now }  // he may have changed more meanwhile
            problem = nil
        } catch {
            problem = Self.line(error)
            if draft == sent { draft = before }
        }
    }

    private static func line(_ error: Error) -> String {
        (error as? DeckError)?.userFacingText ?? error.localizedDescription
    }
}

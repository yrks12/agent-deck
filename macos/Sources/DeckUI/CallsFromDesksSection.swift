import SwiftUI
import DeckKit

/// **Who may call him, and when** — `GET`/`PATCH /v1/calls/settings`. The
/// decisions live here so they are tested without a window: an edit sends
/// only the field he changed and shows what the deck answered; a refusal puts
/// the old value back and says why in one line.
@MainActor
final class CallSettingsModel: ObservableObject {
    enum Phase: Equatable { case loading, unavailable, failed(String), ready }

    static let unavailableLine = "This deck can't take calls from desks yet."

    @Published private(set) var phase: Phase
    @Published private(set) var settings = CallOwnerSettings.deckDefault
    @Published private(set) var problem: String?
    private let client: IncomingCallClient?

    init(client: IncomingCallClient?) {
        self.client = client
        phase = client == nil ? .unavailable : .loading
    }

    func load() async {
        guard let client else { return }
        do {
            settings = try await client.callSettings()
            phase = .ready
        } catch {
            phase = .failed(Self.sentence(error))
        }
    }

    func apply(_ edited: CallOwnerSettings) async {
        guard let client, phase == .ready else { return }
        let old = settings
        let changes = edited.changes(from: old)
        guard !changes.isEmpty else { return }
        settings = edited
        do {
            settings = try await client.updateCallSettings(changes)
            problem = nil
        } catch {
            settings = old
            problem = Self.sentence(error)
        }
    }

    private static func sentence(_ error: Error) -> String {
        let why = (error as? DeckError)?.userFacingText ?? error.localizedDescription
        return why.replacingOccurrences(of: "\n", with: " ")
    }
}

/// The "Calls from desks" section of the settings form.
struct CallsFromDesksSection: View {
    @StateObject private var model: CallSettingsModel
    @State private var quietHours = ""

    init(client: IncomingCallClient?) {
        _model = StateObject(wrappedValue: CallSettingsModel(client: client))
    }

    var body: some View {
        Section("Calls from desks") {
            switch model.phase {
            case .unavailable:
                Text(CallSettingsModel.unavailableLine).foregroundStyle(.secondary)
            case .loading:
                ProgressView().controlSize(.small).accessibilityLabel("Loading call settings")
            case .failed(let why):
                FlatLabel(why, systemImage: "exclamationmark.triangle").foregroundStyle(.red)
            case .ready:
                controls
            }
        }
        .task { await model.load() }
        .onChange(of: model.settings.quietHours, initial: true) { _, value in quietHours = value }
    }

    @ViewBuilder
    private var controls: some View {
        // Labels above the controls, as the rest of this form draws them.
        VStack(alignment: .leading, spacing: 4) {
            Text("Who").font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
            DeckPillPicker(selection: edit(\.who),
                           options: CallOwnerSettings.Who.allCases.map { ($0, $0.label) }, label: "Who may call")
        }
        VStack(alignment: .leading, spacing: 4) {
            Text("When").font(.caption).foregroundStyle(.secondary).accessibilityHidden(true)
            DeckPillPicker(selection: edit(\.when),
                           options: CallOwnerSettings.When.allCases.map { ($0, $0.label) }, label: "When they may call")
        }
        VStack(alignment: .leading, spacing: 4) {
            TextField("Quiet hours", text: $quietHours, prompt: Text("22:00-08:00"))
                .deckField()
                .accessibilityLabel("Quiet hours")
                .onSubmit { edit(\.quietHours).wrappedValue = quietHours }
            Text("Quiet hours: only urgent calls ring; your time zone: \(model.settings.tz)")
                .font(.caption).foregroundStyle(.secondary)
        }
        Stepper("At most \(model.settings.maxPerDay) calls a day", value: edit(\.maxPerDay), in: 0...20)
        VStack(alignment: .leading, spacing: 4) {
            HStack(alignment: .top, spacing: 8) {
                Text("Call me now").accessibilityHidden(true)
                Spacer(minLength: 0)
                Toggle("", isOn: edit(\.callMeNow))
                    .labelsHidden()
                    .toggleStyle(.switch).tint(DeckPalette.working)
                    .accessibilityLabel("Call me now")
            }
            Text("For an hour, any allowed call rings and Atlas is told to call you")
                .font(.caption).foregroundStyle(.secondary)
        }
        if let problem = model.problem {
            FlatLabel(problem, systemImage: "exclamationmark.triangle")
                .font(.callout).foregroundStyle(.red).lineLimit(2)
        }
    }

    /// A binding that PATCHes the one field it writes.
    private func edit<Value>(_ field: WritableKeyPath<CallOwnerSettings, Value>) -> Binding<Value> {
        Binding(
            get: { model.settings[keyPath: field] },
            set: { value in
                var edited = model.settings
                edited[keyPath: field] = value
                Task { await model.apply(edited) }
            })
    }
}

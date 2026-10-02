import SwiftUI
import DeckKit

/// **"Always, up to a limit…"**: the limit to put on a card's standing
/// approval (§23, from a card). One file for both apps. `submit` answers `nil`
/// when the deck made it, or the reason it did not; the sheet stays open on a
/// reason so he can change the limit.
public struct StandingLimitSheet: View {
    let option: StandingOption
    let submit: (StandingFromCard) async -> String?
    let onDone: () -> Void
    @State private var count = ""
    @State private var dollars = ""
    @State private var expires = false
    @State private var expiry = Date().addingTimeInterval(7 * 86_400)
    @State private var problem: String?
    @State private var sending = false

    public init(option: StandingOption, submit: @escaping (StandingFromCard) async -> String?,
                onDone: @escaping () -> Void) {
        self.option = option
        self.submit = submit
        self.onDone = onDone
    }

    public var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button("Cancel", action: onDone).keyboardShortcut(.cancelAction)
                Spacer(minLength: 8)
                Text(StandingPresentation.cardOption).font(.headline).accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                Button("Allow") { Task { await save() } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(sending)
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            Form {
                Section {
                    Text(option.summary).font(.callout)
                    Text(StandingPresentation.floor).font(.caption).foregroundStyle(.secondary)
                }
                Section("Daily limit") {
                    TextField("How many \(StandingPresentation.noun(option.kind, 2)) a day", text: $count)
                    TextField("Dollars a day", text: $dollars)
                }
                Section {
                    HStack {
                        Text("Expires")
                        Spacer(minLength: 8)
                        Toggle("", isOn: $expires).labelsHidden().toggleStyle(.switch).tint(DeckPalette.working)
                            .accessibilityLabel("Expires")
                    }
                    if expires { DatePicker("On", selection: $expiry, in: Date()...) }
                }
                if let problem {
                    Text(problem).font(.callout).foregroundStyle(.orange)
                }
            }
            .formStyle(.grouped)
        }
        .background(DeckPalette.canvas)
        #if os(macOS)
        .frame(minWidth: 420, minHeight: 420)
        #endif
    }

    private func save() async {
        let body = StandingFromCard(
            limits: StandingLimits(countPerDay: StandingPresentation.count(count),
                                   usdPerDay: StandingPresentation.dollars(dollars)),
            expiresAt: expires ? expiry : nil)
        if let local = StandingPresentation.cardProblem(body, kind: option.kind) { problem = local; return }
        sending = true
        problem = await submit(body)
        sending = false
        if problem == nil { onDone() }
    }
}

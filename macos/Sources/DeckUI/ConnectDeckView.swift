import SwiftUI
import DeckKit

/// **Connect this Mac to a deck.** A pairing code (ADK1...) is the front door;
/// the deck's address and token typed by hand is the side door. The logic is
/// `ConnectForm`, the same one the iPhone uses; this is only the drawing.
/// A successful connect saves through `DeckConnection`, which posts
/// `.deckCredentialsChanged` -- the open window then asks the deck again.
public struct ConnectDeckView: View {
    @State private var form: ConnectForm
    @State private var working = false
    @State private var problem: String?
    private let deck: DeckConnection
    private let onDone: () -> Void

    public init(form: ConnectForm = ConnectForm(),
                deck: DeckConnection = DeckConnection(suite: StateSuite()),
                onDone: @escaping () -> Void = {}) {
        _form = State(initialValue: form)
        self.deck = deck
        self.onDone = onDone
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Connect to a deck").font(.title2.weight(.semibold))
            DeckPillPicker(selection: $form.mode,
                           options: ConnectForm.Mode.allCases.map { ($0, $0.rawValue) },
                           label: "How to connect")
            .fixedSize()

            switch form.mode {
            case .code:
                VStack(alignment: .leading, spacing: 4) {
                    TextField("ADK1.…", text: $form.code, axis: .vertical)
                        .lineLimit(3...6)
                        .font(.system(.footnote, design: .monospaced))
                        .deckField()
                        .accessibilityLabel("Pairing code")
                    Text("Paste the ADK1… code from the deck's pairing command. It works once and expires in 15 minutes.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            case .manual:
                VStack(alignment: .leading, spacing: 8) {
                    TextField("Deck address, like your-deck:7789", text: $form.address)
                        .deckField()
                        .accessibilityLabel("Deck address")
                    SecureField("API token", text: $form.token)
                        .deckField()
                        .accessibilityLabel("Deck API token")
                    Text("The token is stored in your login Keychain, never written to a file.")
                        .font(.caption).foregroundStyle(.secondary)
                }
            }

            if let problem {
                FlatLabel(problem, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout).foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }

            HStack {
                Spacer()
                if working { ProgressView().controlSize(.small) }
                Button("Cancel", action: onDone).keyboardShortcut(.cancelAction)
                Button("Connect", action: connect)
                    .buttonStyle(.deckPrimary)
                    .keyboardShortcut(.defaultAction)
                    .disabled(working || !form.isReady)
            }
        }
        .padding(20)
        .frame(width: 440)
    }

    private func connect() {
        working = true
        problem = nil
        let (form, deck) = (form, deck)
        let device = Host.current().localizedName ?? "Mac"
        Task {
            defer { working = false }
            do {
                try await form.submit(on: deck, device: device)
                onDone()
            } catch {
                problem = ConnectForm.problemText(for: error)
            }
        }
    }
}

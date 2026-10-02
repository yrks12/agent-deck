import SwiftUI
import DeckKit

/// **Connect to a deck.** A pairing code (ADK1…, from the deck or the Mac
/// app) is the front door; the deck's address and token by hand is the side
/// door for a deck already reachable over his VPN.
struct ConnectView: View {
    @EnvironmentObject private var store: PhoneStore
    typealias Mode = ConnectForm.Mode

    @State private var mode: Mode = .code
    @State private var code = ""
    @State private var address = ""
    @State private var token = ""
    @State private var working = false
    @State private var problem: String?

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 22) {
                HStack(spacing: -10) {
                    ForEach(["chief", "hemingway", "seeker"], id: \.self) { name in
                        // They look around while it connects and nod off if it fails.
                        AvatarView(look: AvatarLook.forName(name), attention: working ? .working : .quiet,
                                   isDimmed: problem != nil && !working, localAvatarURL: nil, size: 58)
                    }
                }
                .padding(.top, 8)
                VStack(alignment: .leading, spacing: 6) {
                    Text("Your desks, in your pocket.").font(.title2.weight(.bold))
                    Text("Connect this iPhone to your Agent Deck. Your deck must be reachable from here — on your VPN, like your Mac.")
                        .font(.subheadline).foregroundStyle(.secondary)
                }
                Picker("How", selection: $mode) {
                    ForEach(Mode.allCases) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)

                if mode == .code {
                    field(title: "Pairing code", hint: "Paste the ADK1… code from Settings → Pair a device.") {
                        TextField("ADK1.…", text: $code, axis: .vertical)
                            .lineLimit(3...6)
                            .font(.system(.footnote, design: .monospaced))
                    }
                } else {
                    field(title: "Deck address", hint: "Like 10.0.0.1:7789 — the address in your Mac app's settings.") {
                        TextField("10.0.0.1:7788", text: $address)
                            .keyboardType(.URL)
                    }
                    field(title: "API token", hint: "Stored in this iPhone's Keychain, never shown again.") {
                        SecureField("adt_…", text: $token)
                    }
                }

                if let problem {
                    Label(problem, systemImage: "exclamationmark.triangle.fill")
                        .font(.footnote).foregroundStyle(.red)
                }

                Button(action: connect) {
                    HStack {
                        if working { ProgressView().tint(PhoneTheme.canvas) }
                        Text(working ? "Saying hello…" : "Connect").font(.headline)
                    }
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 15)
                    .foregroundStyle(PhoneTheme.canvas)
                    .background(Color.primary, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
                }
                .buttonStyle(.plain)
                .disabled(working || !ready)
                .opacity(ready ? 1 : 0.6)
            }
            .padding(20)
        }
        .scrollDismissesKeyboard(.interactively)
        .background(PhoneTheme.canvas)
        .navigationTitle("Connect")
        .navigationBarTitleDisplayMode(.large)
        .onAppear { if address.isEmpty { address = store.addressHint } }
    }

    private var ready: Bool {
        ConnectForm(mode: mode, code: code, address: address, token: token).isReady
    }

    private func field<Content: View>(title: String, hint: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title).font(.footnote.weight(.semibold)).foregroundStyle(.secondary)
            content()
                .textInputAutocapitalization(.never)
                .autocorrectionDisabled()
                .padding(.horizontal, 14).padding(.vertical, 12)
                .background(PhoneTheme.field, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            Text(hint).font(.caption).foregroundStyle(.secondary)
        }
    }

    private func connect() {
        working = true
        problem = nil
        Task {
            defer { working = false }
            do {
                switch mode {
                case .code: try await store.connect(code: code)
                case .manual: try await store.connect(address: address, token: token)
                }
                Haptics.success()
            } catch {
                problem = ConnectForm.problemText(for: error); Haptics.failure()
            }
        }
    }
}

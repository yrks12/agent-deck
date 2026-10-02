import Foundation

/// **The Connect screen's logic, with no view in it.** The iPhone and the Mac
/// both draw their own form over this, so they agree on when Connect is
/// enabled, what a submit does, and what a failure says.
public struct ConnectForm: Equatable, Sendable {
    public enum Mode: String, CaseIterable, Identifiable, Sendable {
        case code = "Pairing code", manual = "Address & token"
        public var id: String { rawValue }
    }

    public var mode: Mode
    public var code: String
    public var address: String
    public var token: String

    public init(mode: Mode = .code, code: String = "", address: String = "", token: String = "") {
        self.mode = mode
        self.code = code
        self.address = address
        self.token = token
    }

    /// Opens on the pairing-code tab with the launch code filled in (see
    /// `LaunchPairing`). Filling is all it does.
    public init(launchArguments: [String]) {
        self.init(code: LaunchPairing.code(from: launchArguments) ?? "")
    }

    /// Whether Connect may be pressed: the fields of the visible tab only.
    public var isReady: Bool {
        func filled(_ s: String) -> Bool { !s.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        switch mode {
        case .code: return filled(code)
        case .manual: return filled(address) && filled(token)
        }
    }

    /// Redeem the code, or prove the typed address and token with `verify` and
    /// only then keep them. Throws `PairingFailure`, `ManualDeckEntry.Problem`
    /// or whatever `verify` throws; nothing is kept on a throw.
    public func submit(
        on deck: DeckConnection, device: String,
        verify: @Sendable (ManualDeckEntry) async throws -> Void = ConnectForm.probeRoster
    ) async throws {
        switch mode {
        case .code:
            _ = try await deck.pair(rawCode: code, device: device)
        case .manual:
            let entry = try ManualDeckEntry.parse(url: address, token: token)
            try await verify(entry)
            try deck.save(entry)
        }
    }

    /// One read of the roster with the typed token.
    @Sendable public static func probeRoster(_ entry: ManualDeckEntry) async throws {
        let probe = HTTPDeckClient(baseURL: entry.url, tokens: InMemoryTokenStore(token: entry.token))
        _ = try await probe.roster()
    }

    /// The one sentence for a failed submit.
    public static func problemText(for error: Error) -> String {
        switch error {
        case let e as PairingFailure: return e.userFacingText
        case let e as ManualDeckEntry.Problem: return e.userFacingText
        case let e as DeckError: return e.userFacingText
        default: return error.localizedDescription
        }
    }
}

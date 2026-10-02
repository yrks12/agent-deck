import Foundation

/// Why a pairing code was refused. `reason` is the slug the server's tests use
/// too (tests/fixtures/pairing_vectors.json).
public enum PairingCodeError: Error, Equatable, Sendable {
    /// Not a code at all: wrong prefix, not base64url, not JSON.
    case malformed
    /// A code from a newer Agent Deck than this app understands.
    case version
    /// Looks like a code but a required field is missing or unusable
    /// (pin mode without a pin, a url that is not a bare https origin, ...).
    case incomplete

    public var reason: String {
        switch self {
        case .malformed: return "code_malformed"
        case .version: return "code_version"
        case .incomplete: return "code_incomplete"
        }
    }

    public var userFacingText: String {
        switch self {
        case .malformed:
            return "That does not look like a Shaliach pairing code. Copy the whole line that starts with ADK1."
        case .version:
            return "This code is from a newer Shaliach — update the app."
        case .incomplete:
            return "This pairing code is incomplete. Run the pairing command on the server again and copy the whole line."
        }
    }
}

/// K2: `ADK1.<base64url-no-padding(JSON)>`. A one-time, 15-minute secret that is
/// exchanged for a per-device token; it is never stored.
public struct PairingCode: Equatable, Sendable {
    public enum TLSMode: Equatable, Sendable { case ca, pin }

    public let url: URL
    /// The 22-character one-time secret.
    public let code: String
    /// Unix seconds.
    public let expires: Int
    public let tls: TLSMode
    /// `sha256/<base64>`; non-nil exactly when `tls == .pin`.
    public let pin: String?
    public let name: String

    public func isExpired(now: Date = Date()) -> Bool {
        now.timeIntervalSince1970 >= Double(expires)
    }

    public static func parse(_ raw: String) throws -> PairingCode {
        // Terminals wrap; pasting keeps the breaks. No legitimate code holds
        // whitespace, so all of it goes.
        let text = String(raw.unicodeScalars.filter { !CharacterSet.whitespacesAndNewlines.contains($0) })
        guard let dot = text.firstIndex(of: ".") else { throw PairingCodeError.malformed }
        let prefix = text[..<dot].lowercased()
        guard prefix.hasPrefix("adk"), prefix.count > 3,
              let version = Int(prefix.dropFirst(3)) else { throw PairingCodeError.malformed }
        guard version == 1 else { throw PairingCodeError.version }

        guard let data = decodeBase64URL(String(text[text.index(after: dot)...])),
              let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        else { throw PairingCodeError.malformed }

        guard let v = object["v"] as? Int else { throw PairingCodeError.incomplete }
        guard v == 1 else { throw PairingCodeError.version }

        guard let urlText = object["url"] as? String, let url = bareHTTPSOrigin(urlText),
              let code = object["code"] as? String, code.count == 22,
              let exp = object["exp"] as? Int,
              let tlsText = object["tls"] as? String
        else { throw PairingCodeError.incomplete }

        let tls: TLSMode
        var pin: String?
        switch tlsText {
        case "ca":
            tls = .ca
        case "pin":
            tls = .pin
            guard let p = object["pin"] as? String, PinnedTrust.isWellFormed(pin: p) else {
                throw PairingCodeError.incomplete
            }
            pin = p
        default:
            throw PairingCodeError.incomplete
        }
        let name = (object["name"] as? String) ?? ""
        return PairingCode(url: url, code: code, expires: exp, tls: tls, pin: pin, name: String(name.prefix(60)))
    }

    private static func bareHTTPSOrigin(_ text: String) -> URL? {
        guard let comps = URLComponents(string: text), comps.scheme == "https",
              let host = comps.host, !host.isEmpty,
              comps.path.isEmpty || comps.path == "/",
              comps.query == nil, comps.fragment == nil, comps.user == nil
        else { return nil }
        var origin = URLComponents()
        origin.scheme = "https"
        origin.host = host
        origin.port = comps.port
        return origin.url
    }

    private static func decodeBase64URL(_ s: String) -> Data? {
        var b64 = s.replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        b64 += String(repeating: "=", count: (4 - b64.count % 4) % 4)
        return Data(base64Encoded: b64)
    }
}

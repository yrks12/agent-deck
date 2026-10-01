import Foundation

/// **No Mac bridge over plain HTTP on the internet.**
///
/// The bridge carries shell commands and file contents. Over `http://` to a
/// public host anyone on the path can read them or inject a job, so the switch
/// is forced `Off` and the user is told why in one sentence. Plain HTTP is
/// fine where the network itself is private: loopback, RFC 1918, the
/// Tailscale/CGNAT range, `*.ts.net`, IPv6 ULA and `::1` — the owner's own box
/// is `http://10.0.0.1` over WireGuard.
public enum MacTransport {

    public static let refusal =
        "Your deck is reached over plain HTTP on the internet. Mac access needs HTTPS or a private network (WireGuard/Tailscale)."

    public static func allowed(_ url: URL) -> Bool {
        switch url.scheme?.lowercased() {
        case "https": return url.host?.isEmpty == false
        case "http": return url.host.map(isPrivateHost) ?? false
        default: return false
        }
    }

    static func isPrivateHost(_ rawHost: String) -> Bool {
        let host = rawHost.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: "[]"))
        if host == "localhost" || host.hasSuffix(".ts.net") { return true }
        if let v4 = ipv4(host) {
            switch (v4[0], v4[1]) {
            case (127, _), (10, _): return true
            case (172, 16...31): return true
            case (192, 168): return true
            case (100, 64...127): return true
            default: return false
            }
        }
        if host.contains(":") {
            var addr = in6_addr()
            guard inet_pton(AF_INET6, host, &addr) == 1 else { return false }
            let bytes = withUnsafeBytes(of: addr) { Array($0) }
            if bytes == Array(repeating: 0, count: 15) + [1] { return true }   // ::1
            return bytes[0] == 0xfd                                           // fd00::/8
        }
        return false
    }

    /// Four octets for a dotted-quad literal only — `10.1`, `0x0a.0.0.1` and
    /// friends are not accepted, so they cannot be used to dress a public
    /// address up as a private one.
    static func ipv4(_ host: String) -> [Int]? {
        let parts = host.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var octets: [Int] = []
        for p in parts {
            guard !p.isEmpty, p.count <= 3, p.allSatisfy(\.isASCII), p.allSatisfy(\.isNumber),
                  let n = Int(p), n <= 255 else { return nil }
            octets.append(n)
        }
        return octets
    }
}

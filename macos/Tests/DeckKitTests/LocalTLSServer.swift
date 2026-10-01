import Foundation
import Network
import Security

/// A real TLS server on 127.0.0.1 for the pin tests: a recorded self-signed
/// P-256 certificate (CN=127.0.0.1, 100 years) that no system trust store has
/// ever seen, so the only thing that can make a client accept it is the pin.
/// Answers every request with the canned status and body, then closes.
final class LocalTLSServer: @unchecked Sendable {
    /// `sha256/<base64>` of this certificate's SubjectPublicKeyInfo
    /// (openssl x509 -pubkey | openssl pkey -pubin -outform der | sha256).
    static let pin = "sha256/Nq72BuPgdxDIxp7DdLNy78LbCXTHkqMdwbctziEXIiM="
    static let certificateDER = Data(base64Encoded: "MIIBjzCCATagAwIBAgIUBY07p0++m7UkpvU2qRt/B8+HFRQwCgYIKoZIzj0EAwIwFDESMBAGA1UEAwwJMTI3LjAuMC4xMCAXDTI2MDkzMDE5NTA0NFoYDzIxMjYwOTA2MTk1MDQ0WjAUMRIwEAYDVQQDDAkxMjcuMC4wLjEwWTATBgcqhkjOPQIBBggqhkjOPQMBBwNCAARBDm9KJ+O7KMaddXReac6ylKbSh8kTSkdJ105s3Vs9gRYFTdftiLngxKnrDMUJOzp89sYghM/QrIe9+VR0Ctpfo2QwYjAdBgNVHQ4EFgQUfeWLbTQEHSyoaSOMqmQaWhDkxMowHwYDVR0jBBgwFoAUfeWLbTQEHSyoaSOMqmQaWhDkxMowDwYDVR0TAQH/BAUwAwEB/zAPBgNVHREECDAGhwR/AAABMAoGCCqGSM49BAMCA0cAMEQCIEke9jkJzIduv12TdliEk89GX/IbVa9phu2EYRRWM0paAiAm8j3pvPs08k10R313Iupw/QVvQzSLHtstiyxJukPa2Q==")!
    private static let p12 = Data(base64Encoded: "MIIDmgIBAzCCA1gGCSqGSIb3DQEHAaCCA0kEggNFMIIDQTCCAjcGCSqGSIb3DQEHBqCCAigwggIkAgEAMIICHQYJKoZIhvcNAQcBMBwGCiqGSIb3DQEMAQYwDgQIt1n0aVj6pcsCAggAgIIB8NUOApJvQJlHi2N7u6NFCtvQH+S5yXBdwbkQY4kVDYrQv/zdyqiywPe3bOm+3vUTj8/f4HXGZxIJlUN3lfOBsjHFzEEFiaUJTAHMfqWq+tMpueDg6oV4A63pnpOrfNI/3wGk4ax6nCDm82Jaa+EpR50sslplDotJDS4+STpN6Fpl3VzFd1xIhEW8VRD5yGNVu2+KgNvIAwd/VWUgMsXYrnC3uisHfRaADZnGSk79irXNu4EPWRQqNT7iK7tlQwgNUjBxOXguYN7FgYAuB9aM1/zQT1zyPbXCOePq1RK6WNXU3VHT/Zyjb6atlr4+AwXE48W/KYz4biVkPZtwqG/kZ1YOWVJH89Hwqy2M9xj84dwsRiE6T8J09uHark7sFq5Jxp+DTbskNy26pUQJSOAURTi+HkyWXH7nfZ/cBQ2XeqLYvwq3t7Mw1wJ92Ew9VcnJHRi+ke0ZkHjlVUgPojUYKxX6BUaCXCIExTpsMSp7h3LD1MthOnEnHm6oJ9/PYKkPDAmFXqcpi+1k1eOv8K1+qCjcF1wObe0swI3SvVe0eYhLbUn7IDvAnCknRZanNtZZJNzJZxsqYzd6QT/SMA3WdrnaJXsOrV3jMbgcNmyVPTZAcfEaincKZ40fWcgmJD+ukNkq8yJcxCumYFnzRhnbkvMwggECBgkqhkiG9w0BBwGggfQEgfEwge4wgesGCyqGSIb3DQEMCgECoIG0MIGxMBwGCiqGSIb3DQEMAQMwDgQI/m/i4/6cyUYCAggABIGQuWP9f1NMVJHhPjRYEYpn/IizoDRPf3K7YS3NfGfCYhIS9OVfMyJrjA7Lax6MeB5GRdhhZDQw8R9HGC9jzaWcrbmcKIVz6KBWAGTtX58XZDFg2bItKA2AgqUOz8YZgZhOFjsUfOvD95dsM2G3mzGrUhX2gUwNeLpGgz5eGX0VxpA1oMc0ZruCcWbnpV5+3vM5MSUwIwYJKoZIhvcNAQkVMRYEFJnIChWHegwsv4opxHNxsJiEBPNJMDkwITAJBgUrDgMCGgUABBSxXTwch15obKVo25Pa6A3jkj7lCgQQC75xUZSQEokzhb9ERPp6MgICCAA=")!

    private let listener: NWListener
    private let queue = DispatchQueue(label: "LocalTLSServer")
    private let lock = NSLock()
    private var _requests: [String] = []
    var status = 200
    var body = #"{"ok":true}"#
    var headers: [String: String] = [:]
    private(set) var port: UInt16 = 0

    /// Every request head+body received, verbatim.
    var requests: [String] { lock.lock(); defer { lock.unlock() }; return _requests }

    init() throws {
        var items: CFArray?
        let status = SecPKCS12Import(Self.p12 as CFData, [kSecImportExportPassphrase as String: "deckpin"] as CFDictionary, &items)
        guard status == errSecSuccess,
              let first = (items as? [[String: Any]])?.first,
              let identityRef = first[kSecImportItemIdentity as String]
        else { throw NSError(domain: "LocalTLSServer", code: Int(status)) }
        let identity = identityRef as! SecIdentity

        let tls = NWProtocolTLS.Options()
        sec_protocol_options_set_local_identity(tls.securityProtocolOptions, sec_identity_create(identity)!)
        listener = try NWListener(using: NWParameters(tls: tls), on: .any)
    }

    func start() async throws {
        listener.newConnectionHandler = { [weak self] conn in self?.serve(conn) }
        try await withCheckedThrowingContinuation { (c: CheckedContinuation<Void, Error>) in
            let once = Once()
            listener.stateUpdateHandler = { [weak self] state in
                switch state {
                case .ready:
                    self?.port = self?.listener.port?.rawValue ?? 0
                    if once.first() { c.resume() }
                case .failed(let e):
                    if once.first() { c.resume(throwing: e) }
                default: break
                }
            }
            listener.start(queue: queue)
        }
    }

    func stop() { listener.cancel() }

    var url: URL { URL(string: "https://127.0.0.1:\(port)")! }

    private final class Once: @unchecked Sendable {
        private let l = NSLock(); private var done = false
        func first() -> Bool { l.lock(); defer { l.unlock() }; if done { return false }; done = true; return true }
    }

    private func serve(_ conn: NWConnection) {
        conn.start(queue: queue)
        read(conn, acc: Data())
    }

    private func read(_ conn: NWConnection, acc: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 65536) { [weak self] data, _, done, error in
            guard let self else { return }
            var buf = acc
            if let data { buf.append(data) }
            if let text = String(data: buf, encoding: .utf8), let head = text.range(of: "\r\n\r\n") {
                let declared = text.lowercased().range(of: "content-length: ")
                    .flatMap { Int(text[$0.upperBound...].prefix { $0.isNumber }) } ?? 0
                if text[head.upperBound...].utf8.count >= declared {
                    self.lock.lock(); self._requests.append(text); self.lock.unlock()
                    var resp = "HTTP/1.1 \(self.status) X\r\nContent-Type: application/json\r\nContent-Length: \(self.body.utf8.count)\r\nConnection: close\r\n"
                    for (k, v) in self.headers { resp += "\(k): \(v)\r\n" }
                    resp += "\r\n" + self.body
                    conn.send(content: Data(resp.utf8), contentContext: .finalMessage, isComplete: true,
                              completion: .contentProcessed { _ in conn.cancel() })
                    return
                }
            }
            if done || error != nil { conn.cancel(); return }
            self.read(conn, acc: buf)
        }
    }
}

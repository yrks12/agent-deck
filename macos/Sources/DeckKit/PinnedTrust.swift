import Foundation
import CryptoKit
import Security

/// K9 pin mode: trust a server iff SHA-256 of its leaf certificate's
/// SubjectPublicKeyInfo equals the pin from the pairing code. Chain and hostname
/// are deliberately ignored: a self-signed cert on a bare IP is the case this
/// exists for. Only the key is compared, so a re-issued cert with the same key
/// keeps working and a different key never does.
public final class PinnedTrust: NSObject, URLSessionDelegate, @unchecked Sendable {
    public static let prefix = "sha256/"

    private let expected: Data?
    private let lock = NSLock()
    private var rejected = false

    /// `pin` is `sha256/<base64>`.
    public init(pin: String) {
        self.expected = PinnedTrust.digest(fromPin: pin)
        super.init()
    }

    /// True once any connection through this delegate was refused for its key.
    public var didRejectKey: Bool {
        lock.lock(); defer { lock.unlock() }
        return rejected
    }

    public static func isWellFormed(pin: String) -> Bool { digest(fromPin: pin) != nil }

    private static func digest(fromPin pin: String) -> Data? {
        guard pin.hasPrefix(prefix), let d = Data(base64Encoded: String(pin.dropFirst(prefix.count))),
              d.count == 32 else { return nil }
        return d
    }

    /// `sha256/<base64>` of the SPKI inside one DER certificate.
    public static func pin(ofCertificateDER der: Data) -> String? {
        guard let spki = subjectPublicKeyInfo(ofCertificateDER: der) else { return nil }
        return prefix + Data(SHA256.hash(data: spki)).base64EncodedString()
    }

    public func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                           completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
              let trust = challenge.protectionSpace.serverTrust else {
            completionHandler(.performDefaultHandling, nil)
            return
        }
        let leaf = (SecTrustCopyCertificateChain(trust) as? [SecCertificate])?.first
        if let leaf, let expected,
           let spki = PinnedTrust.subjectPublicKeyInfo(ofCertificateDER: SecCertificateCopyData(leaf) as Data),
           Data(SHA256.hash(data: spki)) == expected {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            lock.lock(); rejected = true; lock.unlock()
            completionHandler(.cancelAuthenticationChallenge, nil)
        }
    }

    /// A session for this pin, or the plain system-trust session for CA mode
    /// (`pin` nil or empty). Both the request performer and the SSE transport
    /// take one of these.
    public static func session(pin: String?, configuration: URLSessionConfiguration = .default)
        -> (session: URLSession, trust: PinnedTrust?) {
        guard let pin, !pin.isEmpty else { return (URLSession(configuration: configuration), nil) }
        let trust = PinnedTrust(pin: pin)
        return (URLSession(configuration: configuration, delegate: trust, delegateQueue: nil), trust)
    }

    // MARK: DER

    /// Certificate ::= SEQ { tbsCertificate SEQ { [0] version?, serial, sigAlg,
    /// issuer, validity, subject, subjectPublicKeyInfo, ... }, ... }
    static func subjectPublicKeyInfo(ofCertificateDER der: Data) -> Data? {
        let bytes = [UInt8](der)
        guard let cert = element(bytes, at: 0), cert.tag == 0x30,
              let tbs = element(bytes, at: cert.contentStart), tbs.tag == 0x30 else { return nil }
        var cursor = tbs.contentStart
        if let first = element(bytes, at: cursor), first.tag == 0xA0 { cursor = first.end }
        for _ in 0..<5 { // serial, sigAlg, issuer, validity, subject
            guard let e = element(bytes, at: cursor) else { return nil }
            cursor = e.end
        }
        guard let spki = element(bytes, at: cursor), spki.tag == 0x30, spki.end <= bytes.count else { return nil }
        return Data(bytes[cursor..<spki.end])
    }

    private struct Element { let tag: UInt8; let contentStart: Int; let end: Int }

    private static func element(_ b: [UInt8], at i: Int) -> Element? {
        guard i + 2 <= b.count else { return nil }
        let tag = b[i]
        var length = Int(b[i + 1])
        var start = i + 2
        if length & 0x80 != 0 {
            let n = length & 0x7F
            guard n >= 1, n <= 4, start + n <= b.count else { return nil }
            length = b[start..<start + n].reduce(0) { $0 << 8 | Int($1) }
            start += n
        }
        guard start + length <= b.count else { return nil }
        return Element(tag: tag, contentStart: start, end: start + length)
    }
}

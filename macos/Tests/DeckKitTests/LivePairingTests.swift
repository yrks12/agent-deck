import XCTest
import CryptoKit
import Security
import Foundation

/// **Live pairing against a real box** (easy-setup plan, L1 step "pin path").
///
/// Skipped unless `DECK_PAIR_CODE` is set, so `swift test` stays hermetic. L1 sets it from
/// `deckctl pair --json`:
///
/// ```console
/// $ DECK_PAIR_CODE=ADK1.… DECK_STATE_SUITE=livepair swift test --package-path macos \
///     --filter LivePairingTests
/// ```
///
/// Deliberately self-contained (raw URLSession + a local SPKI pin check) so it compiles before
/// the F1 connection core exists and so it is an *independent* check of the wire contract K2/K3
/// rather than of our own client. When F1 lands, extend it to also drive `DeckConnection`.
///
/// Asserts: the code parses (incl. `tls=pin`), redeems at `POST /v1/pair` for a device token,
/// `GET /v1/agents` answers 200 through the pinned session, a **second** redeem is 409
/// `pair_used`, and a **wrong pin** refuses the connection.
final class LivePairingTests: XCTestCase {

    private struct Code { let url: URL; let code: String; let tls: String; let pin: String? }

    private func requiredCode() throws -> Code {
        // XCTUnwrap records a failure before it throws, so a catch-and-skip
        // still failed the run on every Mac without a code. Skip first.
        guard let raw = ProcessInfo.processInfo.environment["DECK_PAIR_CODE"],
              !raw.isEmpty else { throw XCTSkip("DECK_PAIR_CODE not set") }
        return try parse(raw)
    }

    private func parse(_ raw: String) throws -> Code {
        let s = raw.filter { !$0.isWhitespace }
        let prefix = "adk1."
        XCTAssertTrue(s.lowercased().hasPrefix(prefix), "not an ADK1 code")
        var b64 = String(s.dropFirst(prefix.count))
            .replacingOccurrences(of: "-", with: "+").replacingOccurrences(of: "_", with: "/")
        while b64.count % 4 != 0 { b64 += "=" }
        let data = try XCTUnwrap(Data(base64Encoded: b64), "payload is not base64url")
        let j = try XCTUnwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(j["v"] as? Int, 1)
        let tls = try XCTUnwrap(j["tls"] as? String)
        let pin = j["pin"] as? String
        if tls == "pin" { XCTAssertNotNil(pin, "tls=pin requires a pin") }
        return Code(url: try XCTUnwrap(URL(string: try XCTUnwrap(j["url"] as? String))),
                    code: try XCTUnwrap(j["code"] as? String), tls: tls, pin: pin)
    }

    /// Trusts the server only if the leaf SPKI SHA-256 equals `pin` ("sha256/<base64>").
    private final class PinDelegate: NSObject, URLSessionDelegate {
        let pin: String
        init(pin: String) { self.pin = pin }
        func urlSession(_ s: URLSession, didReceive c: URLAuthenticationChallenge,
                        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
            guard c.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
                  let trust = c.protectionSpace.serverTrust,
                  let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
                  let leaf = chain.first, let key = SecCertificateCopyKey(leaf),
                  let raw = SecKeyCopyExternalRepresentation(key, nil) as Data?,
                  let attrs = SecKeyCopyAttributes(key) as? [CFString: Any] else {
                return completionHandler(.cancelAuthenticationChallenge, nil)
            }
            let type = attrs[kSecAttrKeyType] as? String ?? ""
            let bits = attrs[kSecAttrKeySizeInBits] as? Int ?? 0
            // DER SubjectPublicKeyInfo headers for the key types `openssl req -x509` makes.
            let header: [UInt8]
            if type == (kSecAttrKeyTypeECSECPrimeRandom as String) && bits == 256 {
                header = [0x30,0x59,0x30,0x13,0x06,0x07,0x2A,0x86,0x48,0xCE,0x3D,0x02,0x01,0x06,0x08,
                          0x2A,0x86,0x48,0xCE,0x3D,0x03,0x01,0x07,0x03,0x42,0x00]
            } else if type == (kSecAttrKeyTypeRSA as String) && bits == 2048 {
                header = [0x30,0x82,0x01,0x22,0x30,0x0D,0x06,0x09,0x2A,0x86,0x48,0x86,0xF7,0x0D,0x01,
                          0x01,0x01,0x05,0x00,0x03,0x82,0x01,0x0F,0x00]
            } else {
                return completionHandler(.cancelAuthenticationChallenge, nil)
            }
            let hash = Data(SHA256.hash(data: Data(header) + raw)).base64EncodedString()
            if "sha256/\(hash)" == pin {
                completionHandler(.useCredential, URLCredential(trust: trust))
            } else {
                completionHandler(.cancelAuthenticationChallenge, nil)
            }
        }
    }

    private func session(for c: Code, pin: String? = nil) -> URLSession {
        if c.tls == "pin", let p = pin ?? c.pin {
            return URLSession(configuration: .ephemeral, delegate: PinDelegate(pin: p), delegateQueue: nil)
        }
        return URLSession(configuration: .ephemeral)
    }

    private func send(_ s: URLSession, _ req: URLRequest) async throws -> (Int, [String: Any]) {
        let (d, r) = try await s.data(for: req)
        let json = (try? JSONSerialization.jsonObject(with: d) as? [String: Any]) ?? [:]
        return ((r as! HTTPURLResponse).statusCode, json)
    }

    private func redeem(_ s: URLSession, _ c: Code) async throws -> (Int, [String: Any]) {
        var req = URLRequest(url: c.url.appendingPathComponent("v1/pair"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: ["code": c.code, "device": "LivePairingTests"])
        return try await send(s, req)
    }

    func testARealCodeRedeemsOnceAndTheDeviceTokenOpensV1() async throws {
        let c = try { do { return try requiredCode() } catch { throw XCTSkip("DECK_PAIR_CODE not set") } }()
        let s = session(for: c)

        let (status, body) = try await redeem(s, c)
        XCTAssertEqual(status, 200, "redeem failed: \(body)")
        let token = try XCTUnwrap(body["token"] as? String)
        XCTAssertTrue(token.hasPrefix("adt_"))
        XCTAssertNotNil(body["device_id"])

        var agents = URLRequest(url: c.url.appendingPathComponent("v1/agents"))
        agents.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        let (aStatus, _) = try await send(s, agents)
        XCTAssertEqual(aStatus, 200, "device token must open /v1/agents through the pinned session")

        let (again, err) = try await redeem(s, c)
        XCTAssertEqual(again, 409)
        XCTAssertEqual(err["error"] as? String, "pair_used")
    }

    func testAWrongPinRefusesTheConnectionAndSpendsNothing() async throws {
        let c = try { do { return try requiredCode() } catch { throw XCTSkip("DECK_PAIR_CODE not set") } }()
        guard c.tls == "pin" else { throw XCTSkip("box is tls=ca; no pin to get wrong") }
        let wrong = "sha256/" + Data(SHA256.hash(data: Data("not the key".utf8))).base64EncodedString()
        do {
            _ = try await redeem(session(for: c, pin: wrong), c)
            XCTFail("a wrong pin must refuse the connection before the code is sent")
        } catch {
            XCTAssertNotNil(error as? URLError, "expected a TLS refusal, got \(error)")
        }
    }
}

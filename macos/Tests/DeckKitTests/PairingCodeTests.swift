import XCTest
@testable import DeckKit

/// K2. The vectors file is shared byte-for-byte with the server's tests: if
/// the two sides ever disagree about a code, one of these suites goes red.
final class PairingCodeTests: XCTestCase {

    private struct Vector: Decodable {
        let id: String
        let code: String
        let parsed: Parsed?
        let rejected: String?
    }
    private struct Parsed: Decodable {
        let code: String
        let exp: Int
        let name: String
        let tls: String
        let url: String
        let pin: String?
        let v: Int
    }

    private func vectors() throws -> [String: Vector] {
        let file = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("Fixtures/pairing_vectors.json")
        struct Root: Decodable { let vectors: [Vector] }
        let root = try JSONDecoder().decode(Root.self, from: Data(contentsOf: file))
        return Dictionary(uniqueKeysWithValues: root.vectors.map { ($0.id, $0) })
    }

    private func assertMatches(_ code: PairingCode, _ p: Parsed, file: StaticString = #filePath, line: UInt = #line) {
        XCTAssertEqual(code.code, p.code, file: file, line: line)
        XCTAssertEqual(code.expires, p.exp, file: file, line: line)
        XCTAssertEqual(code.name, p.name, file: file, line: line)
        XCTAssertEqual(code.url.absoluteString, p.url, file: file, line: line)
        XCTAssertEqual(code.pin, p.pin, file: file, line: line)
        XCTAssertEqual(code.tls, p.tls == "pin" ? .pin : .ca, file: file, line: line)
    }

    func testV1CaCodeParses() throws {
        let v = try vectors()["V1"]!
        assertMatches(try PairingCode.parse(v.code), v.parsed!)
    }

    func testV2PinCodeParses() throws {
        let v = try vectors()["V2"]!
        let code = try PairingCode.parse(v.code)
        assertMatches(code, v.parsed!)
        XCTAssertEqual(code.tls, .pin)
    }

    func testV3WrappedTerminalOutputParsesEqualToV1() throws {
        let v = try vectors()
        XCTAssertEqual(try PairingCode.parse(v["V3"]!.code), try PairingCode.parse(v["V1"]!.code))
    }

    func testV4PinModeWithoutAPinIsIncomplete() throws {
        let v = try vectors()["V4"]!
        XCTAssertThrowsError(try PairingCode.parse(v.code)) {
            XCTAssertEqual($0 as? PairingCodeError, .incomplete)
            XCTAssertEqual(($0 as? PairingCodeError)?.reason, v.rejected)
        }
    }

    func testV5NewerVersionSaysUpdateTheApp() throws {
        let v = try vectors()["V5"]!
        XCTAssertThrowsError(try PairingCode.parse(v.code)) {
            XCTAssertEqual($0 as? PairingCodeError, .version)
            XCTAssertEqual(($0 as? PairingCodeError)?.reason, v.rejected)
            XCTAssertEqual(($0 as? PairingCodeError)?.userFacingText,
                           "This code is from a newer Shaliach — update the app.")
        }
    }

    func testUpperCasePrefixAndSurroundingWhitespaceAreAccepted() throws {
        let v1 = try vectors()["V1"]!.code
        let lower = "  \t" + "adk1." + v1.dropFirst(5) + "\r\n "
        XCTAssertEqual(try PairingCode.parse(lower), try PairingCode.parse(v1))
    }

    func testGarbageIsMalformedNotACrash() {
        for junk in ["", "hello", "ADK1.", "ADK1.!!!!", "ADK1.e30", "https://x"] {
            XCTAssertThrowsError(try PairingCode.parse(junk), junk)
        }
        XCTAssertThrowsError(try PairingCode.parse("hello")) {
            XCTAssertEqual($0 as? PairingCodeError, .malformed)
        }
    }

    func testCodeExpiryIsAWallClockQuestion() throws {
        let code = try PairingCode.parse(try vectors()["V1"]!.code)
        XCTAssertFalse(code.isExpired(now: Date(timeIntervalSince1970: 1790812799)))
        XCTAssertTrue(code.isExpired(now: Date(timeIntervalSince1970: 1790812800)))
    }

    /// A code that names http:// or a path is not an origin we will send a
    /// secret to.
    func testNonHttpsOrPathedUrlIsRejected() throws {
        func make(_ url: String) -> String {
            let json = #"{"code":"q7Zb0cV2l8RkT1xYw3HnAg","exp":1,"name":"n","tls":"ca","url":"\#(url)","v":1}"#
            let b64 = Data(json.utf8).base64EncodedString()
                .replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_")
                .replacingOccurrences(of: "=", with: "")
            return "ADK1." + b64
        }
        XCTAssertThrowsError(try PairingCode.parse(make("http://1.2.3.4")))
        XCTAssertThrowsError(try PairingCode.parse(make("https://1.2.3.4/deck")))
        XCTAssertNoThrow(try PairingCode.parse(make("https://1.2.3.4:8443")))
    }
}

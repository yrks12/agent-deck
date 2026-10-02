import XCTest

/// **C-1: the first mic press shows both macOS prompts with our words.**
/// Without these two keys in the bundle's Info.plist, macOS does not show a
/// prompt at all — it kills the app the moment it touches the microphone.
final class VoiceBundleTests: XCTestCase {

    private func infoPlist() throws -> String {
        let script = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Scripts/make-app-bundle.sh")
        let text = try String(contentsOf: script, encoding: .utf8)
        let start = try XCTUnwrap(text.range(of: "<plist version=\"1.0\">"))
        let end = try XCTUnwrap(text.range(of: "</plist>"))
        return String(text[start.lowerBound..<end.upperBound])
    }

    private func value(of key: String, in plist: String) -> String? {
        guard let keyRange = plist.range(of: "<key>\(key)</key>") else { return nil }
        let rest = plist[keyRange.upperBound...]
        guard let open = rest.range(of: "<string>"), let close = rest.range(of: "</string>"),
              open.upperBound <= close.lowerBound else { return nil }
        return String(rest[open.upperBound..<close.lowerBound])
    }

    func testTheBundleAsksForTheMicrophoneInPlainWords() throws {
        let sentence = value(of: "NSMicrophoneUsageDescription", in: try infoPlist())
        XCTAssertNotNil(sentence)
        XCTAssertGreaterThan(sentence?.count ?? 0, 20)
    }

    func testTheBundleAsksForSpeechRecognitionInPlainWords() throws {
        let sentence = value(of: "NSSpeechRecognitionUsageDescription", in: try infoPlist())
        XCTAssertNotNil(sentence)
        XCTAssertGreaterThan(sentence?.count ?? 0, 20)
    }

    func testThePlistStillParses() throws {
        let data = Data(try infoPlist().utf8)
        let parsed = try PropertyListSerialization.propertyList(from: data, format: nil) as? [String: Any]
        XCTAssertEqual(parsed?["CFBundleIdentifier"] as? String, "$DECK_BUNDLE_ID", "the id comes from Scripts/bundle-id.sh")
    }
}

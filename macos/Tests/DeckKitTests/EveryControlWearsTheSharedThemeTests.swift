import XCTest

/// **"This is not matching our UX/UI."** The owner's screenshot: a stock
/// macOS segmented control — system-blue selection on a grey bar — floating
/// over the conversation with the messages cut off behind it. The iPhone has
/// no such thing: desk-to-desk traffic is an inline chip in the thread, and
/// every control is drawn from the shared palette (`DeckPalette`): greys, ink,
/// and the two meanings, orange and green. System blue is not one of them.
///
/// A lint over the Mac's view sources, comments stripped. It fails on:
/// - the accent colour or system blue, which is the stock look by definition;
/// - `.borderedProminent` / `.bordered` button styles, which paint the accent;
/// - a `.segmented` picker, which is the control in the screenshot;
/// - a bezel `.roundedBorder` text field, whose focus ring is system blue.
/// The replacements are `DeckButtonStyle` and `DeckPillPicker`.
///
/// And it asks the good signal: the replacements are really in use, and the
/// app's roots set the phone's tint, so a default switch or link is not blue.
final class EveryControlWearsTheSharedThemeTests: XCTestCase {

    private var macos: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    }

    private func sources(_ folder: String) throws -> [(name: String, lines: [String])] {
        let dir = macos.appendingPathComponent(folder)
        let files = try FileManager.default.contentsOfDirectory(at: dir, includingPropertiesForKeys: nil)
            .filter { $0.pathExtension == "swift" }
        return try files.map { url in
            let lines = try String(contentsOf: url, encoding: .utf8)
                .split(separator: "\n", omittingEmptySubsequences: false)
                .map { line -> String in
                    let text = String(line)
                    if text.trimmingCharacters(in: .whitespaces).hasPrefix("//") { return "" }
                    // Drop a trailing comment so a sentence about the old
                    // style is not an offence.
                    if let range = text.range(of: " // ") { return String(text[..<range.lowerBound]) }
                    return text
                }
            return (url.lastPathComponent, lines)
        }
    }

    static let banned = [
        "accentColor", "Color.blue", ".systemBlue", "NSColor.controlAccentColor",
        ".borderedProminent", ".buttonStyle(.bordered)", ".pickerStyle(.segmented)",
        // A bezel field with a system-blue focus ring; `.deckField()` is the
        // phone's field grey.
        ".textFieldStyle(.roundedBorder)",
    ]

    func testNoMacViewUsesTheStockAccentOrStockControlStyles() throws {
        var offences: [String] = []
        for folder in ["Sources/DeckUI", "Sources/DeckApp"] {
            for file in try sources(folder) {
                for (index, line) in file.lines.enumerated() {
                    for word in Self.banned where line.contains(word) {
                        offences.append("\(file.name):\(index + 1) \(word)")
                    }
                }
            }
        }
        XCTAssertEqual(offences, [], "stock-styled controls left on the Mac: \(offences.joined(separator: ", "))")
    }

    func testTheThemedReplacementsAreInUseAndTheRootsSetThePhonesTint() throws {
        let all = try sources("Sources/DeckUI").flatMap(\.lines).joined(separator: "\n")
        XCTAssertGreaterThanOrEqual(all.components(separatedBy: ".deckPrimary").count - 1, 5,
                                    "the primary buttons are not the shared style")
        XCTAssertGreaterThanOrEqual(all.components(separatedBy: "DeckPillPicker(").count - 1, 2,
                                    "the scope switches are not the shared pill")
        let root = try String(contentsOf: macos.appendingPathComponent("Sources/DeckUI/DeckRootView.swift"),
                              encoding: .utf8)
        XCTAssertTrue(root.contains(".tint(DeckPalette.ink)"), "the window's controls take the system accent")
        let app = try String(contentsOf: macos.appendingPathComponent("Sources/DeckApp/DeckAppMain.swift"),
                             encoding: .utf8)
        XCTAssertTrue(app.contains(".tint(DeckPalette.ink)") || app.contains("deckThemed()"),
                      "the Settings window's controls take the system accent")
    }

    /// A switch takes the phone's green: under the window's ink tint an "on"
    /// switch would be a white track under a white knob.
    func testEverySwitchIsThePhonesGreen() throws {
        for file in try sources("Sources/DeckUI") {
            for (index, line) in file.lines.enumerated() where line.contains(".toggleStyle(.switch)") {
                XCTAssertTrue(line.contains(".tint(DeckPalette.working)"), "\(file.name):\(index + 1)")
            }
        }
    }

    /// The thread is the Direct thread; desk-to-desk traffic is the inline
    /// chip, as on the phone. No scope switch floats over the messages.
    func testTheConversationHasNoScopeSwitchOverTheMessages() throws {
        let thread = try String(contentsOf: macos.appendingPathComponent("Sources/DeckUI/ThreadView.swift"),
                                encoding: .utf8)
        XCTAssertFalse(thread.contains("threadPicker("), "the conversation draws a thread switch again")
        XCTAssertFalse(thread.contains("Picker(\"Conversation\""), "the conversation draws a thread switch again")
    }
}

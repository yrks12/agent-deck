import XCTest
@testable import DeckKit

/// The phone's key bar over the terminal: the keys the iOS keyboard does not
/// have, as the bytes a terminal reads. Same row as the screen's key bar
/// (esc, tab, arrows, delete, return, a sticky ctrl), different wire.
final class PhoneTerminalKeysTests: XCTestCase {

    func testTheRowIsTheScreenBarsRow() {
        XCTAssertEqual(TerminalKey.bar, [.escape, .tab, .left, .up, .down, .right, .backspace, .enter])
    }

    func testEachKeyIsTheBytesAnXtermSends() {
        XCTAssertEqual(TerminalKey.escape.bytes, [0x1B])
        XCTAssertEqual(TerminalKey.tab.bytes, [0x09])
        XCTAssertEqual(TerminalKey.enter.bytes, [0x0D])
        XCTAssertEqual(TerminalKey.backspace.bytes, [0x7F])
        XCTAssertEqual(TerminalKey.up.bytes, Array("\u{1b}[A".utf8))
        XCTAssertEqual(TerminalKey.down.bytes, Array("\u{1b}[B".utf8))
        XCTAssertEqual(TerminalKey.right.bytes, Array("\u{1b}[C".utf8))
        XCTAssertEqual(TerminalKey.left.bytes, Array("\u{1b}[D".utf8))
    }

    func testCtrlTurnsALetterIntoItsControlCode() {
        XCTAssertEqual(TerminalKey.control(of: "c"), [0x03])
        XCTAssertEqual(TerminalKey.control(of: "C"), [0x03])
        XCTAssertEqual(TerminalKey.control(of: "["), [0x1B])
        XCTAssertNil(TerminalKey.control(of: "é"))
        XCTAssertNil(TerminalKey.control(of: "ab"))
    }

    func testEveryKeyHasALabelForVoiceOver() {
        for key in TerminalKey.bar { XCTAssertFalse(key.label.isEmpty) }
    }
}

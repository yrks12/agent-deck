import Foundation

/// **The phone's key bar over the terminal.** The same row the screen's key
/// bar has (esc, tab, arrows, delete, return, and a sticky ctrl), sent as the
/// bytes an xterm sends rather than as keysyms — the far end is a PTY.
public enum TerminalKey: String, CaseIterable, Sendable {
    case escape, tab, left, up, down, right, backspace, enter

    /// The row, in the screen bar's order.
    public static let bar: [TerminalKey] = [.escape, .tab, .left, .up, .down, .right, .backspace, .enter]

    public var bytes: [UInt8] {
        switch self {
        case .escape: return [0x1B]
        case .tab: return [0x09]
        case .enter: return [0x0D]
        case .backspace: return [0x7F]
        case .up: return Array("\u{1b}[A".utf8)
        case .down: return Array("\u{1b}[B".utf8)
        case .right: return Array("\u{1b}[C".utf8)
        case .left: return Array("\u{1b}[D".utf8)
        }
    }

    public var label: String {
        switch self {
        case .escape: return "esc"
        case .tab: return "tab"
        case .left: return "Left"
        case .up: return "Up"
        case .down: return "Down"
        case .right: return "Right"
        case .backspace: return "Delete"
        case .enter: return "Return"
        }
    }

    /// The SF Symbol, or nil for a key drawn as its label.
    public var symbol: String? {
        switch self {
        case .escape, .tab: return nil
        case .left: return "arrow.left"
        case .up: return "arrow.up"
        case .down: return "arrow.down"
        case .right: return "arrow.right"
        case .backspace: return "delete.left"
        case .enter: return "return"
        }
    }

    /// Ctrl plus one character: its control code (ctrl-C is 0x03), or nil for
    /// a character that has none.
    public static func control(of text: String) -> [UInt8]? {
        guard text.count == 1, let scalar = text.uppercased().unicodeScalars.first,
              text.unicodeScalars.count == 1, (0x40...0x5F).contains(scalar.value)
        else { return nil }
        return [UInt8(scalar.value & 0x1F)]
    }
}

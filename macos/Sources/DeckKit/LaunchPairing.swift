import Foundation

/// The launch contract an installer relies on:
///
///     open "Agent Deck.app" --args --pair ADK1.<code>
///     open "Agent Deck.app" --args --pair=ADK1.<code>
///
/// The app opens its Connect screen with that code filled in. It never submits
/// it: the person clicks Connect. Pure so it can be tested without a launch.
public enum LaunchPairing {
    public static let flag = "--pair"

    /// The code named by the first `--pair` in `arguments` (the program name at
    /// index 0 is skipped), trimmed; nil when absent or empty.
    public static func code(from arguments: [String]) -> String? {
        var index = 1
        while index < arguments.count {
            let arg = arguments[index]
            if arg == flag {
                guard index + 1 < arguments.count, !arguments[index + 1].hasPrefix("--") else { return nil }
                return clean(arguments[index + 1])
            }
            if arg.hasPrefix(flag + "=") { return clean(String(arg.dropFirst(flag.count + 1))) }
            index += 1
        }
        return nil
    }

    private static func clean(_ raw: String) -> String? {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        return text.isEmpty ? nil : text
    }
}

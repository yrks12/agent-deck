import Foundation

/// **How fast a message is listened to — one setting per device.** The button
/// on the player strip cycles 1×, 1.25×, 1.5×, 1.75×, 2×, 0.75× and round
/// again; the choice is kept in `UserDefaults` and used for the next message.
public enum ListenSpeed {
    public static let key = "listenSpeed"
    public static let playNextKey = "listenPlayNext"
    public static let steps: [Double] = [1.0, 1.25, 1.5, 1.75, 2.0, 0.75]

    /// The speed after `current`; anything not on the list restarts at 1×.
    public static func next(after current: Double) -> Double {
        guard let index = steps.firstIndex(of: current) else { return 1.0 }
        return steps[(index + 1) % steps.count]
    }

    /// "1×", "1.25×", "2×".
    public static func label(_ speed: Double) -> String {
        let text = speed == speed.rounded() ? String(Int(speed)) : String(speed)
        return text + "×"
    }

    public static func stored(in defaults: UserDefaults = .standard) -> Double {
        let value = defaults.double(forKey: key)
        return steps.contains(value) ? value : 1.0
    }

    public static func store(_ speed: Double, in defaults: UserDefaults = .standard) {
        defaults.set(speed, forKey: key)
    }
}

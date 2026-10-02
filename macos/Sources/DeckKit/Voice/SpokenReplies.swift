import Foundation

/// **"Spoken replies" — one switch per device.** On (the default): after he
/// talks to a desk (hold to talk, or a voice message) its answer is read out
/// in the desk's call voice. Off: answers stay text, even right after he
/// spoke. A live call is a call and always speaks; this switch is for chat.
///
/// Views bind the same key with `@AppStorage(SpokenReplies.key)`, so the
/// header switch and the speaker read one value.
public enum SpokenReplies {
    public static let key = "spokenReplies"

    public static func isOn(in defaults: UserDefaults = .standard) -> Bool {
        defaults.object(forKey: key) as? Bool ?? true
    }

    public static func set(_ on: Bool, in defaults: UserDefaults = .standard) {
        defaults.set(on, forKey: key)
    }
}

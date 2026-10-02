import Foundation

/// **When a plan limit resets**, in the viewer's own clock: "resets 03:40"
/// within a day, "resets Mon 14:00" beyond it. `nil` when unknown or past.
public enum ResetLabel {
    public static func text(now: Date, resetsAt: Date?, calendar: Calendar = .current) -> String? {
        guard let reset = resetsAt, reset > now else { return nil }
        let f = DateFormatter()
        f.calendar = calendar
        f.timeZone = calendar.timeZone
        f.locale = Locale(identifier: "en_GB")
        if reset.timeIntervalSince(now) <= 24 * 3600 {
            f.dateFormat = "HH:mm"
        } else {
            f.dateFormat = "EEE HH:mm"
        }
        return "resets " + f.string(from: reset)
    }
}

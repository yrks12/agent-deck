import Foundation

/// The app's bundle id, as this process was built with it.
///
/// Every id the app scopes something to -- the Keychain service, the
/// UserDefaults domain, notification categories, the log subsystem -- derives
/// from this, so a build made under another id (`DECK_BUNDLE_ID`,
/// `Scripts/bundle-id.sh`) keeps its own Keychain item and settings instead of
/// sharing, or stranding, someone else's. The phone's own id is `<id>.ios`, so
/// an app names the shared id in its Info.plist (`DeckBundleID`); failing that
/// an `.app` uses its bundle id, and anything else (the test runner) the
/// neutral open-source id.
public enum DeckIdentity {
    public static let neutral = "dev.agentdeck.app"

    public static let bundleID: String = resolve(Bundle.main)

    static func resolve(_ bundle: Bundle) -> String {
        if let named = bundle.object(forInfoDictionaryKey: "DeckBundleID") as? String,
           !named.isEmpty, !named.contains("$") {
            return named
        }
        guard bundle.bundleURL.pathExtension == "app",
              let id = bundle.bundleIdentifier, !id.isEmpty else { return neutral }
        return id
    }
}

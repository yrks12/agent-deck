#if os(macOS)
import AppKit
import ApplicationServices
import CoreGraphics

/// Settings → Mac's "Open System Settings…" buttons for Mac control.
///
/// macOS lists an app under Accessibility or Screen Recording only after the
/// app has asked once. So each button asks first (that adds Agent Deck's row
/// and shows macOS's own prompt), then opens the pane he has to tick it in.
public struct MacPermissionRequest {
    public static let accessibilityPane = "Privacy_Accessibility"
    public static let screenRecordingPane = "Privacy_ScreenCapture"

    let askAccessibility: () -> Void
    let askScreenRecording: () -> Void
    let open: (URL) -> Void

    public init(askAccessibility: @escaping () -> Void, askScreenRecording: @escaping () -> Void,
                open: @escaping (URL) -> Void) {
        self.askAccessibility = askAccessibility
        self.askScreenRecording = askScreenRecording
        self.open = open
    }

    /// The real asks and the real System Settings.
    public static var live: MacPermissionRequest {
        MacPermissionRequest(
            askAccessibility: {
                let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
                _ = AXIsProcessTrustedWithOptions([key: true] as CFDictionary)
            },
            askScreenRecording: { _ = CGRequestScreenCaptureAccess() },
            open: { NSWorkspace.shared.open($0) })
    }

    public func request(_ pane: String) {
        switch pane {
        case Self.accessibilityPane: askAccessibility()
        case Self.screenRecordingPane: askScreenRecording()
        default: break
        }
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)") { open(url) }
    }
}
#endif

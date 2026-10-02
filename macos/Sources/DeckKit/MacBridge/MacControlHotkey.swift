#if os(macOS)
import Carbon.HIToolbox
import Foundation

/// **⌃⌥⌘. stops Mac control, anywhere, at once.**
///
/// A Carbon hot key: it needs no permission, works whichever app is in
/// front, and the event is consumed (never reaches the app he is in).
/// Registered only while a grant is live.
@MainActor
public final class MacControlHotkey {
    public static let keyCode = UInt32(kVK_ANSI_Period)
    public static let modifiers = UInt32(controlKey | optionKey | cmdKey)

    private var ref: EventHotKeyRef?
    private var handler: EventHandlerRef?
    private static var onPress: (() -> Void)?

    public init() {}

    public var isRegistered: Bool { ref != nil }

    public func register(_ action: @escaping () -> Void) {
        Self.onPress = action
        guard ref == nil else { return }
        var spec = EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed))
        InstallEventHandler(GetApplicationEventTarget(), { _, _, _ in
            DispatchQueue.main.async { MainActor.assumeIsolated { MacControlHotkey.onPress?() } }
            return noErr
        }, 1, &spec, nil, &handler)
        let id = EventHotKeyID(signature: OSType(0x4443_4B53), id: 1)   // "DCKS"
        RegisterEventHotKey(Self.keyCode, Self.modifiers, id, GetApplicationEventTarget(), 0, &ref)
    }

    public func unregister() {
        if let ref { UnregisterEventHotKey(ref) }
        if let handler { RemoveEventHandler(handler) }
        ref = nil
        handler = nil
        Self.onPress = nil
    }
}
#endif

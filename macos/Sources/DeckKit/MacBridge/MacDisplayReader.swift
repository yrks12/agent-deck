#if os(macOS)
import AppKit
import CoreGraphics
import Foundation

/// **The displays this Mac has right now**, as the poll reports them.
///
/// Geometry comes from CoreGraphics (any thread): `CGDisplayBounds` is the
/// display's frame in global points, the display mode's pixel width over the
/// point width is its scale. The human name ("PM1561P", "Built-in Retina
/// Display") only AppKit knows, and `NSScreen` belongs to the main thread, so
/// names are read there at launch and on every screen change and cached.
public enum MacDisplayReader {
    private static let lock = NSLock()
    nonisolated(unsafe) private static var names: [Int: String] = [:]
    nonisolated(unsafe) private static var observer: NSObjectProtocol?

    /// Reads the names now and again whenever a display is plugged in,
    /// unplugged or rearranged.
    @MainActor public static func watchNames() {
        refreshNames()
        guard observer == nil else { return }
        observer = NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification, object: nil, queue: .main) { _ in
            MainActor.assumeIsolated { refreshNames() }
        }
    }

    @MainActor static func refreshNames() {
        var out: [Int: String] = [:]
        for screen in NSScreen.screens {
            if let n = screen.deviceDescription[NSDeviceDescriptionKey("NSScreenNumber")] as? NSNumber {
                out[n.intValue] = screen.localizedName
            }
        }
        lock.withLock { names = out }
    }

    /// Every active display. Empty only if CoreGraphics refuses.
    public static func current() -> [MacDisplayInfo] {
        var ids = [CGDirectDisplayID](repeating: 0, count: 16)
        var count: UInt32 = 0
        guard CGGetActiveDisplayList(UInt32(ids.count), &ids, &count) == .success else { return [] }
        let known = lock.withLock { names }
        return ids.prefix(Int(count)).map { id in
            let b = CGDisplayBounds(id)
            let mode = CGDisplayCopyDisplayMode(id)
            let wpx = mode?.pixelWidth ?? Int(b.width)
            let hpx = mode?.pixelHeight ?? Int(b.height)
            let scale = b.width > 0 ? Double(wpx) / Double(b.width) : 1
            let fallback = CGDisplayIsBuiltin(id) != 0 ? "Built-in Display" : "Display \(id)"
            return MacDisplayInfo(id: Int(id), name: known[Int(id)] ?? fallback, widthPx: wpx, heightPx: hpx,
                                  scale: scale, originX: Double(b.origin.x), originY: Double(b.origin.y),
                                  isMain: CGDisplayIsMain(id) != 0)
        }
    }
}
#endif

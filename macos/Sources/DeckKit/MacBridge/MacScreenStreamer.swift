#if os(macOS)
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit

/// **The live view: this Mac's main display, pushed to the deck only while
/// someone watches.**
///
/// The Mac never listens, so it pushes. A poll answering `watch: true` (under
/// a live control grant) starts this loop; each upload is answered with
/// whether anyone still watches, and `false` ends it. Nothing runs while
/// nobody looks — the app's idle CPU is the budget.
///
/// ScreenCaptureKit one-shot captures at `fps`, scaled to the display's point
/// size (half the pixels on Retina): modest, and the same space the agent's
/// clicks are mapped from. An unchanged screen is still re-sent every
/// `keepFresh` seconds so the viewer can tell "still" from "dead".
public actor MacScreenStreamer: MacScreenStreaming {
    public static let fps: Double = 2
    public static let maxWidth = 1600
    public static let quality: Double = 0.6
    static let keepFresh: TimeInterval = 2

    private let client: any MacNodeClienting
    private var task: Task<Void, Never>?

    public init(client: any MacNodeClienting) { self.client = client }

    public static var screenRecordingGranted: Bool { CGPreflightScreenCaptureAccess() }

    /// The live view's pixel size: the main display in points, capped.
    public static func frameSize(display: MacDisplayFrame = MacInputInjector.mainDisplay) -> MacSpace {
        let w = max(1, Int(display.width.rounded()))
        let h = max(1, Int(display.height.rounded()))
        guard w > maxWidth else { return MacSpace(width: w, height: h) }
        return MacSpace(width: maxWidth, height: max(1, h * maxWidth / w))
    }

    public func start(nodeId: String) {
        guard task == nil, Self.screenRecordingGranted else { return }
        task = Task { [client] in
            await Self.loop(client: client, nodeId: nodeId)
            await self.ended()
        }
    }

    public func stop() {
        task?.cancel()
        task = nil
    }

    private func ended() { task = nil }

    private static func loop(client: any MacNodeClienting, nodeId: String) async {
        guard let display = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
            .displays.first(where: { $0.displayID == CGMainDisplayID() }) else { return }
        let filter = SCContentFilter(display: display, excludingWindows: [])
        let size = frameSize()
        let config = SCStreamConfiguration()
        config.width = size.width
        config.height = size.height
        config.showsCursor = true
        var last: Data?
        var lastSent = Date.distantPast
        while !Task.isCancelled {
            let began = Date()
            if let image = try? await SCScreenshotManager.captureImage(contentFilter: filter, configuration: config),
               let jpeg = encode(image) {
                if jpeg != last || Date().timeIntervalSince(lastSent) >= keepFresh {
                    do {
                        let watching = try await client.postFrame(nodeId: nodeId, jpeg: jpeg,
                                                                  width: image.width, height: image.height)
                        last = jpeg
                        lastSent = Date()
                        if !watching { return }
                    } catch {
                        return   // the deck is unreachable or refused: the next watch restarts it
                    }
                }
            }
            let wait = max(0.05, 1 / fps - Date().timeIntervalSince(began))
            try? await Task.sleep(nanoseconds: UInt64(wait * 1e9))
        }
    }

    static func encode(_ image: CGImage) -> Data? {
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil) else { return nil }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        return CGImageDestinationFinalize(dest) ? out as Data : nil
    }
}
#endif

#if os(macOS)
import CoreGraphics
import Foundation
import ImageIO
import ScreenCaptureKit

/// **The live view: the display(s) a viewer picked (main by default), pushed
/// to the deck only while someone watches.**
///
/// The Mac never listens, so it pushes. A poll answering `watch: true` (the
/// owner's own viewer; no agent grant needed) starts this loop; each upload is answered with
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

    /// Which displays to stream; empty = the main one. Updated by every poll
    /// and every upload's answer, so switching displays in a viewer moves the
    /// capture within one frame.
    private var wanted: [Int] = []

    public func start(nodeId: String, displays: [Int]) {
        wanted = displays
        guard task == nil, Self.screenRecordingGranted else { return }
        task = Task { [client] in
            await Self.loop(client: client, nodeId: nodeId, streamer: self)
            await self.ended()
        }
    }

    public func stop() {
        task?.cancel()
        task = nil
    }

    private func ended() { task = nil }
    fileprivate func want(_ displays: [Int]) { wanted = displays }
    fileprivate func targets() -> [Int] { wanted }

    /// One captured display: its filter and size, kept until the set changes.
    private struct Target {
        let id: Int
        let filter: SCContentFilter
        let config: SCStreamConfiguration
        var last: Data?
        var lastSent = Date.distantPast
    }

    private static func targets(for ids: [Int]) async -> [Target] {
        guard let content = try? await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: true)
        else { return [] }
        let live = MacDisplayReader.current()
        let main = Int(CGMainDisplayID())
        let picked = ids.isEmpty ? [main] : ids
        return picked.compactMap { id in
            guard let display = content.displays.first(where: { Int($0.displayID) == id }),
                  let info = live.first(where: { $0.id == id }) else { return nil }
            let size = frameSize(display: info.frame)
            let config = SCStreamConfiguration()
            config.width = size.width
            config.height = size.height
            config.showsCursor = true
            return Target(id: id, filter: SCContentFilter(display: display, excludingWindows: []), config: config)
        }
    }

    private static func loop(client: any MacNodeClienting, nodeId: String, streamer: MacScreenStreamer) async {
        var ids = await streamer.targets()
        var targets = await targets(for: ids)
        while !Task.isCancelled {
            let began = Date()
            for i in targets.indices {
                guard let image = try? await SCScreenshotManager.captureImage(contentFilter: targets[i].filter,
                                                                              configuration: targets[i].config),
                      let jpeg = encode(image) else { continue }
                guard jpeg != targets[i].last || Date().timeIntervalSince(targets[i].lastSent) >= keepFresh else { continue }
                do {
                    let answer = try await client.postFrame(nodeId: nodeId, jpeg: jpeg, width: image.width,
                                                            height: image.height, display: targets[i].id)
                    targets[i].last = jpeg
                    targets[i].lastSent = Date()
                    if !answer.watch { return }
                    if let displays = answer.displays { await streamer.want(displays) }
                } catch {
                    return   // the deck is unreachable or refused: the next watch restarts it
                }
            }
            let now = await streamer.targets()
            if now != ids || targets.isEmpty {
                ids = now
                targets = await Self.targets(for: ids)
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

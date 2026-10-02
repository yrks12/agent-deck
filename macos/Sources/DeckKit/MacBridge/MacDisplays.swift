import Foundation

/// **Every display on his Mac, not just the main one (owner, 2026-10-02).**
///
/// MEASURED on his MacBook Pro with a second monitor: the built-in Retina
/// panel is display 1 at (0,0), 1512x982 points, 3024x1964 pixels; a
/// PM1561P is display 3 at (1512,0), 1920x1080 points at scale 1. The live
/// view, the agent's screenshot and every click were pinned to the main
/// display, so nothing on the second one could be seen or reached.
///
/// This file is the pure half: what the Mac reports about each display, the
/// order and the names a viewer shows, and one pixel of a picture of a
/// display as a global CGEvent point. Reading the real displays lives in
/// `MacDisplayReader` (macOS only).

/// One display, as the poll reports it. Origins are global points (the
/// main display's top-left is 0,0; a display to its left has a negative x,
/// one above it a negative y). Pixels are the panel's backing pixels.
public struct MacDisplayInfo: Codable, Equatable, Sendable, Identifiable {
    public var id: Int
    public var name: String
    public var widthPx: Int
    public var heightPx: Int
    /// Pixels per point: 2 on Retina, 1 on most external monitors.
    public var scale: Double
    public var originX: Double
    public var originY: Double
    public var isMain: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, scale
        case widthPx = "width_px"
        case heightPx = "height_px"
        case originX = "origin_x"
        case originY = "origin_y"
        case isMain = "is_main"
    }

    public init(id: Int, name: String, widthPx: Int, heightPx: Int, scale: Double,
                originX: Double, originY: Double, isMain: Bool) {
        self.id = id
        self.name = name
        self.widthPx = widthPx
        self.heightPx = heightPx
        self.scale = scale
        self.originX = originX
        self.originY = originY
        self.isMain = isMain
    }

    /// The display's frame in global points: what CGEvent takes.
    public var frame: MacDisplayFrame {
        let s = scale > 0 ? scale : 1
        return MacDisplayFrame(x: originX, y: originY, width: Double(widthPx) / s, height: Double(heightPx) / s)
    }

    /// "Built-in" for the laptop's own panel, the monitor's name otherwise.
    public var shortName: String {
        name.lowercased().hasPrefix("built-in") ? "Built-in" : name
    }
}

public enum MacDisplays {
    /// Main first (Display 1), then left to right, top to bottom: the order
    /// every viewer numbers them in.
    public static func ordered(_ displays: [MacDisplayInfo]) -> [MacDisplayInfo] {
        displays.sorted { a, b in
            if a.isMain != b.isMain { return a.isMain }
            if a.originX != b.originX { return a.originX < b.originX }
            if a.originY != b.originY { return a.originY < b.originY }
            return a.id < b.id
        }
    }

    /// "Display 2 · PM1561P".
    public static func label(_ display: MacDisplayInfo, in displays: [MacDisplayInfo]) -> String {
        let n = (ordered(displays).firstIndex { $0.id == display.id } ?? 0) + 1
        return "Display \(n) · \(display.shortName)"
    }

    /// The display `id` names, or the main one when `id` is nil. Nil when
    /// that display is gone (unplugged): never a guess at another one.
    public static func resolve(_ id: Int?, in displays: [MacDisplayInfo]) -> MacDisplayInfo? {
        guard let id else { return displays.first(where: \.isMain) ?? ordered(displays).first }
        return displays.first { $0.id == id }
    }

    /// A pixel of a picture of display `id` (nil = main), as a global
    /// CGEvent point. `space` is that picture's pixel size: the live view
    /// sends points, the agent's screenshot sends backing pixels, and the
    /// ratio covers both. Nil when the display is gone.
    public static func point(x: Int, y: Int, space: MacSpace, display id: Int?,
                             in displays: [MacDisplayInfo]) -> (x: Double, y: Double)? {
        guard let display = resolve(id, in: displays) else { return nil }
        return MacInputMapping.point(x: x, y: y, space: space, display: display.frame)
    }
}

/// **His last display choice, per Mac**, so the viewer opens where he left
/// it. Nil = the main display (nothing stored).
public struct MacDisplayMemory: @unchecked Sendable {
    private let defaults: UserDefaults

    public init(defaults: UserDefaults = .standard) { self.defaults = defaults }

    public static let standard = MacDisplayMemory()

    private func key(_ nodeId: String) -> String { "macLiveViewDisplay.\(nodeId)" }

    public func load(nodeId: String) -> Int? {
        defaults.object(forKey: key(nodeId)) as? Int
    }

    public func save(nodeId: String, display: Int?) {
        if let display { defaults.set(display, forKey: key(nodeId)) } else { defaults.removeObject(forKey: key(nodeId)) }
    }
}

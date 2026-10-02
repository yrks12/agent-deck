import Foundation

/// The silhouettes an agent can be drawn as. Eight, each readable at 32pt and
/// none of them a plain circle: a roster of identical circles is what this
/// replaced. The raw values are the wire-stable names the view layer draws.
public enum AvatarShape: String, CaseIterable, Hashable, Sendable {
    case blob
    case hexagon
    case wedge
    case tablet
    case pebble
    case teardrop
    case cloud
    case squircle
}

/// Where a character's eyes rest when it is not busy. Part of its personality:
/// two desks of the same shape and colour still look at the world differently.
public enum EyeGaze: String, CaseIterable, Hashable, Sendable {
    case centre, upRight, upLeft, left, right

    /// Direction in -1...1 on each axis (y grows downward).
    public var vector: (dx: Double, dy: Double) {
        switch self {
        case .centre: return (0, 0)
        case .upRight: return (0.8, -0.8)
        case .upLeft: return (-0.8, -0.8)
        case .left: return (-0.9, 0.1)
        case .right: return (0.9, 0.1)
        }
    }
}

/// How far apart the two dot eyes sit.
public enum EyeSpread: String, CaseIterable, Hashable, Sendable {
    case close, normal, wide
}

/// A character derived from the agent's name: a shape, a colour, two dot eyes
/// with a resting gaze and spacing, and sometimes a blush. Initials are kept
/// for the places that name a *person* (the account row), never for a desk.
///
/// The derivation is FNV-1a rather than `Hasher`, deliberately. Swift seeds its
/// hashing per process, so a face built on `hashValue` would change every time
/// the app is launched — which defeats the entire point of having one.
public struct AvatarLook: Equatable, Hashable, Sendable {
    public let shape: AvatarShape
    /// An index into whatever palette the view layer holds. DeckKit does not
    /// know what the colours are, only that the choice must be stable.
    public let tintIndex: Int
    public let initials: String
    public let gaze: EyeGaze
    public let eyes: EyeSpread
    public let blush: Bool
    /// Desynchronises two working desks' eye movement; stable per name.
    public let seed: Int

    public init(
        shape: AvatarShape, tintIndex: Int, initials: String,
        gaze: EyeGaze = .centre, eyes: EyeSpread = .normal, blush: Bool = false, seed: Int = 0
    ) {
        self.shape = shape
        self.tintIndex = tintIndex
        self.initials = initials
        self.gaze = gaze
        self.eyes = eyes
        self.blush = blush
        self.seed = seed
    }

    /// How many distinct tints the derivation spreads across. The view layer
    /// may hold fewer and take the remainder; it may not hold more and expect
    /// them all to appear.
    public static let tintCount = 12

    public static func forName(_ name: String) -> AvatarLook {
        let hash = fnv1a(name)
        // Every trait is a different slice of the same hash: they vary
        // independently, so two agents rarely collide on all of them.
        return AvatarLook(
            shape: AvatarShape.allCases[Int(hash % UInt64(AvatarShape.allCases.count))],
            tintIndex: Int((hash >> 16) % UInt64(tintCount)),
            initials: initials(of: name),
            gaze: EyeGaze.allCases[Int((hash >> 24) % UInt64(EyeGaze.allCases.count))],
            eyes: EyeSpread.allCases[Int((hash >> 32) % UInt64(EyeSpread.allCases.count))],
            blush: (hash >> 40) % 4 == 0,
            seed: Int((hash >> 48) % 997)
        )
    }

    /// 64-bit FNV-1a over the UTF-8 bytes. Small, dependency-free, and — the
    /// only property this needs — identical on every run and every machine.
    static func fnv1a(_ text: String) -> UInt64 {
        var hash: UInt64 = 0xcbf2_9ce4_8422_2325
        for byte in Array(text.utf8) {
            hash ^= UInt64(byte)
            hash = hash &* 0x100_0000_01b3
        }
        return hash
    }

    /// "travel scout" -> "TS". A name with no letters in it still gets
    /// something to draw rather than an empty shape.
    static func initials(of name: String) -> String {
        let words = name.split(separator: " ").prefix(2)
        let letters = words.compactMap { word -> String? in
            guard let first = word.first, first.isLetter || first.isNumber else { return nil }
            return String(first).uppercased()
        }
        return letters.isEmpty ? "?" : letters.joined()
    }
}

extension Agent {
    /// Keyed on `name` alone — the one field that cannot be edited — so a
    /// retitled or offline desk keeps the face the sidebar learned.
    /// A K6 `avatarStyle` overrides shape and colour; a shape this build cannot
    /// draw keeps the derived one.
    public var look: AvatarLook {
        let derived = AvatarLook.forName(name)
        guard let style = avatarStyle else { return derived }
        return AvatarLook(
            shape: AvatarShape(rawValue: style.shape) ?? derived.shape,
            tintIndex: ((style.color % AvatarLook.tintCount) + AvatarLook.tintCount) % AvatarLook.tintCount,
            initials: derived.initials, gaze: derived.gaze, eyes: derived.eyes,
            blush: derived.blush, seed: derived.seed)
    }
}

/// One pose of a working character's eyes.
public struct EyePose: Equatable, Sendable {
    public let dx: Double
    public let dy: Double
    public let blink: Bool
}

/// **The only motion in the sidebar, and it is rationed.** Eyes move while a
/// desk is working and at no other time; the view drives them from a periodic
/// timeline ticking once per `interval` (never the display rate), and Reduce
/// Motion switches them off. A sidebar that is on screen all day must cost
/// nothing when nothing is happening.
public enum EyeMotion {
    /// Seconds between poses.
    public static let interval: Double = 0.8

    public static func animates(attention: RowAttention, reduceMotion: Bool) -> Bool {
        attention == .working && !reduceMotion
    }

    private static let glances: [(Double, Double)] = [
        (0.7, -0.6), (-0.8, -0.2), (0.0, 0.7), (0.9, 0.2), (-0.5, -0.8), (0.4, 0.5), (-0.9, 0.3), (0.0, -0.5),
    ]

    /// Pure in `(step, seed)`: a glance every tick, a blink every fifth, and a
    /// per-character offset so two working desks are never in unison.
    public static func pose(step: Int, seed: Int) -> EyePose {
        let n = step &+ seed
        let index = ((n &* 5) &+ seed &* 3) % glances.count
        let glance = glances[abs(index)]
        return EyePose(dx: glance.0, dy: glance.1, blink: abs(n) % 5 == 4)
    }
}

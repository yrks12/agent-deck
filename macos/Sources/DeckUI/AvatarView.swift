import SwiftUI
#if os(macOS)
import AppKit
#else
import UIKit
#endif
import DeckKit

/// **The two colours the sidebar's status can be, and the one rule they obey.**
///
/// System orange is an *accent* colour. As a dot it is fine — WCAG asks 3:1 of
/// a status indicator. As the **text** of "Waiting for you: …" on a light
/// sidebar it lands near 2.2:1 against white, which is a fail, and this is the
/// one row state the whole product is managed by.
///
/// So the text form is darkened in the light appearance and left alone in the
/// dark one, resolved by AppKit at draw time so increased-contrast and a theme
/// switch both follow the OS. `SidebarContrastTests` computes the real ratio
/// off the resolved sRGB components rather than trusting this comment.
public enum AttentionPalette {

    /// The dot on the face.
    ///
    /// Darkened in the light appearance like the text is. Measured before this
    /// existed: system green read **2.22:1** and system orange **2.31:1** on a
    /// light sidebar, both under the 3:1 WCAG 1.4.11 asks of a status
    /// indicator, and a status he cannot see is not a status.
    #if os(macOS)
    public static func dot(for attention: RowAttention) -> NSColor? {
        switch attention {
        case .quiet: return nil
        case .working: return workingDot
        case .waitingForYou: return waitingDot
        }
    }

    /// The same meaning as a line of type, which is a stricter test.
    public static let waitingText = adaptive(.systemOrange, darkenedBy: 0.42)
    static let waitingDot = adaptive(.systemOrange, darkenedBy: 0.34)
    static let workingDot = adaptive(.systemGreen, darkenedBy: 0.38)

    /// Apple's colour in the dark appearance, and the same hue taken toward
    /// black in the light one. Resolved by AppKit at draw time, so a theme
    /// switch and increased-contrast both follow the OS rather than a snapshot
    /// taken at launch.
    private static func adaptive(_ base: NSColor, darkenedBy fraction: CGFloat) -> NSColor {
        NSColor(name: nil) { appearance in
            guard appearance.bestMatch(from: [.aqua, .darkAqua]) != .darkAqua else { return base }
            return base.blended(withFraction: fraction, of: .black) ?? base
        }
    }
    #else
    // The iPhone app compiles this file into its own target: the same rule,
    // resolved by UIKit's trait collection instead of an NSAppearance.
    public static func dot(for attention: RowAttention) -> UIColor? {
        switch attention {
        case .quiet: return nil
        case .working: return workingDot
        case .waitingForYou: return waitingDot
        }
    }

    public static let waitingText = adaptive(.systemOrange, darkenedBy: 0.42)
    static let waitingDot = adaptive(.systemOrange, darkenedBy: 0.34)
    static let workingDot = adaptive(.systemGreen, darkenedBy: 0.38)

    private static func adaptive(_ base: UIColor, darkenedBy fraction: CGFloat) -> UIColor {
        UIColor { traits in
            guard traits.userInterfaceStyle != .dark else { return base }
            var r: CGFloat = 0, g: CGFloat = 0, b: CGFloat = 0, a: CGFloat = 0
            base.resolvedColor(with: traits).getRed(&r, green: &g, blue: &b, alpha: &a)
            let keep = 1 - fraction
            return UIColor(red: r * keep, green: g * keep, blue: b * keep, alpha: a)
        }
    }
    #endif
}

/// The silhouette an agent is drawn in. Which one is decided in DeckKit from
/// the agent's name, so it is the same shape on every launch. Each is drawn in
/// the unit square so the eyes can be placed by proportion.
struct AgentAvatarShape: Shape {
    let kind: AvatarShape

    func path(in rect: CGRect) -> Path {
        func pt(_ x: CGFloat, _ y: CGFloat) -> CGPoint {
            CGPoint(x: rect.minX + x * rect.width, y: rect.minY + y * rect.height)
        }
        switch kind {
        case .blob:
            var path = Path()
            path.move(to: pt(0.52, 0.02))
            path.addCurve(to: pt(0.99, 0.50), control1: pt(0.80, 0.02), control2: pt(0.99, 0.24))
            path.addCurve(to: pt(0.46, 0.98), control1: pt(0.99, 0.78), control2: pt(0.74, 0.99))
            path.addCurve(to: pt(0.01, 0.47), control1: pt(0.20, 0.99), control2: pt(0.01, 0.74))
            path.addCurve(to: pt(0.52, 0.02), control1: pt(0.01, 0.20), control2: pt(0.26, 0.02))
            path.closeSubpath()
            return path
        case .hexagon:
            let vertices = (0..<6).map { index -> CGPoint in
                let angle = -CGFloat.pi / 2 + CGFloat(index) * .pi / 3
                return pt(0.5 + 0.5 * cos(angle), 0.5 + 0.5 * sin(angle))
            }
            return roundedPolygon(vertices, radius: rect.width * 0.14)
        case .wedge:
            return roundedPolygon([pt(0.5, 0.03), pt(0.98, 0.93), pt(0.02, 0.93)], radius: rect.width * 0.2)
        case .tablet:
            return Path(roundedRect: CGRect(
                x: rect.minX + rect.width * 0.05, y: rect.minY,
                width: rect.width * 0.9, height: rect.height),
                cornerRadius: rect.width * 0.34, style: .continuous)
        case .pebble:
            return Path(roundedRect: CGRect(
                x: rect.minX, y: rect.minY + rect.height * 0.14,
                width: rect.width, height: rect.height * 0.72),
                cornerRadius: rect.height * 0.36, style: .continuous)
        case .teardrop:
            var path = Path()
            path.move(to: pt(0.5, 0.02))
            path.addCurve(to: pt(0.95, 0.62), control1: pt(0.62, 0.22), control2: pt(0.95, 0.38))
            path.addCurve(to: pt(0.5, 0.98), control1: pt(0.95, 0.84), control2: pt(0.74, 0.98))
            path.addCurve(to: pt(0.05, 0.62), control1: pt(0.26, 0.98), control2: pt(0.05, 0.84))
            path.addCurve(to: pt(0.5, 0.02), control1: pt(0.05, 0.38), control2: pt(0.38, 0.22))
            path.closeSubpath()
            return path
        case .cloud:
            var path = Path(roundedRect: CGRect(
                x: rect.minX, y: rect.minY + rect.height * 0.40,
                width: rect.width, height: rect.height * 0.56),
                cornerRadius: rect.height * 0.28, style: .continuous)
            for (cx, cy, r) in [(0.30, 0.44, 0.24), (0.57, 0.36, 0.31), (0.79, 0.52, 0.2)] {
                path.addEllipse(in: CGRect(
                    x: rect.minX + (cx - r) * rect.width, y: rect.minY + (cy - r) * rect.height,
                    width: 2 * r * rect.width, height: 2 * r * rect.height))
            }
            return path
        case .squircle:
            return Path(roundedRect: rect, cornerRadius: rect.width * 0.42, style: .continuous)
        }
    }

    /// Where the eyes sit, as a fraction of the frame: a wedge and a teardrop
    /// carry their weight low, a cloud has its face under the bumps.
    var eyeLine: CGFloat {
        switch kind {
        case .blob: return 0.48
        case .hexagon: return 0.50
        case .wedge: return 0.64
        case .tablet: return 0.46
        case .pebble: return 0.50
        case .teardrop: return 0.62
        case .cloud: return 0.60
        case .squircle: return 0.50
        }
    }

    private func roundedPolygon(_ vertices: [CGPoint], radius: CGFloat) -> Path {
        var path = Path()
        let count = vertices.count
        let start = CGPoint(
            x: (vertices[0].x + vertices[1].x) / 2, y: (vertices[0].y + vertices[1].y) / 2)
        path.move(to: start)
        for offset in 1...count {
            let current = vertices[offset % count]
            let next = vertices[(offset + 1) % count]
            path.addArc(tangent1End: current, tangent2End: next, radius: radius)
        }
        path.closeSubpath()
        return path
    }
}

/// The two dot eyes. Static unless handed a pose; `pose` is the only thing
/// that ever changes, and only a working desk is handed one.
struct AvatarEyes: View {
    let look: AvatarLook
    let size: CGFloat
    let eyeLine: CGFloat
    /// Nobody home: lids half down.
    let sleepy: Bool
    var pose: EyePose?

    private static let ink = Color(red: 0.08, green: 0.07, blue: 0.13)

    var body: some View {
        let gaze = pose.map { (dx: $0.dx, dy: $0.dy) } ?? look.gaze.vector
        let reach = pose == nil ? size * 0.035 : size * 0.05
        let blinking = pose?.blink ?? false
        let spread: CGFloat = {
            switch look.eyes {
            case .close: return 0.125
            case .normal: return 0.17
            case .wide: return 0.215
            }
        }()
        let eyeW = size * 0.125
        let eyeH = size * 0.2
        ZStack {
            if look.blush && size >= 28 {
                ForEach([-1.0, 1.0], id: \.self) { side in
                    Capsule()
                        .fill(Color(red: 1, green: 0.45, blue: 0.5).opacity(0.38))
                        .frame(width: size * 0.16, height: size * 0.09)
                        .offset(x: side * size * (spread + 0.1), y: size * 0.16)
                }
            }
            ForEach([-1.0, 1.0], id: \.self) { side in
                ZStack(alignment: .topTrailing) {
                    Capsule().fill(Self.ink)
                    if size >= 40 && !sleepy && !blinking {
                        Circle().fill(.white.opacity(0.9))
                            .frame(width: eyeW * 0.36, height: eyeW * 0.36)
                            .offset(x: -eyeW * 0.14, y: eyeW * 0.2)
                    }
                }
                .frame(width: eyeW, height: eyeH)
                .scaleEffect(y: (sleepy ? 0.3 : 1) * (blinking ? 0.12 : 1))
                .offset(x: side * size * spread + gaze.dx * reach, y: gaze.dy * reach)
            }
        }
        .offset(y: size * (eyeLine - 0.5))
        .frame(width: size, height: size)
    }
}

/// An agent's character: a coloured shape with two dot eyes, never initials.
/// Shape, colour and temperament are derived from the name in DeckKit, so the
/// same desk looks the same way every launch — that stability is what makes a
/// sidebar scannable. A local image, when the desk has one, replaces the
/// character.
public struct AvatarView: View {
    let look: AvatarLook
    let attention: RowAttention
    /// Nobody is at this desk. Distinct from `attention`: a seated-but-frozen
    /// desk is not dark, it is stuck.
    let isDimmed: Bool
    let localAvatarURL: URL?
    var size: CGFloat = 32
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    /// **The face, already decided.** The sidebar's rows use this: the look and
    /// the avatar's file are settled once, when the roster is built, so drawing
    /// a row costs neither a hash nor a `stat`.
    public init(
        look: AvatarLook,
        attention: RowAttention,
        isDimmed: Bool,
        localAvatarURL: URL?,
        size: CGFloat = 32
    ) {
        self.look = look
        self.attention = attention
        self.isDimmed = isDimmed
        self.localAvatarURL = localAvatarURL
        self.size = size
    }

    /// Derives the look from the agent, for the panels that hold an `Agent` and
    /// no row. Counted, because deriving it is an FNV-1a hash over the name's
    /// UTF-8 bytes plus a split, a `compactMap` and two string allocations, and
    /// `agent.localAvatarURL` is a filesystem call. Free unless
    /// `DECK_DIAGNOSE=1`. See `SidebarBodyWorkTests`.
    public init(agent: Agent, size: CGFloat = 32) {
        Diagnostics.count("sidebar.avatar.look")
        self.init(
            look: agent.look,
            attention: RowAttention(for: agent),
            isDimmed: agent.state == .offline,
            localAvatarURL: agent.localAvatarURL,
            size: size)
    }

    private var shape: AgentAvatarShape { AgentAvatarShape(kind: look.shape) }

    public var body: some View {
        ZStack(alignment: .bottomTrailing) {
            face
            // The online dot, on the corner of the character — the one thing he
            // can read across a whole roster without opening anything. Ringed
            // in the sidebar's own material so it separates from the tile under
            // it at any tint, and never the only carrier of its meaning: the
            // row's spoken label says the word (see `AgentRow`).
            if let tint = AvatarView.tint(for: attention) {
                Circle()
                    .fill(tint)
                    .frame(width: dotSize, height: dotSize)
                    .overlay(Circle().strokeBorder(.background, lineWidth: dotSize * 0.2))
                    .offset(x: dotSize * 0.12, y: dotSize * 0.12)
            }
        }
        .frame(width: size, height: size)
        .accessibilityHidden(true)
    }

    private var dotSize: CGFloat { max(8, size * 0.28) }

    /// Green for working, orange for waiting on him, nothing for the rest.
    /// The **dot**, which is not text: WCAG asks 3:1 of a status indicator, and
    /// it carries a ring in the sidebar's own material besides.
    static func tint(for attention: RowAttention) -> Color? {
        #if os(macOS)
        AttentionPalette.dot(for: attention).map(Color.init(nsColor:))
        #else
        AttentionPalette.dot(for: attention).map(Color.init(uiColor:))
        #endif
    }

    private var face: some View {
        ZStack {
            body(of: Theme.avatarTint(look.tintIndex))
            // The deck stores an opaque avatar string and serves no images, so
            // one is only drawn when it names a file this Mac can reach.
            if let url = localAvatarURL {
                AsyncImage(url: url) { image in
                    image.resizable().scaledToFill()
                } placeholder: {
                    Color.clear
                }
                .clipShape(shape)
            } else {
                eyes
            }
        }
        .frame(width: size, height: size)
        // Dimming means "nobody is at this desk" — a seated-but-frozen desk
        // (`blocked.reason == .dialogUnrelayed`) is not dark, it is stuck, so
        // it stays at full opacity and lets the orange dot carry the urgency.
        .opacity(isDimmed ? 0.55 : 1)
    }

    private func body(of tint: Color) -> some View {
        ZStack {
            shape.fill(
                LinearGradient(
                    colors: [tint.mix(with: .white, by: 0.22), tint, tint.mix(with: .black, by: 0.14)],
                    startPoint: .top, endPoint: .bottom))
            // A soft catch-light, top left: the difference between a flat
            // sticker and a little creature.
            Ellipse()
                .fill(.white.opacity(0.16))
                .frame(width: size * 0.5, height: size * 0.26)
                .rotationEffect(.degrees(-24))
                .offset(x: -size * 0.14, y: -size * 0.3)
                .clipShape(shape)
        }
        .shadow(color: tint.opacity(size >= 56 ? 0.35 : 0), radius: size * 0.07, y: size * 0.04)
    }

    /// The eyes move only while the desk is working, on a timeline that ticks
    /// once per `EyeMotion.interval` — not the display rate. Every other state
    /// is a plain still picture with no timeline at all, so an idle roster
    /// costs nothing to keep on screen.
    ///
    /// The poses snap: an implicit `.animation` on the step measured 1,434
    /// layout passes in 0.5s in the inspector (`InspectorIsQuietTests`), the
    /// same permanent-animation trap that once pegged the app at 99% CPU.
    @ViewBuilder
    private var eyes: some View {
        if EyeMotion.animates(attention: attention, reduceMotion: reduceMotion) && !isDimmed {
            TimelineView(.periodic(from: Date(timeIntervalSinceReferenceDate: 0), by: EyeMotion.interval)) { context in
                let step = Int(context.date.timeIntervalSinceReferenceDate / EyeMotion.interval)
                AvatarEyes(
                    look: look, size: size, eyeLine: shape.eyeLine, sleepy: false,
                    pose: EyeMotion.pose(step: step, seed: look.seed))

            }
        } else {
            AvatarEyes(look: look, size: size, eyeLine: shape.eyeLine, sleepy: isDimmed)
        }
    }
}

/// **A row whose newest conversation has several desks in it.**
///
/// One letter tile said "this is a conversation with a desk" for a thread that
/// is a conversation between three, so the roster read as a list of 1:1s that
/// it is not. The faces overlap, the ones that do not fit are counted rather
/// than dropped, and the whole group falls back to the row's own single face
/// the moment the thread is an ordinary 1:1.
struct GroupedAvatarView: View {
    let row: SidebarRow
    var size: CGFloat = 32

    var body: some View {
        if row.participantLooks.isEmpty {
            AvatarView(
                look: row.look, attention: row.attention,
                isDimmed: row.agent.state == .offline,
                localAvatarURL: row.localAvatarURL, size: size)
        } else {
            stack
        }
    }

    private var stack: some View {
        ZStack(alignment: .bottomTrailing) {
            ZStack(alignment: .topLeading) {
                ForEach(Array(row.participantLooks.enumerated()), id: \.offset) { index, look in
                    AvatarView(
                        look: look, attention: index == 0 ? row.attention : .quiet,
                        isDimmed: false, localAvatarURL: nil, size: size * 0.72
                    )
                    .offset(x: CGFloat(index) * size * 0.36, y: CGFloat(index) * size * 0.28)
                }
            }
            if row.extraParticipantCount > 0 {
                Text("+\(row.extraParticipantCount)")
                    .font(.system(size: size * 0.28, weight: .semibold, design: .rounded))
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 3)
                    .background(.background, in: Capsule())
                    .offset(x: size * 0.1, y: size * 0.1)
            }
        }
        .frame(width: size, height: size, alignment: .topLeading)
        // The row's own label already names the desk and reads its preview;
        // the count of faces is decoration on top of that sentence.
        .accessibilityHidden(true)
    }
}

/// The chip beside a name: "Researcher", "The Builder".
public struct TitleChip: View {
    let title: String

    public init(title: String) {
        self.title = title
    }

    public var body: some View {
        Text(title)
            .font(.caption2.weight(.medium))
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(.quaternary, in: Capsule())
            .foregroundStyle(.secondary)
            .accessibilityLabel("Title: \(title)")
    }
}

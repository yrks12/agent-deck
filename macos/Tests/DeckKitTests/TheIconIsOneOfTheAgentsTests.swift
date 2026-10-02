import XCTest
import AppKit

/// **The app icon is one of the agents.** The owner asked for the icon to
/// look like the bots the app draws for every desk -- a rounded character
/// with two dark eyes -- instead of a fanned deck of cards, and for it to
/// read at 16 px in Finder.
///
/// Two properties, both measured off pixels rather than trusted:
///
/// 1. The master artwork is the berry character: below the eyes, at the
///    centre line, the icon is the character's pink/red body (the old deck
///    of cards is a near-white card there).
/// 2. The bundle's `.icns` carries the *hand-tuned* small sizes from
///    `Resources/AppIcon.iconset` (bigger eyes, no blush), not a `sips`
///    shrink of the 1024 master, which blurs the face into a pink square.
final class TheIconIsOneOfTheAgentsTests: XCTestCase {

    private var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
    }

    private func rgba(_ url: URL) throws -> (width: Int, height: Int, bytes: [UInt8]) {
        guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
              let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
            struct Undecodable: Error { let file: String }
            throw Undecodable(file: url.lastPathComponent)
        }
        let w = image.width, h = image.height
        var bytes = [UInt8](repeating: 0, count: w * h * 4)
        let ctx = CGContext(
            data: &bytes, width: w, height: h, bitsPerComponent: 8, bytesPerRow: w * 4,
            space: CGColorSpace(name: CGColorSpace.sRGB)!,
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        ctx.draw(image, in: CGRect(x: 0, y: 0, width: w, height: h))
        return (w, h, bytes)
    }

    private func pixel(_ img: (width: Int, height: Int, bytes: [UInt8]), _ x: Int, _ y: Int)
        -> (r: Int, g: Int, b: Int, a: Int) {
        let i = (y * img.width + x) * 4
        return (Int(img.bytes[i]), Int(img.bytes[i + 1]), Int(img.bytes[i + 2]), Int(img.bytes[i + 3]))
    }

    func testTheMasterIconIsTheBerryCharacterNotACardDeck() throws {
        let img = try rgba(packageRoot.appendingPathComponent("Resources/AppIcon.png"))
        XCTAssertEqual(img.width, 1024)
        // Centre column, low on the body (between the smile and the chin).
        let p = pixel(img, 512, 720)
        XCTAssertGreaterThan(p.r - p.g, 80, "centre-low of the icon is not the berry body: \(p)")
        XCTAssertGreaterThan(p.r - p.b, 60, "centre-low of the icon is not the berry body: \(p)")
        // Full bleed: the corner is the opaque dark canvas. macOS 26 masks an
        // app icon to its own squircle, and one with transparent margins (the
        // Big Sur grid) is shrunk onto a grey plate instead — measured off
        // `NSWorkspace.icon(forFile:)` on the installed app.
        let corner = pixel(img, 2, 2)
        XCTAssertEqual(corner.a, 255, "the icon has a transparent margin, so macOS 26 plates it")
        XCTAssertLessThan(corner.r, 60, "the corner is not the dark canvas: \(corner)")
    }

    /// **No alpha channel at all.** Measured with `NSWorkspace.icon(forFile:)`
    /// on macOS 26: the same full-bleed artwork saved as RGBA (every pixel
    /// opaque) is still shrunk onto a grey plate; saved as RGB it is masked to
    /// the system squircle. The channel, not the pixels, decides.
    func testTheMacArtworkCarriesNoAlphaChannel() throws {
        let set = packageRoot.appendingPathComponent("Resources/AppIcon.iconset")
        let files = [packageRoot.appendingPathComponent("Resources/AppIcon.png")]
            + ["icon_16x16.png", "icon_32x32.png", "icon_512x512@2x.png"].map { set.appendingPathComponent($0) }
        for file in files {
            let source = try XCTUnwrap(CGImageSourceCreateWithURL(file as CFURL, nil))
            let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
            let alpha = image.alphaInfo
            XCTAssertTrue(alpha == .none || alpha == .noneSkipLast || alpha == .noneSkipFirst,
                          "\(file.lastPathComponent) has an alpha channel, so macOS 26 plates it grey")
        }
    }

    func testTheHandTunedSmallSizesAreCommitted() throws {
        let set = packageRoot.appendingPathComponent("Resources/AppIcon.iconset")
        for name in ["icon_16x16.png", "icon_16x16@2x.png", "icon_32x32.png", "icon_32x32@2x.png",
                     "icon_128x128.png", "icon_128x128@2x.png", "icon_256x256.png",
                     "icon_256x256@2x.png", "icon_512x512.png", "icon_512x512@2x.png"] {
            XCTAssertTrue(FileManager.default.fileExists(atPath: set.appendingPathComponent(name).path),
                          "missing \(name)")
        }
    }

    func testTheBundlesSixteenPixelIconIsTheHandTunedOne() throws {
        let script = packageRoot.appendingPathComponent("Scripts/embed-icon.sh").path
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("AgentDeckIconFace-\(UUID().uuidString)")
        let app = scratch.appendingPathComponent("Fake.app")
        try FileManager.default.createDirectory(
            at: app.appendingPathComponent("Contents/Resources"), withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }

        func run(_ exe: String, _ args: [String]) throws -> Int32 {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: exe)
            p.arguments = args
            p.standardOutput = FileHandle.nullDevice
            p.standardError = FileHandle.nullDevice
            try p.run()
            p.waitUntilExit()
            return p.terminationStatus
        }
        XCTAssertEqual(try run("/bin/bash", [script, app.path]), 0)
        let back = scratch.appendingPathComponent("back.iconset")
        XCTAssertEqual(try run("/usr/bin/iconutil", [
            "-c", "iconset", "-o", back.path,
            app.appendingPathComponent("Contents/Resources/AppIcon.icns").path]), 0)

        let shipped = try rgba(back.appendingPathComponent("icon_16x16.png"))
        let tuned = try rgba(packageRoot.appendingPathComponent("Resources/AppIcon.iconset/icon_16x16.png"))
        XCTAssertEqual(shipped.width, 16)
        // Opaque pixels only: iconutil re-encodes the small slots, and the
        // anti-aliased rim under near-zero alpha does not survive. Measured
        // on the full-bleed artwork: one pixel of 256 (the last one) comes
        // back off by 15; every other one matches. A sips shrink of the
        // master moves most of them by up to 232.
        var off = 0
        for px in stride(from: 0, to: min(shipped.bytes.count, tuned.bytes.count), by: 4)
        where shipped.bytes[px + 3] == 255 && tuned.bytes[px + 3] == 255 {
            if (0..<3).contains(where: { abs(Int(shipped.bytes[px + $0]) - Int(tuned.bytes[px + $0])) > 3 }) {
                off += 1
            }
        }
        XCTAssertLessThanOrEqual(off, 2, "\(off) of the .icns 16px pixels are not the hand-tuned artwork")
    }
}

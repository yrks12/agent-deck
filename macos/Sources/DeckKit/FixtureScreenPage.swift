import Foundation
import CoreGraphics
import CoreText
import ImageIO

/// **A drawn computer, for documentation screenshots.** Never a captured frame:
/// every word here is made up (Acme, Sam Carter, `example.com`). Only the fixture
/// uses it, and only when asked (`FixtureDeckClient(demoScreenPage: true)`).
enum FixtureScreenPage {

    static func jpeg(width: Int, height: Int) -> Data {
        let space = CGColorSpaceCreateDeviceRGB()
        guard let ctx = CGContext(
            data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
            space: space, bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else { return Data() }

        // Top-left origin, like a page.
        ctx.translateBy(x: 0, y: CGFloat(height))
        ctx.scaleBy(x: 1, y: -1)

        func fill(_ white: CGFloat, _ r: CGRect) {
            ctx.setFillColor(CGColor(gray: white, alpha: 1)); ctx.fill(r)
        }
        func text(_ s: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat, _ color: CGColor, bold: Bool = false) {
            let font = CTFontCreateWithName((bold ? "Helvetica-Bold" : "Helvetica") as CFString, size, nil)
            let attrs: [CFString: Any] = [kCTFontAttributeName: font, kCTForegroundColorAttributeName: color]
            let line = CTLineCreateWithAttributedString(
                CFAttributedStringCreate(nil, s as CFString, attrs as CFDictionary))
            ctx.saveGState()
            ctx.textMatrix = CGAffineTransform(scaleX: 1, y: -1)
            ctx.textPosition = CGPoint(x: x, y: y + size)
            CTLineDraw(line, ctx)
            ctx.restoreGState()
        }
        let black = CGColor(gray: 0.1, alpha: 1), grey = CGColor(gray: 0.4, alpha: 1)
        let blue = CGColor(red: 0.1, green: 0.4, blue: 0.85, alpha: 1), white = CGColor(gray: 1, alpha: 1)
        let W = CGFloat(width)

        fill(0.95, CGRect(x: 0, y: 0, width: W, height: CGFloat(height)))

        // A page with no browser chrome: the screen panel draws its own bar over the top.
        fill(1, CGRect(x: 440, y: 200, width: 400, height: 460))
        text("Acme", 596, 232, 32, blue, bold: true)
        text("Welcome back", 560, 292, 26, black)
        text("sam.carter@example.com", 548, 338, 15, grey)
        text("Enter your password", 470, 470, 13, grey)
        ctx.setStrokeColor(blue); ctx.setLineWidth(2)
        ctx.stroke(CGRect(x: 470, y: 495, width: 340, height: 48))
        text("Forgot password?", 470, 580, 14, blue)
        ctx.setFillColor(blue); ctx.fill(CGRect(x: 730, y: 600, width: 80, height: 36))
        text("Next", 752, 609, 15, white)

        guard let image = ctx.makeImage() else { return Data() }
        let out = NSMutableData()
        guard let dest = CGImageDestinationCreateWithData(out, "public.jpeg" as CFString, 1, nil) else { return Data() }
        CGImageDestinationAddImage(dest, image, [kCGImageDestinationLossyCompressionQuality: 0.85] as CFDictionary)
        CGImageDestinationFinalize(dest)
        return out as Data
    }
}

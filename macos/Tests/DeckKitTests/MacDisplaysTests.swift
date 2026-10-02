import XCTest
@testable import DeckKit

/// **His Mac has more than one display, and every one must be reachable.**
///
/// MEASURED 2026-10-02 on his MacBook Pro: Built-in Retina at (0,0) 1512x982
/// points (3024x1964 px, scale 2) plus a PM1561P at (1512,0) 1920x1080 at
/// scale 1. The live view and every click were pinned to the main display.
///
/// One pixel of a picture of display N becomes a global CGEvent point using
/// THAT display's origin and size, for every arrangement macOS allows: side by
/// side, above (negative y), to the left (negative x), Retina next to a
/// non-Retina monitor. A display that is gone maps to nothing, never to
/// another display.
final class MacDisplaysTests: XCTestCase {
    private let builtIn = MacDisplayInfo(id: 1, name: "Built-in Retina Display", widthPx: 3024, heightPx: 1964,
                                         scale: 2, originX: 0, originY: 0, isMain: true)

    private func external(id: Int = 3, name: String = "PM1561P", x: Double, y: Double,
                          w: Int = 1920, h: Int = 1080, scale: Double = 1) -> MacDisplayInfo {
        MacDisplayInfo(id: id, name: name, widthPx: w, heightPx: h, scale: scale, originX: x, originY: y, isMain: false)
    }

    func testAPixelOfEachDisplayLandsOnThatDisplay() {
        struct Row {
            let what: String
            let displays: [MacDisplayInfo]
            let display: Int?
            let x: Int, y: Int
            let space: MacSpace
            let want: (Double, Double)
        }
        let right = external(x: 1512, y: 0)
        let above = external(x: 0, y: -1080)
        let left = external(x: -1920, y: 0)
        let retinaLeft = external(id: 5, name: "LG UltraFine", x: -2560, y: -200, w: 5120, h: 2880, scale: 2)
        let rows: [Row] = [
            Row(what: "main, live view in points", displays: [builtIn, right], display: nil,
                x: 756, y: 491, space: MacSpace(width: 1512, height: 982), want: (756, 491)),
            Row(what: "main, agent screenshot in Retina pixels", displays: [builtIn, right], display: 1,
                x: 3024 / 2, y: 1964 / 2, space: MacSpace(width: 3024, height: 1964), want: (756, 491)),
            Row(what: "side by side: second display's origin", displays: [builtIn, right], display: 3,
                x: 0, y: 0, space: MacSpace(width: 1920, height: 1080), want: (1512, 0)),
            Row(what: "side by side: live view capped at 1600 wide", displays: [builtIn, right], display: 3,
                x: 800, y: 450, space: MacSpace(width: 1600, height: 900), want: (1512 + 960, 540)),
            Row(what: "above: negative y", displays: [builtIn, above], display: 3,
                x: 960, y: 0, space: MacSpace(width: 1920, height: 1080), want: (960, -1080)),
            Row(what: "above: bottom edge meets the main display", displays: [builtIn, above], display: 3,
                x: 0, y: 1079, space: MacSpace(width: 1920, height: 1080), want: (0, -1)),
            Row(what: "left: negative x", displays: [builtIn, left], display: 3,
                x: 1919, y: 540, space: MacSpace(width: 1920, height: 1080), want: (-1, 540)),
            Row(what: "Retina monitor to the left and above, in its pixels", displays: [builtIn, retinaLeft],
                display: 5, x: 2560, y: 1440, space: MacSpace(width: 5120, height: 2880), want: (-1280, 520)),
            Row(what: "Retina monitor, live view in its points", displays: [builtIn, retinaLeft],
                display: 5, x: 0, y: 0, space: MacSpace(width: 1600, height: 900), want: (-2560, -200)),
        ]
        for row in rows {
            let p = MacDisplays.point(x: row.x, y: row.y, space: row.space, display: row.display, in: row.displays)
            XCTAssertNotNil(p, row.what)
            XCTAssertEqual(p?.x ?? .nan, row.want.0, accuracy: 0.01, row.what)
            XCTAssertEqual(p?.y ?? .nan, row.want.1, accuracy: 0.01, row.what)
        }
    }

    func testAGoneDisplayMapsToNothing() {
        XCTAssertNil(MacDisplays.point(x: 1, y: 1, space: MacSpace(width: 10, height: 10), display: 3,
                                       in: [builtIn]))
        XCTAssertEqual(MacDisplays.resolve(nil, in: [external(x: 1512, y: 0), builtIn])?.id, 1)
        XCTAssertNil(MacDisplays.resolve(7, in: [builtIn]))
    }

    func testFrameIsGlobalPoints() {
        XCTAssertEqual(builtIn.frame, MacDisplayFrame(x: 0, y: 0, width: 1512, height: 982))
        XCTAssertEqual(external(id: 5, x: -2560, y: -200, w: 5120, h: 2880, scale: 2).frame,
                       MacDisplayFrame(x: -2560, y: -200, width: 2560, height: 1440))
    }

    func testDisplaysAreNumberedMainFirstThenLeftToRight() {
        let left = external(id: 4, name: "LG HDR 4K", x: -1920, y: 0)
        let right = external(id: 3, x: 1512, y: 0)
        let all = [right, left, builtIn]
        XCTAssertEqual(MacDisplays.ordered(all).map(\.id), [1, 4, 3])
        XCTAssertEqual(MacDisplays.label(builtIn, in: all), "Display 1 · Built-in")
        XCTAssertEqual(MacDisplays.label(left, in: all), "Display 2 · LG HDR 4K")
        XCTAssertEqual(MacDisplays.label(right, in: all), "Display 3 · PM1561P")
    }

    func testTheWireShapeIsSnakeCase() throws {
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(builtIn)) as? [String: Any]
        XCTAssertEqual(Set(json?.keys ?? [:].keys),
                       ["id", "name", "width_px", "height_px", "scale", "origin_x", "origin_y", "is_main"])
        let back = try JSONDecoder().decode(MacDisplayInfo.self, from: JSONEncoder().encode(builtIn))
        XCTAssertEqual(back, builtIn)
    }

    func testThePollCarriesEveryDisplayAndTheMainScreen() throws {
        var body = MacPollRequest(wait: 1, freeSlots: 1, running: [], mode: .full, grants: [:])
        body.screen = MacSpace(width: 1512, height: 982)
        body.displays = [builtIn, external(x: 1512, y: 0)]
        let json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual((json?["displays"] as? [[String: Any]])?.count, 2)
        XCTAssertEqual((json?["screen"] as? [String: Any])?["width"] as? Int, 1512)
    }

    func testThePollSaysWhichDisplaysAreWatched() throws {
        let r = try JSONDecoder().decode(MacPollResponse.self,
                                         from: Data(#"{"jobs":[],"watch":true,"watch_displays":[1,3]}"#.utf8))
        XCTAssertEqual(r.watchDisplays, [1, 3])
        let old = try JSONDecoder().decode(MacPollResponse.self, from: Data(#"{"jobs":[],"watch":true}"#.utf8))
        XCTAssertEqual(old.watchDisplays, [])
    }

    func testAGestureAndAScreenshotNameTheirDisplay() throws {
        let args = try JSONDecoder().decode(MacJobArgs.self, from: Data(
            #"{"action":"move","x":5,"y":6,"space":{"width":1920,"height":1080,"display":3}}"#.utf8))
        XCTAssertEqual(args.space?.display, 3)
        XCTAssertEqual(try MacInputGesture.parse(args),
                       .move(x: 5, y: 6, space: MacSpace(width: 1920, height: 1080, display: 3)))
        let shot = try JSONDecoder().decode(MacJobArgs.self, from: Data(#"{"display":3}"#.utf8))
        XCTAssertEqual(shot.display, 3)
    }
}

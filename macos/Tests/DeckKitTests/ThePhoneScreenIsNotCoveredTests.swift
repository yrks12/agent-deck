import XCTest
@testable import DeckKit

/// **"The UI blocks stuff"**, on the phone. The stage started at the top of
/// the phone and the toolbar floated over it; the status line floated over
/// its bottom. The stage now starts under the toolbar and stops above the
/// status line and the key bar (`ScreenStageLayout.uncovered`), and the
/// picture is clipped to it, so no control is ever drawn over the desk.
final class ThePhoneScreenIsNotCoveredTests: XCTestCase {

    /// Portrait and landscape iPhones: (width, height, toolbar's bottom).
    static let phones: [(Double, Double, Double)] = [(402, 874, 106), (874, 402, 50), (393, 852, 103)]

    func testTheStageIntersectsNoControl() {
        for (w, h, bar) in Self.phones {
            for keys in [0.0, 300] {
                for status in [0.0, ScreenStageLayout.statusHeight] {
                    let s = ScreenStageLayout.uncovered(viewWidth: w, viewHeight: h, coveredTop: bar,
                                                        coveredBottom: keys, statusHeight: status)
                    let stageTop = s.top, stageBottom = s.top + s.area.stageHeight
                    XCTAssertGreaterThanOrEqual(stageTop, bar, "\(w)x\(h): under the toolbar")
                    XCTAssertLessThanOrEqual(stageBottom + status, h - keys + 0.001,
                                             "\(w)x\(h): under the status line or the key bar")
                    XCTAssertGreaterThan(s.area.stageHeight, 0)
                    XCTAssertEqual(s.area.bandTop, 0, "nothing covers the stage's own top")
                    XCTAssertEqual(s.area.bandBottom, s.area.stageHeight)
                }
            }
        }
    }

    func testControlsHiddenTheStageIsTheWholePhone() {
        let s = ScreenStageLayout.uncovered(viewWidth: 874, viewHeight: 402, coveredTop: 0,
                                            coveredBottom: 0, statusHeight: 0)
        XCTAssertEqual(s.top, 0)
        XCTAssertEqual(s.area.stageHeight, 402)
        XCTAssertFalse(s.area.isCovered)
    }
}

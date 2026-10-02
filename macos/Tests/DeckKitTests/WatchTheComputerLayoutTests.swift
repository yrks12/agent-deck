import XCTest
import CoreGraphics
@testable import DeckKit

/// **"On desktop, make the view bigger so we can see what the agents do."**
///
/// The layout of the agent's computer — the screen, its terminal, both side by
/// side, and Watch mode where the computer takes the main window and the chat
/// becomes a side panel — decided as numbers, so no size is a guess in a view.
final class WatchTheComputerLayoutTests: XCTestCase {

    // MARK: the 90%

    func testTheSheetAndTheExpandWindowOpenAtNinetyPercentOfTheScreen() {
        let size = ComputerLayout.defaultWindowSize(visibleFrame: CGSize(width: 1728, height: 1079))
        XCTAssertEqual(size.width, 1555, accuracy: 1)
        XCTAssertEqual(size.height, 971, accuracy: 1)
    }

    func testTheSheetIsNeverBiggerThanTheWindowItHangsFrom() {
        let sheet = ComputerLayout.sheetSize(visibleFrame: CGSize(width: 1728, height: 1079),
                                             window: CGSize(width: 1208, height: 949))
        XCTAssertLessThanOrEqual(sheet.width, 1208)
        XCTAssertLessThanOrEqual(sheet.height, 949)
        // In a window as big as the screen, the screen's 90% is what it gets.
        let big = ComputerLayout.sheetSize(visibleFrame: CGSize(width: 1728, height: 1079),
                                           window: CGSize(width: 1728, height: 1079))
        XCTAssertEqual(big.width, 1555, accuracy: 1)
    }

    // MARK: fitting the picture

    func testTheScreenIsFittedInItsOwnShapeAndCentred() {
        // A 1600x1000 desktop in a 1000x1000 area: full width, letterboxed.
        let rect = ComputerLayout.fit(content: CGSize(width: 1600, height: 1000),
                                      in: CGSize(width: 1000, height: 1000))
        XCTAssertEqual(rect.width, 1000, accuracy: 0.5)
        XCTAssertEqual(rect.height, 625, accuracy: 0.5)
        XCTAssertEqual(rect.midY, 500, accuracy: 0.5)
        XCTAssertEqual(ComputerLayout.fit(content: .zero, in: CGSize(width: 10, height: 10)), .zero)
    }

    // MARK: Screen | Terminal | Both

    func testScreenAndTerminalAloneTakeTheWholeArea() {
        let area = CGSize(width: 1400, height: 900)
        let screen = ComputerLayout.split(.screen, area: area, display: CGSize(width: 1600, height: 1000))
        XCTAssertEqual(screen.screen.size, area)
        XCTAssertEqual(screen.terminal, .zero)
        let terminal = ComputerLayout.split(.terminal, area: area, display: CGSize(width: 1600, height: 1000))
        XCTAssertEqual(terminal.terminal.size, area)
        XCTAssertEqual(terminal.screen, .zero)
    }

    func testBothSitSideBySideInAWideAreaAndTheTerminalStaysUsable() {
        let area = CGSize(width: 1600, height: 900)
        let both = ComputerLayout.split(.both, area: area, display: CGSize(width: 1600, height: 1000))
        XCTAssertEqual(both.axis, .horizontal)
        XCTAssertGreaterThanOrEqual(both.terminal.width, ComputerLayout.minTerminalWidth)
        XCTAssertGreaterThan(both.screen.width, both.terminal.width, "the screen keeps the larger share")
        XCTAssertEqual(both.screen.maxX, both.terminal.minX, accuracy: 0.5)
        XCTAssertEqual(both.terminal.maxX, area.width, accuracy: 0.5)
        XCTAssertEqual(both.screen.height, area.height)
    }

    func testBothStackInATallArea() {
        let area = CGSize(width: 700, height: 1100)
        let both = ComputerLayout.split(.both, area: area, display: CGSize(width: 1600, height: 1000))
        XCTAssertEqual(both.axis, .vertical)
        XCTAssertEqual(both.screen.width, area.width)
        XCTAssertGreaterThanOrEqual(both.terminal.height, ComputerLayout.minTerminalHeight)
        XCTAssertEqual(both.screen.maxY, both.terminal.minY, accuracy: 0.5)
        XCTAssertEqual(both.terminal.maxY, area.height, accuracy: 0.5)
    }

    func testTheShareFollowsTheDisplaysShapeNotAHardcodedSize() {
        let area = CGSize(width: 2400, height: 900)
        let old = ComputerLayout.split(.both, area: area, display: CGSize(width: 1280, height: 800))
        let wider = ComputerLayout.split(.both, area: area, display: CGSize(width: 1920, height: 900))
        XCTAssertGreaterThan(wider.screen.width, old.screen.width)
    }

    // MARK: Watch mode's chat panel

    func testTheChatPanelIsASideColumnOrNothing() {
        XCTAssertEqual(ComputerLayout.chatPanelWidth(expanded: false, windowWidth: 1600), 0)
        let open = ComputerLayout.chatPanelWidth(expanded: true, windowWidth: 1600)
        XCTAssertGreaterThanOrEqual(open, ComputerLayout.chatPanelRange.lowerBound)
        XCTAssertLessThanOrEqual(open, ComputerLayout.chatPanelRange.upperBound)
        XCTAssertLessThanOrEqual(open, 1600 * 0.34, "the computer keeps most of the window")
        // A small window still gets a usable chat column.
        XCTAssertEqual(ComputerLayout.chatPanelWidth(expanded: true, windowWidth: 900),
                       ComputerLayout.chatPanelRange.lowerBound)
    }

    func testTheViewsHaveNames() {
        XCTAssertEqual(ComputerView.allCases.map(\.title), ["Screen", "Terminal", "Both"])
    }
}

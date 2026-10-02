import XCTest
import ImageIO
@testable import DeckKit

/// The fixture refuses a frame on purpose: a captured frame is a photograph of a
/// signed-in browser and never goes into a repository. Documentation screenshots
/// still need the agent's screen to show *something*, so `demoScreenPage` makes
/// the fixture draw a made-up sign-in page (neutral names, `example.com`) instead.
/// Off by default: every other caller keeps the honest `no_frame` refusal.
final class FixtureScreenPageTests: XCTestCase {

    func testTheDefaultFixtureStillRefusesAFrame() async {
        do {
            _ = try await FixtureDeckClient().screenFrame(agent: "chief")
            XCTFail("the default fixture must not answer a frame")
        } catch {
            XCTAssertTrue("\(error)".contains("noFrame"), "expected the no_frame refusal, got \(error)")
        }
    }

    func testTheDemoFixtureDrawsAMadeUpPageAtTheReportedSize() async throws {
        let client = FixtureDeckClient(demoScreenPage: true)
        let frame = try await client.screenFrame(agent: "chief")
        let status = try await client.screenStatus(agent: "chief")

        let source = try XCTUnwrap(CGImageSourceCreateWithData(frame.jpeg as CFData, nil))
        let image = try XCTUnwrap(CGImageSourceCreateImageAtIndex(source, 0, nil))
        XCTAssertEqual(image.width, status.width)
        XCTAssertEqual(image.height, status.height)
    }

    func testOnlyAComputerThatIsUpHasAPage() async {
        do {
            _ = try await FixtureDeckClient(demoScreenPage: true).screenFrame(agent: "ledger")
            XCTFail("a desk with no computer has no page")
        } catch {
            XCTAssertTrue("\(error)".contains("noComputerYet"), "got \(error)")
        }
    }
}

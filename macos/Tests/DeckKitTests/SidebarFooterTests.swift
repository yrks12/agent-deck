import XCTest
@testable import DeckKit

/// The bottom of the sidebar: the Connectors & Skills row and who is signed in, the way the
/// reference lays it out. The only thing worth pinning is that neither row
/// claims something this deck cannot do.
final class SidebarFooterTests: XCTestCase {

    func testTheFooterNamesWhoIsSignedInOnThisMac() {
        let footer = SidebarFooter.make()

        XCTAssertEqual(footer.accountTitle, "Owner")
        XCTAssertEqual(footer.accountDetail, "Signed in on this Mac")
    }

    /// The old row said "Plugins — None". The deck now has a store for
    /// connectors and skills, so the row names it and opens it.
    func testTheBottomRowOpensConnectorsAndSkills() {
        let footer = SidebarFooter.make()

        XCTAssertEqual(footer.storeTitle, "Connectors & Skills")
        XCTAssertEqual(footer.storeDetail, "Add tools and skills to your desks")
        XCTAssertTrue(footer.storeIsAvailable)
    }

    func testTheOwnerIsTheDecksOwnerNotAHardCodedString() {
        XCTAssertEqual(SidebarFooter.make(owner: "sam").accountTitle, "Sam")
        XCTAssertEqual(SidebarFooter.make(owner: DeckOwner.name).accountTitle, "Owner")
    }
}

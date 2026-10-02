import XCTest
@testable import DeckKit

/// The Connect screen's logic, shared by the iPhone and the Mac so the two
/// cannot disagree about when Connect is enabled or what a failure says.
final class ConnectFormTests: XCTestCase {
    private var suiteName = ""
    private var defaults: UserDefaults!
    private var tokens: InMemoryTokenStore!

    override func setUp() {
        suiteName = "dev.agentdeck.app.test\(UUID().uuidString.prefix(8))"
        defaults = UserDefaults(suiteName: suiteName)
        tokens = InMemoryTokenStore()
    }
    override func tearDown() { defaults.removePersistentDomain(forName: suiteName) }

    private var deck: DeckConnection { DeckConnection(defaults: defaults, tokens: tokens) }

    func testReadinessFollowsTheVisibleFieldsOnly() {
        var form = ConnectForm()
        XCTAssertEqual(form.mode, .code)
        XCTAssertFalse(form.isReady)
        form.code = "  \n "
        XCTAssertFalse(form.isReady)
        form.code = "ADK1.x"
        XCTAssertTrue(form.isReady)

        form.mode = .manual
        XCTAssertFalse(form.isReady)           // the code does not count here
        form.address = "10.0.0.1:7789"
        XCTAssertFalse(form.isReady)
        form.token = " adt_x "
        XCTAssertTrue(form.isReady)
    }

    func testLaunchArgumentsPrefillTheCodeButDoNotSubmit() {
        let form = ConnectForm(launchArguments: ["app", "--pair", "ADK1.abc"])
        XCTAssertEqual(form.mode, .code)
        XCTAssertEqual(form.code, "ADK1.abc")
        XCTAssertEqual(ConnectForm(launchArguments: ["app"]).code, "")
    }

    func testManualSubmitVerifiesThenSaves() async throws {
        var form = ConnectForm(mode: .manual)
        form.address = "10.0.0.1:7789"; form.token = "adt_secret"
        let seen = Box<ManualDeckEntry>()
        try await form.submit(on: deck, device: "Mac") { seen.value = $0 }
        XCTAssertEqual(seen.value?.url.absoluteString, "http://10.0.0.1:7789")
        XCTAssertEqual(deck.saved?.url.absoluteString, "http://10.0.0.1:7789")
        XCTAssertEqual(try tokens.token(), "adt_secret")
    }

    func testManualSubmitKeepsNothingWhenVerificationFails() async {
        var form = ConnectForm(mode: .manual)
        form.address = "10.0.0.1:7789"; form.token = "adt_secret"
        do {
            try await form.submit(on: deck, device: "Mac") { _ in throw DeckError.unauthorized }
            XCTFail("should throw")
        } catch {
            XCTAssertEqual(error as? DeckError, .unauthorized)
        }
        XCTAssertNil(deck.saved)
        XCTAssertNil(try tokens.token())
    }

    func testBadManualInputThrowsTheProblemBeforeAnyVerification() async {
        var form = ConnectForm(mode: .manual)
        form.address = "10.0.0.1"; form.token = ""
        do {
            try await form.submit(on: deck, device: "Mac") { _ in XCTFail("must not verify") }
            XCTFail("should throw")
        } catch {
            XCTAssertEqual(error as? ManualDeckEntry.Problem, .noToken)
        }
    }

    func testAGarbageCodeFailsLocallyWithoutTouchingTheNetwork() async {
        var form = ConnectForm()
        form.code = "not a code"
        do {
            try await form.submit(on: deck, device: "Mac")
            XCTFail("should throw")
        } catch {
            XCTAssertEqual(error as? PairingFailure, .code(.malformed))
        }
    }

    func testProblemTextNamesEachKindOfFailureInItsOwnWords() {
        XCTAssertEqual(ConnectForm.problemText(for: PairingFailure.expired), PairingFailure.expired.userFacingText)
        XCTAssertEqual(ConnectForm.problemText(for: ManualDeckEntry.Problem.noAddress),
                       ManualDeckEntry.Problem.noAddress.userFacingText)
        XCTAssertEqual(ConnectForm.problemText(for: DeckError.unauthorized), DeckError.unauthorized.userFacingText)
    }
}

private final class Box<T>: @unchecked Sendable { var value: T? }

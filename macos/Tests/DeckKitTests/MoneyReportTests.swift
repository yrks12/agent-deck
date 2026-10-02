import XCTest
@testable import DeckKit

/// **The Money screen's decisions** (`GET /v1/money`). DeckKit decides what is
/// shown and in what words; DeckUI only draws it. Premise (measured against
/// the route's documented body): every key but `state` may be absent, and
/// `roi` / `flag` may be null.
final class MoneyReportTests: XCTestCase {

    private func decode(_ json: String) throws -> MoneyReport {
        try DeckCoding.decoder.decode(MoneyReport.self, from: Data(json.utf8))
    }

    private let ready = #"""
    {"state":"ready","currency":"GBP","generated_at":1790000000.0,"fx_assumed":true,"flag_days":14,
     "totals":{"revenue":10.0,"costs":812.3,"net":-802.3,"mrr":0.0},
     "companies":[
       {"name":"Shaliach HQ","started_at":1790000000.0,"revenue":0.0,"mrr":0.0,"desks":["cos"],
        "costs":{"cloud":0.0,"providers":0.0,"distributor":0.0,"other":0.0,"claude":100.0},
        "cost_total":100.0,"net":-100.0,"roi":null,"claude":{"tokens":10,"api_usd":130.0},"overhead":true},
       {"name":"Acme","started_at":1790000000.0,"revenue":10.0,"mrr":0.0,"desks":["acme-eng"],
        "costs":{"cloud":0.0,"providers":0.0,"distributor":0.0,"other":0.0,"claude":512.1},
        "cost_total":512.1,"net":-502.1,"roi":-0.98,"claude":{"tokens":123456,"api_usd":682.8},"overhead":false}],
     "experiments":[
       {"desk":"globex-growth","company":"Globex","started_at":1790000000.0,"days":26,"revenue":0.0,"cost":9.1,"roi":null,"signal":false,"flag":"no_revenue_14d"},
       {"desk":"ok-desk","company":"Globex","started_at":1790000000.0,"days":3,"revenue":5.0,"cost":1.0,"roi":4.0,"signal":true,"flag":null}],
     "unattributed":{"revenue":0.0},
     "connect":[
       {"source":"stripe","state":"connected","title":"Connect Stripe","detail":"done"},
       {"source":"cloud","state":"missing","title":"Connect Cloud billing","detail":"In Google Cloud Billing, turn on export."}],
     "claude":[{"desk":"acme-eng","company":"Acme","tokens":1258900000,"api_usd":314.75}],
     "stripe":[{"label":"example","state":"connected","payouts_30d":0.0,"balance":{"available":-0.49,"pending":9.65}}]}
    """#

    func testTheFullBodyReads() throws {
        let r = try decode(ready)
        XCTAssertEqual(r.state, .ready)
        XCTAssertEqual(r.currency, "GBP")
        XCTAssertTrue(r.fxAssumed)
        XCTAssertEqual(r.flagDays, 14)
        XCTAssertEqual(r.totals?.costs, 812.3)
        XCTAssertEqual(r.companies.count, 2)
        XCTAssertEqual(r.companies[1].claude?.apiUsd, 682.8)
        XCTAssertEqual(r.experiments.first?.flag, "no_revenue_14d")
        XCTAssertNil(r.experiments.first?.roi)
        XCTAssertEqual(r.stripe.first?.balance?.pending, 9.65)
    }

    func testAWarmingBodyWithOnlyStateReads() throws {
        let r = try decode(#"{"state":"warming","companies":[],"experiments":[],"connect":[]}"#)
        XCTAssertTrue(r.isWarming)
        XCTAssertNil(r.totals)
        XCTAssertEqual(r.currency, "GBP", "a body with no currency is pounds, the deck's default")
        XCTAssertFalse(r.fxAssumed)
        XCTAssertEqual(r.flagDays, 14)
    }

    func testAnUnknownStateIsNotReadyAndNeverCrashes() throws {
        XCTAssertTrue(try decode(#"{"state":"something-new"}"#).isWarming)
        XCTAssertTrue(try decode("{}").isWarming)
    }

    func testMoneyIsWrittenInTheDecksCurrency() {
        XCTAssertEqual(MoneyPresentation.amount(10, currency: "GBP"), "£10.00")
        XCTAssertEqual(MoneyPresentation.amount(-802.3, currency: "GBP"), "-£802.30")
        XCTAssertEqual(MoneyPresentation.amount(1234.5, currency: "GBP"), "£1,234.50")
        XCTAssertEqual(MoneyPresentation.amount(3, currency: "USD"), "$3.00")
        XCTAssertEqual(MoneyPresentation.amount(0, currency: "GBP"), "£0.00")
    }

    func testRoiIsAPercentOrADash() {
        XCTAssertEqual(MoneyPresentation.roi(-0.98), "-98%")
        XCTAssertEqual(MoneyPresentation.roi(4.0), "+400%")
        XCTAssertEqual(MoneyPresentation.roi(0), "0%")
        XCTAssertEqual(MoneyPresentation.roi(nil), "—")
        XCTAssertEqual(MoneyPresentation.roiTone(-0.98), .bad)
        XCTAssertEqual(MoneyPresentation.roiTone(0.5), .good)
        XCTAssertEqual(MoneyPresentation.roiTone(nil), .none)
    }

    func testOverheadCompaniesComeLast() throws {
        let r = try decode(ready)
        XCTAssertEqual(MoneyPresentation.companies(r).map(\.name), ["Acme", "Shaliach HQ"])
    }

    func testOnlyFlaggedExperimentsAreListed() throws {
        let r = try decode(ready)
        XCTAssertEqual(MoneyPresentation.flagged(r).map(\.desk), ["globex-growth"])
        XCTAssertEqual(MoneyPresentation.flaggedHeading(r), "No sales after 14 days")
    }

    func testOnlyASourceThatIsNotConnectedGetsACard() throws {
        let r = try decode(ready)
        XCTAssertEqual(MoneyPresentation.connectCards(r).map(\.source), ["cloud"])
        XCTAssertEqual(MoneyPresentation.connectCards(r).first?.title, "Connect Cloud billing")
    }

    func testTheFootnotes() throws {
        let r = try decode(ready)
        XCTAssertEqual(MoneyPresentation.footnotes(r),
                       ["Costs include Claude work at API prices", "Exchange rates approximate"])
        let exact = try decode(#"{"state":"ready","fx_assumed":false}"#)
        XCTAssertEqual(MoneyPresentation.footnotes(exact), ["Costs include Claude work at API prices"])
    }

    func testClaudeWorkReadsInTheDecksCurrencyWithAnApproximateSign() throws {
        let r = try decode(ready)
        XCTAssertEqual(MoneyPresentation.claudeWork(r.companies[1], currency: "GBP"), "Claude work ≈ £512.10")
        let none = try decode(#"{"state":"ready","companies":[{"name":"A"}]}"#)
        XCTAssertNil(MoneyPresentation.claudeWork(none.companies[0], currency: "GBP"))
    }
}

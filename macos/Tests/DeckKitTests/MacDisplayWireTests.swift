import XCTest
@testable import DeckKit

/// **The viewer's wire for his Mac's displays (2026-10-02).**
///
/// MEASURED: with a second monitor on his MacBook Pro, the phone and the Mac
/// app asked only for "the screen" and got the built-in display; there was
/// no way to name another one.
///
/// * The status lists the displays and says which one it is about, and why
///   not the one asked for (it was unplugged).
/// * `mac:<node>@<display>` names one display; the routes stay the Mac's and
///   carry `?display=<display>` on `screen`, `screen.jpg` and `screen/input`.
/// * A frame says which display it is.
final class MacDisplayWireTests: XCTestCase {
    static let twoDisplays = #"""
    {"desk":"MacBook Pro","display":"MacBook Pro","computer":{"running":true,"image":"macOS",
     "container":"mac_1"},"width":1512,"height":982,"stale_after":10,"generated_at":1,
     "display_id":1,"display_note":"","displays":[
      {"id":1,"name":"Built-in Retina Display","width_px":3024,"height_px":1964,"scale":2,
       "origin_x":0,"origin_y":0,"is_main":true,"label":"Display 1 · Built-in"},
      {"id":3,"name":"PM1561P","width_px":1920,"height_px":1080,"scale":1,
       "origin_x":1512,"origin_y":0,"is_main":false,"label":"Display 2 · PM1561P"}]}
    """#

    static func status(_ json: String = twoDisplays, edit: (inout [String: Any]) -> Void = { _ in })
        throws -> AgentScreenStatus {
        var obj = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(json.utf8)) as? [String: Any])
        edit(&obj)
        return try DeckCoding.decoder.decode(AgentScreenStatus.self,
                                             from: JSONSerialization.data(withJSONObject: obj))
    }

    // MARK: the wire

    func testTheStatusCarriesTheDisplays() throws {
        let s = try Self.status()
        XCTAssertEqual(s.macDisplays.map(\.id), [1, 3])
        XCTAssertEqual(s.macDisplayId, 1)
        XCTAssertEqual(s.macDisplayNote, "")
        XCTAssertEqual(MacDisplays.label(s.macDisplays[1], in: s.macDisplays), "Display 2 · PM1561P")
    }

    func testTheNameCarriesTheDisplayAndTheRoutesStayTheMacs() {
        let name = MacScreenName.agent(nodeId: "mac_1", display: 3)
        XCTAssertEqual(MacScreenName.nodeId(name), "mac_1")
        XCTAssertEqual(MacScreenName.display(name), 3)
        XCTAssertEqual(MacScreenName.base(name), "/v1/nodes/mac_1")
        XCTAssertEqual(MacScreenName.agent(nodeId: "mac_1", display: nil), "mac:mac_1")
        XCTAssertNil(MacScreenName.display("atlas"))
    }

    func testEveryRouteAsksForThePickedDisplay() async throws {
        let performer = RecordingStubPerformer()
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.local")!, tokens: tokens, performer: performer)
        let name = MacScreenName.agent(nodeId: "mac_1", display: 3)
        _ = try? await client.screenStatus(agent: name)
        _ = try? await client.screenFrame(agent: name)
        try? await client.sendScreenInput(agent: name, .click(x: 1, y: 2))
        XCTAssertEqual(performer.urls.map { $0.path }, ["/v1/nodes/mac_1/screen", "/v1/nodes/mac_1/screen.jpg",
                                                         "/v1/nodes/mac_1/screen/input"])
        XCTAssertEqual(performer.urls.map { $0.query }, ["display=3", "display=3", "display=3"])
        _ = try? await client.screenFrame(agent: "mac:mac_1")
        XCTAssertNil(performer.urls.last?.query, "main: no parameter, as before")
    }

    func testTheFrameSaysWhichDisplayItIs() async throws {
        let performer = HeaderStubPerformer(headers: ["x-frame-age": "0.1", "x-frame-display-id": "3"])
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "http://deck.local")!, tokens: tokens, performer: performer)
        let frame = try await client.screenFrame(agent: "mac:mac_1@3")
        XCTAssertEqual(frame.displayId, 3)
    }
}

/// Answers every request with an empty 200 and records the URL.
final class RecordingStubPerformer: RequestPerformer, @unchecked Sendable {
    private let lock = NSLock()
    private var _urls: [URL] = []
    var urls: [URL] { lock.withLock { _urls } }

    func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        lock.withLock { _urls.append(request.url!) }
        return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: "HTTP/1.1",
                                                 headerFields: [:])!)
    }
}

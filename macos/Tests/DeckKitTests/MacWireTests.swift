import XCTest
@testable import DeckKit

/// **MB1, byte for byte.** The server (`server/mac_api.py`, `mac_nodes.py`)
/// is strict: an unknown event type, a `seq` that is not a positive integer,
/// an output chunk over 65 536 characters or a result without a known
/// `state` is a 400, and a job the Mac cannot decode sits `claimed` until it
/// is `lost`. These pin the keys and shapes the Mac sends and accepts.
final class MacWireTests: XCTestCase {

    private func object(_ value: some Encodable) throws -> [String: Any] {
        let data = try JSONEncoder().encode(value)
        return try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
    }

    private func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONDecoder().decode(type, from: Data(json.utf8))
    }

    // MARK: register

    func testRegisterBodyUsesMB1Keys() throws {
        let body = MacRegisterRequest(machineId: "M-1", name: "Sam's MacBook Pro", os: "macOS 26.0 (25A354)",
                                      appVersion: "1.5.0", capabilities: [.run, .read, .open], mode: .ask)
        let o = try object(body)
        XCTAssertEqual(Set(o.keys), ["machine_id", "name", "os", "app_version", "capabilities", "mode"])
        XCTAssertEqual(o["machine_id"] as? String, "M-1")
        XCTAssertEqual(o["app_version"] as? String, "1.5.0")
        XCTAssertEqual(o["capabilities"] as? [String], ["run", "read", "open"])
        XCTAssertEqual(o["mode"] as? String, "ask")
    }

    func testRegisterResponseCarriesTheSecretOnlyOnAPairing() throws {
        let paired = try decode(MacRegisterResponse.self,
                                #"{"node_id":"mac_0123456789ab","node_secret":"s3cr3t","name":"Mac","primary":true}"#)
        XCTAssertEqual(paired.nodeId, "mac_0123456789ab")
        XCTAssertEqual(paired.nodeSecret, "s3cr3t")
        XCTAssertTrue(paired.primary)

        let refreshed = try decode(MacRegisterResponse.self,
                                   #"{"node_id":"mac_0123456789ab","name":"Mac","primary":false,"online":true}"#)
        XCTAssertNil(refreshed.nodeSecret)
        XCTAssertFalse(refreshed.primary)
    }

    // MARK: poll

    func testPollBodyUsesMB1KeysAndGrantShape() throws {
        let body = MacPollRequest(wait: 25, freeSlots: 3, running: ["mj_aaaaaaaaaaaa"], mode: .full,
                                  grants: ["atlas": MacWireGrant(state: .hour, until: 1_788_222_702),
                                           "orion": MacWireGrant(state: .always, until: nil)])
        let o = try object(body)
        XCTAssertEqual(Set(o.keys), ["wait", "free_slots", "running", "mode", "grants"])
        XCTAssertEqual(o["wait"] as? Double, 25)
        XCTAssertEqual(o["free_slots"] as? Int, 3)
        XCTAssertEqual(o["running"] as? [String], ["mj_aaaaaaaaaaaa"])
        XCTAssertEqual(o["mode"] as? String, "full")
        let grants = try XCTUnwrap(o["grants"] as? [String: [String: Any]])
        XCTAssertEqual(grants["atlas"]?["state"] as? String, "hour")
        XCTAssertEqual(grants["atlas"]?["until"] as? Double, 1_788_222_702)
        // `until: null` is sent, not dropped — the server reads it as "no end".
        XCTAssertTrue(grants["orion"]?.keys.contains("until") == true)
        XCTAssertTrue(grants["orion"]?["until"] is NSNull)
    }

    func testPollResponseDecodesEveryKindsArgs() throws {
        let json = #"""
        {"jobs": [
          {"id":"mj_000000000001","desk":"atlas","kind":"run",
           "args":{"command":"sw_vers","cwd":null,"env":{"A":"1"}},"timeout_s":120,"background":false,"created_at":1788222702.5},
          {"id":"mj_000000000002","desk":"atlas","kind":"read",
           "args":{"path":"~/x","offset":0,"length":100,"encoding":"text"},"timeout_s":30,"background":false,"created_at":1},
          {"id":"mj_000000000003","desk":"orion","kind":"write",
           "args":{"path":"/tmp/y","content":"aGk=","encoding":"base64","mode":"create","make_dirs":true},
           "timeout_s":30,"background":true,"created_at":1},
          {"id":"mj_000000000004","desk":"orion","kind":"list","args":{"path":"~","hidden":true},"timeout_s":30,"background":false,"created_at":1},
          {"id":"mj_000000000005","desk":"orion","kind":"open","args":{"target":"https://example.com"},"timeout_s":30,"background":false,"created_at":1},
          {"id":"mj_000000000006","desk":"orion","kind":"screenshot","args":{},"timeout_s":30,"background":false,"created_at":1}
        ], "cancel": ["mj_00000000000f"], "server_ts": 1788222703.25}
        """#
        let r = try decode(MacPollResponse.self, json)
        XCTAssertEqual(r.jobs.map(\.id), (1...6).map { String(format: "mj_%012d", $0) })
        XCTAssertEqual(r.jobs.map(\.jobKind), [.run, .read, .write, .list, .open, .screenshot])
        XCTAssertEqual(r.cancel, ["mj_00000000000f"])
        XCTAssertEqual(r.serverTs, 1_788_222_703.25)

        let run = r.jobs[0]
        XCTAssertEqual(run.args.command, "sw_vers")
        XCTAssertNil(run.args.cwd)
        XCTAssertEqual(run.args.env, ["A": "1"])
        XCTAssertEqual(run.timeoutS, 120)
        XCTAssertEqual(run.createdAt, 1_788_222_702.5)
        XCTAssertEqual(r.jobs[1].args.length, 100)
        XCTAssertEqual(r.jobs[1].args.encoding, "text")
        XCTAssertEqual(r.jobs[2].args.makeDirs, true)
        XCTAssertEqual(r.jobs[2].args.mode, "create")
        XCTAssertTrue(r.jobs[2].background)
        XCTAssertEqual(r.jobs[3].args.hidden, true)
        XCTAssertEqual(r.jobs[4].args.target, "https://example.com")
    }

    /// A kind this build does not know must not throw away the whole poll —
    /// the Mac refuses that one job instead of letting it go `lost`.
    func testUnknownKindDecodesAndHasNoJobKind() throws {
        let r = try decode(MacPollResponse.self, #"""
        {"jobs":[{"id":"mj_000000000009","desk":"atlas","kind":"teleport","args":{},"timeout_s":5,"background":false,"created_at":1},
                 {"id":"mj_00000000000a","desk":"atlas","kind":"run","args":{"command":"true"},"timeout_s":5,"background":false,"created_at":1}],
         "cancel":[],"server_ts":1}
        """#)
        XCTAssertEqual(r.jobs.count, 2)
        XCTAssertNil(r.jobs[0].jobKind)
        XCTAssertEqual(r.jobs[0].kind, "teleport")
        XCTAssertEqual(r.jobs[1].jobKind, .run)
    }

    // MARK: events

    func testEventsEncodeAsSeqTypeData() throws {
        let events: [MacJobEvent] = [
            MacJobEvent(seq: 1, body: .awaitingGrant),
            MacJobEvent(seq: 2, body: .started(MacStartedData(pid: 4123, cwd: "/Users/y/w", sandboxed: true))),
            MacJobEvent(seq: 3, body: .stdout("ProductName:\tmacOS\n")),
            MacJobEvent(seq: 4, body: .stderr("oops")),
            MacJobEvent(seq: 5, body: .result(MacJobResultData(state: .done, exit: 0, signal: nil, reason: nil, detail: "",
                                                               durationMs: 812, payload: [:]))),
        ]
        let o = try object(MacEventsRequest(events: events))
        let list = try XCTUnwrap(o["events"] as? [[String: Any]])
        XCTAssertEqual(list.map { $0["seq"] as? Int }, [1, 2, 3, 4, 5])
        XCTAssertEqual(list.map { $0["type"] as? String }, ["awaiting_grant", "started", "stdout", "stderr", "result"])
        XCTAssertNil(list[0]["data"])
        let started = try XCTUnwrap(list[1]["data"] as? [String: Any])
        XCTAssertEqual(started["pid"] as? Int, 4123)
        XCTAssertEqual(started["cwd"] as? String, "/Users/y/w")
        XCTAssertEqual(started["sandboxed"] as? Bool, true)
        XCTAssertEqual(list[2]["data"] as? String, "ProductName:\tmacOS\n")
        XCTAssertEqual(list[3]["data"] as? String, "oops")

        let result = try XCTUnwrap(list[4]["data"] as? [String: Any])
        XCTAssertEqual(Set(result.keys), ["state", "exit", "signal", "reason", "detail", "duration_ms", "payload"])
        XCTAssertEqual(result["state"] as? String, "done")
        XCTAssertEqual(result["exit"] as? Int, 0)
        XCTAssertTrue(result["signal"] is NSNull)
        XCTAssertTrue(result["reason"] is NSNull)
        XCTAssertEqual(result["duration_ms"] as? Int, 812)
        XCTAssertEqual((result["payload"] as? [String: Any])?.count, 0)
    }

    func testResultStatesAreExactlyMB1s() {
        XCTAssertEqual(MacResultState.allCases.map(\.rawValue), ["done", "failed", "cancelled", "timed_out", "refused"])
    }

    func testRefusedResultCarriesNullExitAndItsReason() throws {
        let e = MacJobEvent(seq: 1, body: .result(MacJobResultData(state: .refused, exit: nil, signal: nil,
                                                                   reason: "blocked_path", detail: "no", durationMs: 0,
                                                                   payload: [:])))
        let data = try XCTUnwrap(try object(e)["data"] as? [String: Any])
        XCTAssertTrue(data["exit"] is NSNull)
        XCTAssertEqual(data["reason"] as? String, "blocked_path")
        XCTAssertEqual(data["state"] as? String, "refused")
    }

    func testPayloadKeepsIntegersIntegersAndNestsObjects() throws {
        let payload: [String: MacJSON] = [
            "path": "/tmp", "truncated": false, "size": 12,
            "entries": [["name": "a", "kind": "file", "size": 3, "modified": 1.5]],
        ]
        let e = MacJobEvent(seq: 9, body: .result(MacJobResultData(state: .done, exit: nil, signal: nil, reason: nil,
                                                                   detail: "", durationMs: 1, payload: payload)))
        let text = String(decoding: try JSONEncoder().encode(e), as: UTF8.self)
        XCTAssertTrue(text.contains(#""size":12"#), text)   // not 12.0
        let data = try XCTUnwrap(try object(e)["data"] as? [String: Any])
        let p = try XCTUnwrap(data["payload"] as? [String: Any])
        XCTAssertEqual(p["truncated"] as? Bool, false)
        let entries = try XCTUnwrap(p["entries"] as? [[String: Any]])
        XCTAssertEqual(entries.first?["modified"] as? Double, 1.5)
    }

    func testMacJSONRoundTrips() throws {
        let value: MacJSON = ["a": [1, 2.5, "x", true, nil], "b": ["c": "d"]]
        let back = try JSONDecoder().decode(MacJSON.self, from: JSONEncoder().encode(value))
        XCTAssertEqual(back, value)
    }

    func testEventsResponseAndGrantBodies() throws {
        let r = try decode(MacEventsResponse.self, #"{"ok":true,"cancel":true}"#)
        XCTAssertTrue(r.ok)
        XCTAssertTrue(r.cancel)

        let g = try object(MacGrantPost(desk: "atlas", decision: .hour, until: 100))
        XCTAssertEqual(Set(g.keys), ["desk", "decision", "until"])
        XCTAssertEqual(g["decision"] as? String, "hour")
        let revoke = try object(MacGrantPost(desk: "atlas", decision: .revoke, until: nil))
        XCTAssertTrue(revoke["until"] is NSNull)
        XCTAssertEqual(try decode(MacGrantResponse.self, #"{"ok":true,"told":false}"#).told, false)
    }

    func testRefusalBodyDecodes() throws {
        let r = try decode(MacWireRefusal.self, #"{"ok":false,"reason":"unknown_node","detail":"X-Deck-Node is not this Mac's"}"#)
        XCTAssertEqual(r.reason, "unknown_node")
        XCTAssertEqual(r.detail, "X-Deck-Node is not this Mac's")
    }

    // MARK: output chunks

    /// The server counts characters the way Python does (code points); a
    /// chunk over 65 536 is a 400 that loses the whole batch.
    func testChunksNeverExceedTheServerLimitInCodePoints() {
        let text = String(repeating: "a", count: 70_000) + String(repeating: "👩‍👩‍👧", count: 30_000)
        let parts = MacWire.chunks(text)
        XCTAssertTrue(parts.allSatisfy { $0.unicodeScalars.count <= MacWire.chunkMax })
        XCTAssertEqual(parts.joined(), text)
        XCTAssertEqual(MacWire.chunks(""), [])
        XCTAssertEqual(MacWire.chunks("hi"), ["hi"])
    }

    /// A multi-byte character split across two pipe reads is not turned
    /// into two replacement characters.
    func testUTF8SplitHoldsBackAnIncompleteTail() {
        let bytes = Array("héllo €".utf8)
        let cut = Data(bytes.dropLast(2))            // "héllo " + first byte of €
        XCTAssertEqual(MacWire.completeUTF8Prefix(cut), cut.count - 1)
        XCTAssertEqual(MacWire.completeUTF8Prefix(Data(bytes)), bytes.count)
        XCTAssertEqual(MacWire.completeUTF8Prefix(Data()), 0)
        // Garbage is not held back forever.
        XCTAssertEqual(MacWire.completeUTF8Prefix(Data([0xff, 0xfe, 0x41])), 3)
    }

    func testSummaryMatchesTheServersOneLine() {
        XCTAssertEqual(MacWire.summary(kind: .run, args: MacJobArgs(command: "npm   test\n --watch")), "npm test --watch")
        XCTAssertEqual(MacWire.summary(kind: .read, args: MacJobArgs(path: "~/x")), "read ~/x")
        XCTAssertEqual(MacWire.summary(kind: .open, args: MacJobArgs(target: "https://a")), "open https://a")
        XCTAssertEqual(MacWire.summary(kind: .screenshot, args: MacJobArgs()), "screenshot")
        XCTAssertEqual(MacWire.summary(kind: .run, args: MacJobArgs(command: String(repeating: "x", count: 900))).count, 500)
    }
}

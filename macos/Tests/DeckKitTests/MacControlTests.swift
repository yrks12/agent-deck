import XCTest
@testable import DeckKit

/// **Mac control (owner, 2026-10-01): agents click, type, press keys and
/// scroll on his Mac, behind a per-session switch that expires.**
///
/// The pure half, before any of it exists:
///
/// * The grant is per session: 30 minutes, one desk or all, never saved to
///   disk (quitting the app ends it). His own hands (`@owner`, the phone's
///   live view) ride any live grant.
/// * `input` is a job kind the Mac's policy decides like every other: no live
///   grant for that desk is `control_off`, a sentence the desk can act on.
/// * While control is on for a desk it may take screenshots even if he never
///   turned screenshots on: it cannot click what it cannot see.
/// * Coordinates arrive in the pixels of the picture the sender was looking
///   at; on a Retina display that is not the point space CGEvent wants.
/// * Keys arrive as the X11 names every viewer already sends (`Return`,
///   `ctrl+l`) plus `cmd`; each becomes one macOS virtual key and its flags.
final class MacControlTests: XCTestCase {
    let now: Double = 1_788_222_702

    // MARK: the grant

    func testAGrantLastsThirtyMinutesAndCoversItsScope() {
        let one = MacControlGrant.start(scope: "atlas", now: now)
        XCTAssertEqual(one.until, now + 1800)
        XCTAssertTrue(one.covers("atlas", now: now))
        XCTAssertFalse(one.covers("scout", now: now))
        XCTAssertTrue(one.covers(MacControlGrant.ownerDesk, now: now))
        XCTAssertFalse(one.covers("atlas", now: now + 1800))
        let all = MacControlGrant.start(scope: nil, now: now)
        XCTAssertTrue(all.covers("scout", now: now + 60))
        XCTAssertEqual(all.wire, MacWireControl(scope: "all", until: now + 1800))
        XCTAssertEqual(one.wire, MacWireControl(scope: "atlas", until: now + 1800))
    }

    func testTheGrantIsNeverPartOfTheSavedPolicy() throws {
        let data = try JSONEncoder().encode(MacPolicyConfig.defaults)
        let text = String(decoding: data, as: UTF8.self)
        XCTAssertFalse(text.contains("control"))
    }

    // MARK: the policy

    private func policy(screenshots: Bool = false, mode: MacMode = .ask,
                        control: MacControlGrant? = nil) -> MacPolicy {
        var p = MacPolicy(config: MacPolicyConfig(mode: mode, screenshotEnabled: screenshots),
                          paths: MacPaths(home: "/tmp"), seatbeltAvailable: true, tempDirs: [])
        p.control = control
        return p
    }

    private func job(_ kind: MacJobKind, desk: String = "atlas") -> MacJobRequest {
        MacJobRequest(id: "mj_1", desk: desk, kind: kind, summary: "x")
    }

    private func reason(_ d: MacDecision) -> String? {
        if case let .refuse(reason, _) = d { return reason }
        return nil
    }

    private func isRun(_ d: MacDecision) -> Bool {
        if case .run = d { return true }
        return false
    }

    func testInputWithoutAGrantIsControlOffWithTheNextStep() {
        let d = policy().decide(job(.input), now: now)
        XCTAssertEqual(reason(d), "control_off")
        if case let .refuse(_, detail) = d {
            XCTAssertTrue(detail.contains("Let agents control this Mac"), detail)
        }
    }

    func testInputUnderAGrantRunsOnlyForItsScope() {
        let p = policy(control: .start(scope: "atlas", now: now))
        XCTAssertTrue(isRun(p.decide(job(.input), now: now)))
        XCTAssertEqual(reason(p.decide(job(.input, desk: "scout"), now: now)), "control_off")
        XCTAssertTrue(isRun(p.decide(job(.input, desk: MacControlGrant.ownerDesk), now: now)))
        XCTAssertEqual(reason(p.decide(job(.input), now: now + 1801)), "control_off")
    }

    func testPausedAndOffStillWinOverAGrant() {
        let grant = MacControlGrant.start(scope: nil, now: now)
        XCTAssertEqual(reason(policy(mode: .paused, control: grant).decide(job(.input), now: now)), "mac_paused")
        XCTAssertEqual(reason(policy(mode: .off, control: grant).decide(job(.input), now: now)), "mac_offline")
    }

    func testAGrantLetsThatDeskSeeTheScreenEvenWithScreenshotsOff() {
        XCTAssertEqual(reason(policy().decide(job(.screenshot), now: now)), "capability_off")
        let p = policy(control: .start(scope: "atlas", now: now))
        XCTAssertTrue(isRun(p.decide(job(.screenshot), now: now)))
        XCTAssertEqual(reason(p.decide(job(.screenshot, desk: "scout"), now: now)), "capability_off")
    }

    // MARK: the gesture

    func testEachActionParses() throws {
        XCTAssertEqual(try MacInputGesture.parse(MacJobArgs(action: "click", x: 3, y: 4, button: "right", count: 2,
                                                            space: MacSpace(width: 1440, height: 900))),
                       .click(x: 3, y: 4, button: .right, count: 2, space: MacSpace(width: 1440, height: 900)))
        XCTAssertEqual(try MacInputGesture.parse(MacJobArgs(action: "scroll", x: 1, y: 2, dy: -3,
                                                            space: MacSpace(width: 10, height: 10))),
                       .scroll(x: 1, y: 2, dx: 0, dy: -3, space: MacSpace(width: 10, height: 10)))
        XCTAssertEqual(try MacInputGesture.parse(MacJobArgs(action: "drag", x: 1, y: 2, toX: 5, toY: 6,
                                                            space: MacSpace(width: 10, height: 10))),
                       .drag(x: 1, y: 2, toX: 5, toY: 6, button: .left, space: MacSpace(width: 10, height: 10)))
        XCTAssertEqual(try MacInputGesture.parse(MacJobArgs(action: "type", text: "hi")), .type("hi"))
        XCTAssertEqual(try MacInputGesture.parse(MacJobArgs(action: "key", key: "cmd+s")), .key("cmd+s"))
    }

    func testABadGestureIsBadInput() {
        for args in [MacJobArgs(action: "punch"), MacJobArgs(action: "click", x: 1, y: 2),
                     MacJobArgs(action: "click", x: 10, y: 2, space: MacSpace(width: 10, height: 10)),
                     MacJobArgs(action: "type", text: ""), MacJobArgs(action: "key", key: "--file")] {
            XCTAssertThrowsError(try MacInputGesture.parse(args)) { error in
                XCTAssertEqual((error as? MacOpError)?.reason, "bad_input")
            }
        }
    }

    // MARK: pixels to points

    func testRetinaPixelsBecomePoints() {
        let display = MacDisplayFrame(x: 0, y: 0, width: 1512, height: 982)
        let p = MacInputMapping.point(x: 3024 / 2, y: 1964 / 2, space: MacSpace(width: 3024, height: 1964),
                                      display: display)
        XCTAssertEqual(p.x, 756, accuracy: 0.01)
        XCTAssertEqual(p.y, 491, accuracy: 0.01)
        let same = MacInputMapping.point(x: 100, y: 50, space: MacSpace(width: 1512, height: 982), display: display)
        XCTAssertEqual(same.x, 100, accuracy: 0.01)
        XCTAssertEqual(same.y, 50, accuracy: 0.01)
        let offset = MacInputMapping.point(x: 0, y: 0, space: MacSpace(width: 1440, height: 900),
                                           display: MacDisplayFrame(x: -1440, y: 0, width: 1440, height: 900))
        XCTAssertEqual(offset.x, -1440, accuracy: 0.01)
    }

    // MARK: keys

    func testKeysBecomeOneVirtualKeyAndItsFlags() {
        XCTAssertEqual(MacKeyChord.parse("Return"), MacKeyChord(keyCode: 36, flags: []))
        XCTAssertEqual(MacKeyChord.parse("cmd+s"), MacKeyChord(keyCode: 1, flags: [.command]))
        XCTAssertEqual(MacKeyChord.parse("ctrl+alt+shift+Left"),
                       MacKeyChord(keyCode: 123, flags: [.control, .option, .shift]))
        XCTAssertEqual(MacKeyChord.parse("super+space"), MacKeyChord(keyCode: 49, flags: [.command]))
        XCTAssertEqual(MacKeyChord.parse("A"), MacKeyChord(keyCode: 0, flags: [.shift]))
        XCTAssertEqual(MacKeyChord.parse("exclam"), MacKeyChord(keyCode: 18, flags: [.shift]))
        XCTAssertEqual(MacKeyChord.parse("Prior"), MacKeyChord(keyCode: 116, flags: []))
        XCTAssertEqual(MacKeyChord.parse("Page_Down"), MacKeyChord(keyCode: 121, flags: []))
        XCTAssertEqual(MacKeyChord.parse("F5"), MacKeyChord(keyCode: 96, flags: []))
        XCTAssertEqual(MacKeyChord.parse("ctrl+minus"), MacKeyChord(keyCode: 27, flags: [.control]))
        XCTAssertNil(MacKeyChord.parse("nope"))
        XCTAssertNil(MacKeyChord.parse("cmd+"))
        XCTAssertNil(MacKeyChord.parse("cmd+ctrl"))
    }

    func testEveryKeyAViewerSendsHasAKeyCode() {
        for name in ScreenKeys.namedKeys.values {
            XCTAssertNotNil(MacKeyChord.parse(name), name)
        }
        for name in ScreenKeys.punctuationKeysyms.values {
            XCTAssertNotNil(MacKeyChord.parse(name), name)
        }
    }

    // MARK: the wire

    func testInputArgsDecodeFromTheDecksJob() throws {
        let json = #"{"id":"mj_1","desk":"atlas","kind":"input","timeout_s":30,"background":false,"created_at":1,"#
            + #""args":{"action":"drag","x":1,"y":2,"to_x":3,"to_y":4,"button":"left","space":{"width":1440,"height":900}}}"#
        let job = try JSONDecoder().decode(MacWireJob.self, from: Data(json.utf8))
        XCTAssertEqual(job.jobKind, .input)
        XCTAssertEqual(try MacInputGesture.parse(job.args),
                       .drag(x: 1, y: 2, toX: 3, toY: 4, button: .left, space: MacSpace(width: 1440, height: 900)))
    }

    func testThePollCarriesTheGrantTheScreenAndThePermissions() throws {
        var body = MacPollRequest(wait: 25, freeSlots: 4, running: [], mode: .ask, grants: [:])
        var json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertNil(json?["control"], "no grant: the key is absent (an older deck ignores it)")
        body.control = MacWireControl(scope: "atlas", until: now)
        body.screen = MacSpace(width: 1512, height: 982)
        body.perms = MacPermissions(accessibility: true, screenRecording: false)
        json = try JSONSerialization.jsonObject(with: JSONEncoder().encode(body)) as? [String: Any]
        XCTAssertEqual((json?["control"] as? [String: Any])?["scope"] as? String, "atlas")
        XCTAssertEqual((json?["screen"] as? [String: Any])?["width"] as? Int, 1512)
        XCTAssertEqual((json?["perms"] as? [String: Any])?["screen_recording"] as? Bool, false)
    }

    func testThePollAnswerCarriesWatchAndTheAsks() throws {
        let r = try JSONDecoder().decode(MacPollResponse.self, from: Data(
            #"{"jobs":[],"cancel":[],"server_ts":1,"watch":true,"control_asks":["atlas"]}"#.utf8))
        XCTAssertTrue(r.watch)
        XCTAssertEqual(r.controlAsks, ["atlas"])
        let old = try JSONDecoder().decode(MacPollResponse.self, from: Data(#"{"jobs":[],"cancel":[]}"#.utf8))
        XCTAssertFalse(old.watch)
        XCTAssertEqual(old.controlAsks, [])
    }

    func testTheSummarySaysWhatWasDone() {
        XCTAssertEqual(MacWire.summary(kind: .input, args: MacJobArgs(action: "click", x: 3, y: 4, button: "left", count: 2)),
                       "double-click 3,4")
        XCTAssertEqual(MacWire.summary(kind: .input, args: MacJobArgs(action: "key", key: "cmd+s")), "key cmd+s")
        XCTAssertEqual(MacWire.summary(kind: .input, args: MacJobArgs(action: "type", text: "hello")),
                       "type 'hello' (5 chars)")
    }

    // MARK: the viewer

    func testThePhoneListsHisMacsWithWhetherControlIsOn() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/nodes"] = Data(#"""
            {"nodes":[{"node_id":"mac_abc","name":"Studio","os":"macOS 26","online":true,"mode":"ask",
              "last_seen":1,"primary":true,"running":0,"control":{"live":true,"scope":"all","until":5},
              "screen":{"width":1512,"height":982},"perms":null},
             {"node_id":"mac_old","name":"Old","online":false}]}
            """#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens, performer: performer)
        let macs = try await client.macNodes()
        XCTAssertEqual(macs.map(\.nodeId), ["mac_abc", "mac_old"])
        XCTAssertEqual(macs[0].screenName, "mac:mac_abc")
        XCTAssertTrue(macs[0].controlLive)
        XCTAssertFalse(macs[1].controlLive)
        XCTAssertEqual(performer.paths, ["/v1/nodes"])
    }

    func testAMacScreenNameUsesTheNodeRoutes() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/nodes/mac_abc/screen"] = Data(#"""
            {"desk":"Studio","display":"Studio","computer":{"running":true,"image":"macOS","container":"mac_abc"},
             "width":1440,"height":900,"stale_after":10,"generated_at":1}
            """#.utf8)
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        let client = HTTPDeckClient(baseURL: URL(string: "https://deck.local")!, tokens: tokens, performer: performer)
        let name = MacScreenName.agent(nodeId: "mac_abc")
        XCTAssertEqual(name, "mac:mac_abc")
        let status = try await client.screenStatus(agent: name)
        XCTAssertEqual(status.width, 1440)
        try await client.sendScreenInput(agent: name, .key("Return"))
        XCTAssertEqual(performer.paths, ["/v1/nodes/mac_abc/screen", "/v1/nodes/mac_abc/screen/input"])
        do {
            _ = try await client.openScreenStream(agent: name)
            XCTFail("a Mac has no stream; the viewer must poll")
        } catch {}
    }
}

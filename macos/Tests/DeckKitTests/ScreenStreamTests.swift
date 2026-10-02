import XCTest
import Combine
import CoreGraphics
import ImageIO
import UniformTypeIdentifiers
@testable import DeckKit

/// **The screen as a stream, and polling as the fallback.**
///
/// MEASURED 2026-10-01 from this Mac over WireGuard, probe desk, healthy box:
/// polling `screen.jpg` peaked at 1.84 fps and a typed key took 0.81-0.99 s to
/// show; the stream ran at ~10 fps while the screen changed, sent nothing while
/// it was still, and a typed key showed in ~0.2 s.
@MainActor
final class ScreenStreamTests: XCTestCase {

    // MARK: the wire

    func testABinaryMessageIsASequenceAnAgeAndAJPEG() {
        let jpeg = Data([0xFF, 0xD8, 0xFF, 0xE0, 0x01])
        let message = Data([0, 0, 1, 2, 0, 0, 0x01, 0xF4]) + jpeg
        XCTAssertEqual(ScreenStreamEvent.parse(binary: message),
                       .frame(seq: 258, age: 0.5, jpeg: jpeg))
    }

    func testABinaryMessageThatIsNotAJPEGIsDropped() {
        XCTAssertNil(ScreenStreamEvent.parse(binary: Data([0, 0, 0, 1, 0, 0, 0, 0, 1, 2, 3])))
        XCTAssertNil(ScreenStreamEvent.parse(binary: Data([0, 0])))
    }

    func testTheTextMessages() {
        XCTAssertEqual(ScreenStreamEvent.parse(text: #"{"type":"hello","width":1280,"height":800,"window":2}"#),
                       .hello(width: 1280, height: 800, window: 2))
        XCTAssertEqual(ScreenStreamEvent.parse(text: #"{"type":"tick","at":1}"#), .tick)
        XCTAssertEqual(ScreenStreamEvent.parse(text: #"{"type":"input","id":7,"ok":true}"#),
                       .inputResult(id: 7, refusal: nil))
        XCTAssertEqual(ScreenStreamEvent.parse(
            text: #"{"type":"input","id":8,"ok":false,"reason":"bad_input","detail":"off screen"}"#),
                       .inputResult(id: 8, refusal: .badInput("off screen")))
        XCTAssertEqual(ScreenStreamEvent.parse(
            text: #"{"type":"error","reason":"computer_not_running","fallback":"poll"}"#),
                       .fallback(reason: "computer_not_running"))
        XCTAssertNil(ScreenStreamEvent.parse(text: #"{"type":"from-the-future"}"#))
    }

    func testInputOnTheSocketIsTheSameBodyTheRouteTakes() throws {
        let text = try ScreenStreamWire.input(.click(x: 3, y: 4), id: 9)
        let body = try JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any]
        XCTAssertEqual(body?["type"] as? String, "input")
        XCTAssertEqual(body?["id"] as? Int, 9)
        XCTAssertEqual(body?["action"] as? String, "click")
        XCTAssertEqual(body?["x"] as? Int, 3)
        XCTAssertEqual(body?["y"] as? Int, 4)
        XCTAssertEqual(ScreenStreamWire.ack(12), #"{"type":"ack","seq":12}"#)
    }

    // MARK: decoding off the main thread

    func testFramesAreDecodedOffTheMainThread() async throws {
        let ranOnMain = Flag()
        let decoded = await ScreenFrameDecoder.decode(Self.realJPEG(),
                                                      probe: { ranOnMain.set($0) })
        XCTAssertNotNil(decoded)
        XCTAssertEqual(ranOnMain.value, false,
                       "the JPEG was decoded on the main thread, under his cursor")
        XCTAssertEqual(decoded?.cgImage.width, 8)
    }

    // MARK: the model

    func testTheModelStreamsAndAcksEachFrameAfterDecodingIt() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        let jpeg = Self.realJPEG()
        session.queue([.hello(width: 1280, height: 800, window: 2),
                       .frame(seq: 1, age: 0.05, jpeg: jpeg),
                       .frame(seq: 2, age: 0.04, jpeg: jpeg)])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 3))
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        await session.drained()
        try await waitUntil { session.acks == [1, 2] }

        guard case .live(let frame) = model.state else {
            return XCTFail("streamed frame is not live: \(model.state)")
        }
        XCTAssertNotNil(frame.decoded, "a frame reached the view undecoded")
        XCTAssertEqual(frame.decoded?.cgImage.width, 8)
        model.removeWatcher(.takeover)
        await model.pollLoopFinished()
        XCTAssertTrue(session.closed, "the socket outlived the panel")
    }

    func testFramesSwapWithoutAGapBetweenThem() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        session.queue([.frame(seq: 1, age: 0, jpeg: Self.realJPEG()),
                       .tick,
                       .frame(seq: 2, age: 0, jpeg: Self.realJPEG(shade: 200))])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 3))
        var states: [AgentScreenState] = []
        let watch = model.$state.sink { states.append($0) }
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        try await waitUntil { session.acks == [1, 2] }
        watch.cancel()

        let afterFirst = states.drop { if case .live = $0 { return false }; return true }
        XCTAssertFalse(afterFirst.isEmpty)
        for state in afterFirst {
            guard case .live = state else {
                return XCTFail("the picture dropped out between frames: \(state)")
            }
        }
        model.removeWatcher(.takeover)
        await model.pollLoopFinished()
    }

    func testInputGoesOverTheSocketWhileItIsOpen() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        session.queue([.frame(seq: 1, age: 0, jpeg: Self.realJPEG())])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 3))
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        try await waitUntil { session.acks == [1] }

        await model.send(.click(x: 10, y: 20))
        XCTAssertEqual(session.inputs.map(\.0), [.click(x: 10, y: 20)])
        XCTAssertTrue(client.http.inputs.isEmpty, "the click went over HTTP with a socket open")

        session.queue([.inputResult(id: session.inputs[0].1, refusal: .badInput("off"))])
        try await waitUntil { model.lastInputRefusal == .badInput("off") }
        model.removeWatcher(.takeover)
        await model.pollLoopFinished()
    }

    func testNoSocketMeansThePollCarriesOn() async throws {
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: nil)
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 4))
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        await model.pollLoopFinished()
        XCTAssertGreaterThanOrEqual(client.http.frameCalls, 3,
                                    "a deck without the stream left the panel with no pictures")
        XCTAssertGreaterThanOrEqual(client.opens, 1)
        if case .live(let frame) = model.state {
            XCTAssertNotNil(frame.decoded, "polled frames must be decoded off main too")
        }
    }

    func testTheDeckSayingPollFallsBackAndInputUsesHTTPAgain() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        session.queue([.frame(seq: 1, age: 0, jpeg: Self.realJPEG()),
                       .fallback(reason: "stream_ended")])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 4))
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        await model.pollLoopFinished()
        XCTAssertTrue(session.closed)
        XCTAssertGreaterThanOrEqual(client.http.frameCalls, 2,
                                    "after the deck said poll, nothing polled")
        await model.send(.key("Return"))
        XCTAssertEqual(client.http.inputs, [.key("Return")])
    }

    // MARK: helpers

    /// A real 8x8 JPEG, so decoding is the real decoder's.
    nonisolated static func realJPEG(shade: UInt8 = 90) -> Data {
        let space = CGColorSpaceCreateDeviceRGB()
        let ctx = CGContext(data: nil, width: 8, height: 8, bitsPerComponent: 8,
                            bytesPerRow: 32, space: space,
                            bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue)!
        ctx.setFillColor(CGColor(red: CGFloat(shade) / 255, green: 0.3, blue: 0.6, alpha: 1))
        ctx.fill(CGRect(x: 0, y: 0, width: 8, height: 8))
        let image = ctx.makeImage()!
        let out = NSMutableData()
        let dest = CGImageDestinationCreateWithData(out, UTType.jpeg.identifier as CFString, 1, nil)!
        CGImageDestinationAddImage(dest, image, nil)
        CGImageDestinationFinalize(dest)
        return out as Data
    }

    private func waitUntil(_ condition: @escaping @MainActor () -> Bool,
                           seconds: Double = 3) async throws {
        let end = Date().addingTimeInterval(seconds)
        while !condition() {
            if Date() > end { return XCTFail("timed out waiting") }
            try await Task.sleep(nanoseconds: 5_000_000)
        }
    }
}

// MARK: - doubles

final class Flag: @unchecked Sendable {
    private let lock = NSLock()
    private var _value: Bool?
    func set(_ v: Bool) { lock.lock(); _value = v; lock.unlock() }
    var value: Bool? { lock.lock(); defer { lock.unlock() }; return _value }
}

/// A socket whose deck is the test: events are queued, and once they run out
/// `next()` waits until more are queued or the socket is closed.
final class FakeStreamSession: ScreenStreamSession, @unchecked Sendable {
    private let lock = NSLock()
    private var events: [ScreenStreamEvent] = []
    private var waiter: CheckedContinuation<ScreenStreamEvent?, Never>?
    private var _acks: [Int] = []
    private var _inputs: [(ScreenInput, Int)] = []
    private var _closed = false

    var acks: [Int] { lock.lock(); defer { lock.unlock() }; return _acks }
    var inputs: [(ScreenInput, Int)] { lock.lock(); defer { lock.unlock() }; return _inputs }
    var closed: Bool { lock.lock(); defer { lock.unlock() }; return _closed }

    func queue(_ more: [ScreenStreamEvent]) {
        lock.lock()
        events += more
        let w = waiter
        waiter = nil
        let first = w != nil && !events.isEmpty ? events.removeFirst() : nil
        lock.unlock()
        if let w { w.resume(returning: first) }
    }

    func drained() async {
        while true {
            lock.lock(); let empty = events.isEmpty; lock.unlock()
            if empty { return }
            await Task.yield()
        }
    }

    func next() async throws -> ScreenStreamEvent {
        let event: ScreenStreamEvent? = await withCheckedContinuation { cont in
            lock.lock()
            if _closed { lock.unlock(); cont.resume(returning: nil); return }
            if !events.isEmpty {
                let e = events.removeFirst(); lock.unlock(); cont.resume(returning: e); return
            }
            waiter = cont
            lock.unlock()
        }
        guard let event else { throw ScreenRefusal.transport("closed") }
        return event
    }

    func ack(_ seq: Int) async { lock.lock(); _acks.append(seq); lock.unlock() }

    func send(_ input: ScreenInput, id: Int) async throws {
        lock.lock(); _inputs.append((input, id)); lock.unlock()
    }

    func close() {
        lock.lock()
        _closed = true
        let w = waiter
        waiter = nil
        lock.unlock()
        w?.resume(returning: nil)
    }
}

/// The HTTP fake, plus a stream that opens (or, with no session, refuses).
final class StreamingFakeClient: AgentScreenClient, AgentScreenStreaming, @unchecked Sendable {
    let http: FakeScreenClient
    let session: FakeStreamSession?
    private let lock = NSLock()
    private var _opens = 0
    var opens: Int { lock.lock(); defer { lock.unlock() }; return _opens }

    init(status: AgentScreenStatus, session: FakeStreamSession?) {
        http = FakeScreenClient(status: status)
        self.session = session
    }

    func screenStatus(agent: String) async throws -> AgentScreenStatus {
        try await http.screenStatus(agent: agent)
    }

    func screenFrame(agent: String) async throws -> AgentScreenFrame {
        let frame = try await http.screenFrame(agent: agent)
        return AgentScreenFrame(jpeg: ScreenStreamTests.realJPEG(), serverAge: frame.serverAge,
                                display: frame.display, receivedAt: frame.receivedAt)
    }

    func sendScreenInput(agent: String, _ input: ScreenInput) async throws {
        try await http.sendScreenInput(agent: agent, input)
    }

    func openScreenStream(agent: String) async throws -> ScreenStreamSession {
        lock.lock(); _opens += 1; lock.unlock()
        guard let session, !session.closed else {
            throw ScreenRefusal.transport("no stream on this deck")
        }
        return session
    }
}

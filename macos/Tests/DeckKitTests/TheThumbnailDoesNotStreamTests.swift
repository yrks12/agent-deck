import XCTest
@testable import DeckKit

/// **A glance is a glance: the thumbnail does not hold a live stream open.**
///
/// MEASURED 2026-10-01 with `sample` on the installed app, idle with a thread
/// open: 22-29% CPU. The hot frames were `ScreenFrameDecoder.decode`
/// (JPEG decode, ~11% of samples on its own) under `runStream` under
/// `runPoll` — the inspector's small thumbnail had opened the full-rate screen
/// stream and was decoding every frame the deck pushed, about 10 a second,
/// for a picture a few hundred points wide that nobody was looking at.
///
/// The rule now: the stream is for the take-over (the big screen, Expand,
/// Watch). A thumbnail alone polls one frame every couple of seconds. When
/// the take-over closes, the socket closes with it and the thumbnail goes
/// back to its slow poll. Stream and poll are never both running.
@MainActor
final class TheThumbnailDoesNotStreamTests: XCTestCase {

    func testAThumbnailAloneNeverOpensTheStream() async throws {
        // A deck whose stream refuses: the old code tried it anyway, which is
        // what `opens` counts, then fell back to polling.
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: nil)
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: BudgetClock(budget: 4))
        model.setWindowActive(true)
        model.addWatcher(.thumbnail)
        await model.pollLoopFinished()
        XCTAssertEqual(client.opens, 0,
                       "the thumbnail opened the live stream and decoded every frame")
        XCTAssertGreaterThanOrEqual(client.http.frameCalls, 2, "the thumbnail stopped updating")
    }

    func testTheThumbnailPollsSlowly() throws {
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: nil)
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client)
        model.addWatcher(.thumbnail)
        XCTAssertGreaterThanOrEqual(model.pollInterval, 2.0,
                                    "a thumbnail asks for a frame more than every 2 s")
        model.removeWatcher(.thumbnail)
    }

    func testClosingTheTakeoverClosesTheSocketAndTheThumbnailPollsAgain() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        session.queue([.frame(seq: 1, age: 0, jpeg: ScreenStreamTests.realJPEG())])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: RealishClock())
        model.setWindowActive(true)
        model.addWatcher(.thumbnail)
        model.addWatcher(.takeover)
        try await waitUntil { session.acks == [1] }

        model.removeWatcher(.takeover)
        try await waitUntil { session.closed }
        let before = client.http.frameCalls
        try await waitUntil { client.http.frameCalls > before }
        XCTAssertTrue(model.isPolling, "the thumbnail behind the take-over froze")
        XCTAssertEqual(client.opens, 1, "the thumbnail reopened the stream")

        model.removeWatcher(.thumbnail)
        await model.pollLoopFinished()
        XCTAssertFalse(model.isPolling)
    }

    func testAHiddenWindowStopsTheStream() async throws {
        let session = FakeStreamSession()
        let client = StreamingFakeClient(status: try AgentComputerTests.runningStatus(),
                                         session: session)
        session.queue([.frame(seq: 1, age: 0, jpeg: ScreenStreamTests.realJPEG())])
        let model = AgentComputerModel(desk: "atlas", displayName: "Atlas", client: client,
                                       clock: RealishClock())
        model.setWindowActive(true)
        model.addWatcher(.takeover)
        try await waitUntil { session.acks == [1] }
        model.setWindowActive(false)
        await model.pollLoopFinished()
        XCTAssertTrue(session.closed, "a hidden window kept the stream open")
        XCTAssertFalse(model.isPolling)
        model.removeWatcher(.takeover)
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

/// Waits a few real milliseconds per tick, whatever interval is asked for.
struct RealishClock: ScreenPollClock {
    func wait(_ seconds: TimeInterval) async throws {
        try await Task.sleep(nanoseconds: 10_000_000)
    }
}

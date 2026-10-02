import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// C2 captures: the composer with its mic, the call bar, and the two lines the
/// mic can put above the composer. Drawn off-screen from fakes — no microphone,
/// no prompt, no speaker. Written to `UITests/Artifacts/overhaul/c2-*.png` only
/// when `DECK_CAPTURE=1`; the feed and call-bar checks run every time.
@MainActor
final class VoiceCaptureTests: XCTestCase {

    private var capturing: Bool { ProcessInfo.processInfo.environment["DECK_CAPTURE"] == "1" }

    private func fakeSession() -> (VoiceSession, FakeSpeechInput, FakeSpeechOutput) {
        let log = VoiceLog()
        let input = FakeSpeechInput(log: log)
        let output = FakeSpeechOutput(log: log)
        let calls = FakeCalls(log: log)
        let session = VoiceSession(input: input, output: output, calls: { calls })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        return (session, input, output)
    }

    private func render<V: View>(_ view: V, size: CGSize, settle: UInt64 = 600_000_000) async throws
        -> NSBitmapImageRep {
        let (host, window) = RightPaneFixture.host(view.frame(width: size.width, height: size.height), size: size)
        defer { window.close() }
        try await Task.sleep(nanoseconds: settle)
        return try XCTUnwrap(RightPaneFixture.render(host))
    }

    private func words(_ rep: NSBitmapImageRep) -> String {
        RightPaneFixture.readText(rep).map(\.text).joined(separator: " ")
    }

    /// A stand-in for the composer pill, with the real mic and the real line
    /// above it — ThreadView owns its session, so the voice states are drawn
    /// here from a fake one.
    private func composer(_ session: VoiceSession) -> some View {
        VStack(spacing: 0) {
            Spacer(minLength: 0)
            VoiceComposerLine(session: session)
            HStack(alignment: .bottom, spacing: 6) {
                Image(systemName: "plus").frame(width: 30, height: 30).background(.quaternary, in: Circle())
                Text("Message Atlas").foregroundStyle(.secondary).padding(.vertical, 6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                Image(systemName: "mic").frame(width: 30, height: 30).background(.quaternary, in: Circle())
            }
            .padding(.leading, 6).padding(.trailing, 8).padding(.vertical, 5)
            .background(Color(white: 0.17), in: RoundedRectangle(cornerRadius: 20, style: .continuous))
            .padding(.horizontal, 14).padding(.top, 6).padding(.bottom, 12)
        }
    }

    func testTheCallBarShowsTheDeskTheTimerMuteAndHangUp() async throws {
        let (session, _, _) = fakeSession()
        await session.startCall()
        let rep = try await render(
            VStack(spacing: 0) { CallBarView(session: session, agent: nil); Spacer() },
            size: CGSize(width: 440, height: 330))
        let text = words(rep)
        XCTAssertTrue(text.contains("On a call with Atlas"), text)
        XCTAssertTrue(text.contains("Listening"), text)
        if capturing { try RightPaneFixture.saveCapture(rep, named: "c2-call-bar.png") }
    }

    func testTheCallBarIsAbsentWithoutACall() async throws {
        let (session, _, _) = fakeSession()
        let rep = try await render(
            VStack(spacing: 0) { CallBarView(session: session, agent: nil); Spacer() },
            size: CGSize(width: 440, height: 70), settle: 200_000_000)
        XCTAssertFalse(words(rep).contains("On a call"))
    }

    func testCaptureTheComposerWhileHeIsTalking() async throws {
        let (session, input, _) = fakeSession()
        await session.pressMic()
        input.say("what's the status of acme")
        let rep = try await render(composer(session), size: CGSize(width: 440, height: 110))
        XCTAssertTrue(words(rep).contains("status of acme"), words(rep))
        if capturing { try RightPaneFixture.saveCapture(rep, named: "c2-mic-listening.png") }
    }

    func testCaptureTheSentenceWhenTheMicIsOff() async throws {
        let (session, input, _) = fakeSession()
        input.permissionAnswer = .denied(VoicePermission.microphoneOff)
        await session.pressMic()
        let rep = try await render(composer(session), size: CGSize(width: 440, height: 120))
        XCTAssertTrue(words(rep).contains("Typing still works"), words(rep))
        if capturing { try RightPaneFixture.saveCapture(rep, named: "c2-mic-denied.png") }
    }

    /// The real pane from the fixture deck: the header's menu and the mic in
    /// the composer, nothing on a call.
    func testCaptureTheThreadWithTheMic() async throws {
        guard capturing else { throw XCTSkip("set DECK_CAPTURE=1 to write the C2 thread capture") }
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        let rep = try await render(ThreadView(store: store), size: CGSize(width: 440, height: 962),
                                   settle: 1_500_000_000)
        try RightPaneFixture.saveCapture(rep, named: "c2-composer-mic.png")
    }

    /// The pane hands the open direct thread to the session, and a peer or
    /// empty pane points it at nobody.
    func testTheFeedPointsTheSessionAtTheOpenDesk() async throws {
        let (session, _, _) = fakeSession()
        let store = DeckStore(client: FixtureDeckClient(), approvalPollInterval: 600)
        await store.loadRoster()
        store.select(agent: "chief", threadID: "direct:chief")
        for _ in 0..<100 {
            if case .loaded = store.thread { break }
            try await Task.sleep(nanoseconds: 20_000_000)
        }
        VoiceFeed.push(session, thread: store.thread, agents: store.agents)
        XCTAssertEqual(session.desk, "chief")
        VoiceFeed.push(session, thread: .empty, agents: store.agents)
        XCTAssertNil(session.desk)
    }
}

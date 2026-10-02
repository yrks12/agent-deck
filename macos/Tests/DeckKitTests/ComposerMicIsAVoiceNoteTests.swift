import XCTest
@testable import DeckKit

/// Owner: "When I record a voice note on the desktop, it starts a call."
/// The composer's mic was hold-to-talk, which opened a quiet realtime call.
/// The composer's mic now records a voice note, like the iPhone's. Only the
/// header's phone button starts a call. No sound, no mic: fakes only.
@MainActor
final class ComposerMicIsAVoiceNoteTests: XCTestCase {

    private var uiDir: URL {
        URL(fileURLWithPath: #filePath).deletingLastPathComponent()
            .deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("Sources/DeckUI")
    }

    private func ui(_ file: String) throws -> String {
        try String(contentsOf: uiDir.appendingPathComponent(file), encoding: .utf8)
    }

    /// The composer accessory is the mic's whole slot.
    func testTheComposerMicNeverTouchesTheCallSession() throws {
        let thread = try ui("ThreadView.swift")
        let start = try XCTUnwrap(thread.range(of: "private var composerAccessory"))
        let tail = thread[start.upperBound...]
        let end = try XCTUnwrap(tail.range(of: "private func wireVoiceNotes"))
        let accessory = String(tail[..<end.lowerBound])
        for banned in ["MicButton", "pressMic", "releaseMic", "startCall", "voice.session"] {
            XCTAssertFalse(accessory.contains(banned), "the composer mic uses \(banned)")
        }
        XCTAssertTrue(accessory.contains("VoiceNoteButton"), "the composer mic must record a voice note")
    }

    func testHoldToTalkButtonIsGone() {
        XCTAssertFalse(FileManager.default.fileExists(atPath: uiDir.appendingPathComponent("MicButton.swift").path),
                       "hold-to-talk still exists")
    }

    /// The composer has one mic: the second waveform glyph button is merged.
    func testTheVoiceNoteButtonIsTheMicNotASecondWaveform() throws {
        let views = try ui("VoiceNoteViews.swift")
        let start = try XCTUnwrap(views.range(of: "struct VoiceNoteButton"))
        let end = try XCTUnwrap(views.range(of: "struct VoiceNoteRecordingBar"))
        let button = String(views[start.upperBound..<end.lowerBound])
        XCTAssertTrue(button.contains("\"mic\""), "the record button wears the mic glyph")
        XCTAssertFalse(button.contains("\"waveform\""), "no second waveform button")
    }

    /// Sweep: in the Mac views, a call starts from the header's phone button
    /// (`CallButton`) and the dock that builds its session, nowhere else.
    func testOnlyTheCallButtonStartsACall() throws {
        var hits: [String] = []
        for name in try FileManager.default.contentsOfDirectory(atPath: uiDir.path) where name.hasSuffix(".swift") {
            let text = try ui(name)
            for token in ["startCall(", "pressMic(", "RealtimeCallSession("] where text.contains(token) {
                hits.append("\(name):\(token)")
            }
        }
        XCTAssertEqual(hits.sorted(), ["CallBarView.swift:RealtimeCallSession(", "CallBarView.swift:startCall("])
        let bar = try ui("CallBarView.swift")
        let call = try XCTUnwrap(bar.range(of: "struct CallButton"))
        XCTAssertFalse(bar[..<call.lowerBound].contains("session.startCall()"), "a call starts outside CallButton")
    }

    /// Behaviour: recording a note opens no call, and sending it uploads.
    func testRecordingANoteStartsNoCallAndSendsTheNote() async {
        let log = VoiceLog()
        let calls = FakeCalls(log: log)
        let session = VoiceSession(input: FakeSpeechInput(log: log), output: FakeSpeechOutput(log: log),
                                   calls: { calls })
        session.sync(desk: "atlas", displayName: "Atlas", voice: nil, working: false, messages: [])
        let recorder = FakeRecorder()
        let notes = FakeVoiceNotes()
        let composer = VoiceNoteComposer(recorder: recorder, client: { notes })
        await composer.start(threadID: "direct:atlas")
        await composer.send()
        XCTAssertEqual(notes.uploads.map(\.threadID), ["direct:atlas"])
        XCTAssertTrue(calls.started.isEmpty)
        XCTAssertEqual(session.state, .idle)
        XCTAssertNil(session.call)
    }
}

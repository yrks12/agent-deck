import Foundation

/// **After a voice message, the answer is said out loud — if he wants it.**
///
/// He sent a desk a voice message; the desk's next answer in that thread is
/// read in the desk's call voice (its first two sentences, never code, paths
/// or links — `Speakable`). One answer per voice message. With **Spoken
/// replies** off it stays text: the switch is read when the answer arrives,
/// so turning it off mid-wait wins.
@MainActor
public final class ReplySpeaker {
    private struct Armed {
        var desk: String
        var after: String
        var at: Date
        var voice: DeskVoice?
    }

    private let output: SpeechOutput
    private let spokenReplies: () -> Bool
    private var armed: Armed?
    /// An answer later than this is not "the answer to what he just said".
    public var window: TimeInterval = 900

    public init(output: SpeechOutput, spokenReplies: @escaping () -> Bool = { SpokenReplies.isOn() }) {
        self.output = output
        self.spokenReplies = spokenReplies
    }

    public var isArmed: Bool { armed != nil }

    /// His voice message `messageID` went to `desk`.
    public func arm(desk: String, after messageID: String, voice: DeskVoice? = nil) {
        armed = Armed(desk: desk, after: messageID, at: Date(), voice: voice)
    }

    /// The thread on screen, every time it changes.
    public func sync(desk: String?, messages: [Message]) {
        guard let current = armed, let desk,
              desk.caseInsensitiveCompare(current.desk) == .orderedSame else { return }
        if Date().timeIntervalSince(current.at) > window { armed = nil; return }
        guard let mine = messages.firstIndex(where: { $0.id == current.after }) else { return }
        guard let reply = messages[messages.index(after: mine)...].first(where: {
            $0.role == .agent && $0.kind == .text
                && $0.author.caseInsensitiveCompare(current.desk) == .orderedSame
        }) else { return }
        armed = nil
        guard spokenReplies() else {
            VoiceTrace.note("reply.text_only", "desk=\(current.desk)")
            return
        }
        guard let words = Speakable.text(reply.text) else { return }
        VoiceTrace.note("reply.spoken", "desk=\(current.desk)")
        output.speak(SpokenLine(text: words, desk: current.desk, voice: current.voice))
    }

    /// He started talking, typed, or left: nothing more is said.
    public func stop() {
        armed = nil
        output.stop()
    }
}

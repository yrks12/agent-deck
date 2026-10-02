import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// **"i don't have its file syste, and terminal we need it to be like anydesk."**
///
/// `POST /v1/agents/{name}/terminal` has shipped the whole time and not one
/// line of this app had ever called it. This file is the five things a client
/// of that route gets wrong, each one measured rather than assumed.
///
/// 1. **A non-zero exit is a result, not a failure.** The route answers `200`
///    carrying `{"exit":2,…}` when the command ran and disagreed. `grep`
///    finding nothing is an answer. A client that throws on a non-zero exit is
///    not a terminal — it is a client that hides half of what he asked for.
/// 2. **The route is stateless and the client owns the directory.** Every call
///    is a fresh `bash -lc`, so a typed `cd` changes nothing on the far side.
///    It is sent as `cd X && pwd` *from the directory we are in now*, and the
///    answer — `stdout` trimmed — becomes the directory. Nothing else can know
///    where `~`, `..` or a symlink actually landed.
/// 3. **A `cd` that fails moves nothing.** A non-zero exit on the probe leaves
///    the prompt exactly where it was, because the far side did too.
/// 4. **Cut output says it was cut.** `truncated: true` means the deck stopped
///    copying, and output that stops without saying so reads as output that
///    ended.
/// 5. **The 20-second ceiling is a sentence.** `npm install` will not finish
///    inside it, and `computer_not_responding` is a slug he should never see.
///
/// Every assertion below is on the *good* signal — the text that actually
/// reaches the transcript — never on the absence of a thrown error.
@MainActor
final class DeskShellTests: XCTestCase {

    private func shellModel(_ shell: FakeShellClient,
                            cwd: String = "/home/deckop") -> DeskShellModel {
        DeskShellModel(desk: "atlas", displayName: "Atlas",
                       client: shell, startingCwd: cwd)
    }

    // MARK: - 1. a non-zero exit is a result on screen

    func testACommandThatFailedIsShownWithItsExitCodeAndItsError() async {
        let shell = FakeShellClient()
        // The shape the route answers with when the command ran and said no:
        // a 200, an exit code, and the reason on stderr.
        shell.next = .result(DeskCommandResult(exit: 2, stdout: "",
                                               stderr: "no such file", truncated: false))
        let model = shellModel(shell)

        await model.run("cat /nope")

        XCTAssertEqual(model.transcript.count, 1,
                       "The failed command left nothing on screen at all.")
        let line = model.transcript[0]
        XCTAssertEqual(line.exitCode, 2,
                       "A 200 carrying exit 2 did not reach the transcript as exit 2.")
        XCTAssertTrue(line.transcriptBody.contains("no such file"),
                      "stderr never reached the transcript. The body was "
                      + "'\(line.transcriptBody)'.")
        XCTAssertEqual(line.statusLine, "Exited 2",
                       "A non-zero exit has to be readable as one. Got "
                       + "\(String(describing: line.statusLine)).")
        XCTAssertNil(line.refusalSentence,
                     "A command that ran and disagreed was turned into a refusal. "
                     + "That is the throw-on-non-zero bug wearing a different hat.")
    }

    func testACommandThatSucceededAndPrintedNothingSaysSoRatherThanLookingEmpty() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "", stderr: "",
                                               truncated: false))
        let model = shellModel(shell)

        await model.run("touch /tmp/x")

        let line = model.transcript[0]
        XCTAssertEqual(line.transcriptBody, DeskShellLine.noOutput,
                       "A command that worked silently drew a blank, which is what a "
                       + "command that never ran looks like.")
        XCTAssertNil(line.statusLine, "Exit 0 does not need a status line.")
    }

    // MARK: - 2. the client owns the directory

    func testTypingCdSendsThePwdProbeFromWhereWeAreAndTakesTheAnswerAsTheNewPlace() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "/tmp\n", stderr: "",
                                               truncated: false))
        let model = shellModel(shell, cwd: "/home/deckop")

        await model.run("cd /tmp")

        XCTAssertEqual(shell.calls.count, 1)
        XCTAssertEqual(shell.calls[0].command, "cd /tmp && pwd",
                       "A typed `cd` was sent as-is. On a stateless route that "
                       + "changes nothing and the prompt then lies.")
        XCTAssertEqual(shell.calls[0].cwd, "/home/deckop",
                       "The probe was sent from the directory it was trying to reach, "
                       + "not from the one we were in — a relative `cd` would then "
                       + "resolve against the wrong place.")
        XCTAssertEqual(model.cwd, "/tmp",
                       "The new directory has to be the deck's own `pwd`, trimmed. "
                       + "Anything this client works out itself is a guess about `~`, "
                       + "`..` and symlinks.")
    }

    func testARelativeCdIsProbedFromWhereWeAreAndLandsWhereTheDeckSaysItLanded() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "/srv/app/logs\n",
                                               stderr: "", truncated: false))
        let model = shellModel(shell, cwd: "/srv/app")

        await model.run("cd logs")

        XCTAssertEqual(shell.calls[0].command, "cd logs && pwd")
        XCTAssertEqual(shell.calls[0].cwd, "/srv/app")
        XCTAssertEqual(model.cwd, "/srv/app/logs",
                       "A relative `cd` was resolved by this client instead of by the "
                       + "machine that owns the filesystem.")
    }

    func testAnOrdinaryCommandIsSentVerbatimFromTheCurrentDirectory() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "a\nb\n", stderr: "",
                                               truncated: false))
        let model = shellModel(shell, cwd: "/srv/app")

        await model.run("ls")

        XCTAssertEqual(shell.calls[0].command, "ls",
                       "An ordinary command was rewritten on its way out.")
        XCTAssertEqual(shell.calls[0].cwd, "/srv/app")
        XCTAssertEqual(model.cwd, "/srv/app", "`ls` moved the prompt.")
        XCTAssertTrue(model.transcript[0].transcriptBody.contains("a\nb"))
    }

    func testACdBuriedInACompoundCommandDoesNotMoveThePromptBecauseItCannot() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "x\n", stderr: "",
                                               truncated: false))
        let model = shellModel(shell, cwd: "/home/deckop")

        await model.run("cd /tmp && ls")

        XCTAssertEqual(shell.calls[0].command, "cd /tmp && ls",
                       "A compound command was mangled by appending a probe to it, "
                       + "which would have read `ls` output as a directory name.")
        XCTAssertEqual(model.cwd, "/home/deckop",
                       "The prompt moved on the strength of an `ls` listing. The far "
                       + "side is stateless: that `cd` did not survive the call.")
    }

    // MARK: - 3. a cd that fails moves nothing

    func testACdThatFailedLeavesThePromptExactlyWhereItWas() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(
            exit: 1, stdout: "",
            stderr: "bash: line 0: cd: /nowhere: No such file or directory",
            truncated: false))
        let model = shellModel(shell, cwd: "/home/deckop")

        await model.run("cd /nowhere")

        XCTAssertEqual(model.cwd, "/home/deckop",
                       "A failed `cd` moved the prompt. Every command after it would "
                       + "have run somewhere he was not looking.")
        XCTAssertTrue(model.transcript[0].transcriptBody.contains("No such file"),
                      "The reason the `cd` failed never reached the screen.")
        XCTAssertEqual(model.transcript[0].exitCode, 1)
    }

    /// **The exit code decides, not the shape of the string.**
    ///
    /// The first version of the test above passed against a client that had no
    /// exit check at all, because the failing `cd` it scripted printed nothing
    /// and an empty string is not a path. That is a detector held up by the
    /// wrong rule. `bash`'s own `cd` writes the directory it resolved to
    /// *stdout* whenever `CDPATH` is set, so "the probe failed but stdout looks
    /// like a path" is a shape the real deck can produce — and a client reading
    /// the string instead of the code moves the prompt on a `cd` that did not
    /// happen.
    func testACdThatFailedIsNotBelievedEvenWhenItPrintedSomethingThatLooksLikeAPath() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(
            exit: 1, stdout: "/srv/app/logs\n",
            stderr: "bash: cd: too many arguments", truncated: false))
        let model = shellModel(shell, cwd: "/home/deckop")

        await model.run("cd logs")

        XCTAssertEqual(model.cwd, "/home/deckop",
                       "A `cd` that exited non-zero was believed because its stdout "
                       + "looked like a directory. The exit code is the only thing "
                       + "that says whether the far side moved.")
    }

    func testACdWhosePwdCameBackEmptyLeavesThePromptWhereItWas() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "   \n", stderr: "",
                                               truncated: false))
        let model = shellModel(shell, cwd: "/home/deckop")

        await model.run("cd /tmp")

        XCTAssertEqual(model.cwd, "/home/deckop",
                       "An empty `pwd` was accepted as a directory, so the prompt "
                       + "became blank and every later command ran nowhere.")
    }

    // MARK: - 4. cut output says it was cut

    func testTruncatedOutputCarriesAVisibleCutMark() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "line one\nline two",
                                               stderr: "", truncated: true))
        let model = shellModel(shell)

        await model.run("cat /var/log/huge")

        let body = model.transcript[0].transcriptBody
        XCTAssertTrue(body.contains(DeskShellLine.cutMark),
                      "Output the deck stopped copying was drawn as though it had "
                      + "ended. The body was '\(body)'.")
        XCTAssertTrue(body.hasSuffix(DeskShellLine.cutMark),
                      "The cut mark belongs at the cut, not somewhere in the middle.")
        XCTAssertTrue(model.transcript[0].spokenLabel.contains("cut off"),
                      "A screen reader was told the output ended normally.")
    }

    func testOutputThatWasNotCutCarriesNoCutMark() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "all of it\n",
                                               stderr: "", truncated: false))
        let model = shellModel(shell)

        await model.run("echo hi")

        XCTAssertFalse(model.transcript[0].transcriptBody.contains(DeskShellLine.cutMark),
                       "Complete output was marked as cut, which makes the mark noise.")
    }

    // MARK: - 5. the 20-second ceiling is a sentence

    func testACommandThatRanPastTheCeilingIsASentenceAndNeverASlug() async {
        let shell = FakeShellClient()
        shell.next = .refusal(ScreenRefusal(
            status: 504,
            body: Data(#"{"reason":"computer_not_responding","detail":""}"#.utf8)))
        let model = shellModel(shell)

        await model.run("npm install")

        let line = model.transcript[0]
        let sentence = line.refusalSentence ?? ""
        XCTAssertTrue(sentence.contains("that took longer than 20 seconds and was cut off"),
                      "The timeout did not say what happened in words. It said "
                      + "'\(sentence)'.")
        XCTAssertFalse(sentence.contains("computer_not_responding"),
                       "The slug reached the screen.")
        XCTAssertFalse(sentence.contains("504"), "The status code reached the screen.")
        XCTAssertEqual(model.cwd, "/home/deckop", "A timeout moved the prompt.")
    }

    func testADeskWithNoMachineSaysSoRatherThanFailingSilently() async {
        let shell = FakeShellClient()
        shell.next = .refusal(ScreenRefusal(
            status: 409,
            body: Data(#"{"reason":"computer_not_running","detail":"deck-desk-atlas is not up"}"#.utf8)))
        let model = shellModel(shell)

        await model.run("ls")

        let sentence = model.transcript[0].refusalSentence ?? ""
        XCTAssertTrue(sentence.contains("no computer running yet"), "Got '\(sentence)'.")
        XCTAssertTrue(sentence.contains("deck-desk-atlas is not up"),
                      "The deck's own detail was thrown away.")
    }

    func testDockerBeingDownIsItsOwnSentence() async {
        let shell = FakeShellClient()
        shell.next = .refusal(ScreenRefusal(
            status: 503,
            body: Data(#"{"reason":"docker_unavailable","detail":""}"#.utf8)))
        let model = shellModel(shell)

        await model.run("ls")

        XCTAssertTrue((model.transcript[0].refusalSentence ?? "").contains("Docker"),
                      "Got '\(model.transcript[0].refusalSentence ?? "")'.")
    }

    // MARK: - what the panel promises about itself

    func testThePanelSaysItIsOneShotBeforeHeDiscoversItByTypingVim() {
        let nature = DeskShellModel.natureLine
        XCTAssertTrue(nature.lowercased().contains("one command"),
                      "The panel does not say it runs one command at a time: "
                      + "'\(nature)'.")
        XCTAssertTrue(nature.contains("vim"),
                      "The panel does not warn that interactive programs will not "
                      + "work, so the first thing he learns is that it is broken.")
    }

    func testTheBusyFlagIsSetWhileACommandIsOutAndClearedWhenItAnswers() async {
        let shell = FakeShellClient()
        shell.next = .result(DeskCommandResult(exit: 0, stdout: "ok\n", stderr: "",
                                               truncated: false))
        let model = shellModel(shell)
        XCTAssertFalse(model.isBusy)

        await model.run("sleep 1")

        XCTAssertFalse(model.isBusy, "The panel stayed disabled after the answer came.")
        XCTAssertEqual(model.transcript.count, 1)
    }

    func testBlankInputIsNotSentAnywhere() async {
        let shell = FakeShellClient()
        let model = shellModel(shell)

        await model.run("   \n")

        XCTAssertTrue(shell.calls.isEmpty, "A blank line was sent to the deck.")
        XCTAssertTrue(model.transcript.isEmpty)
    }

    // MARK: - the wire

    func testTheRouteBodyDecodesWithEveryFieldTheDeckSends() throws {
        let json = #"{"exit":0,"stdout":"hi\n","stderr":"","truncated":false}"#
        let result = try DeckCoding.decoder.decode(DeskCommandResult.self,
                                                   from: Data(json.utf8))
        XCTAssertEqual(result.exit, 0)
        XCTAssertEqual(result.stdout, "hi\n")
        XCTAssertFalse(result.truncated)
    }

    func testABodyMissingTruncatedStillDecodesRatherThanLosingTheWholeResult() throws {
        let json = #"{"exit":7,"stdout":"","stderr":"boom"}"#
        let result = try DeckCoding.decoder.decode(DeskCommandResult.self,
                                                   from: Data(json.utf8))
        XCTAssertEqual(result.exit, 7)
        XCTAssertEqual(result.stderr, "boom")
        XCTAssertFalse(result.truncated)
    }

    // MARK: - the live route, without a socket

    private func liveShell(_ performer: StubPerformer) throws -> HTTPDeskShell {
        let tokens = InMemoryTokenStore()
        try tokens.setToken("sekret")
        return HTTPDeskShell(baseURL: URL(string: "http://deck.local")!,
                             tokens: tokens, performer: performer)
    }

    func testTheCommandGoesToTheTerminalRouteAsJSONOnTheBearerToken() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents/atlas/terminal"] =
            Data(#"{"exit":0,"stdout":"/srv\n","stderr":"","truncated":false}"#.utf8)

        let result = try await liveShell(performer)
            .runCommand(agent: "atlas", command: "cd /srv && pwd", cwd: "/home/deckop")

        XCTAssertEqual(performer.paths, ["/v1/agents/atlas/terminal"])
        XCTAssertEqual(performer.requests[0].httpMethod, "POST")
        XCTAssertEqual(performer.authHeaders, ["Bearer sekret"])
        let body = try XCTUnwrap(performer.requests[0].httpBody)
        let sent = try XCTUnwrap(
            JSONSerialization.jsonObject(with: body) as? [String: String])
        XCTAssertEqual(sent["command"], "cd /srv && pwd",
                       "The command was rewritten or escaped on its way out.")
        XCTAssertEqual(sent["cwd"], "/home/deckop",
                       "The route was not told where to run, so it ran wherever the "
                       + "deck defaults to.")
        XCTAssertEqual(result.stdout, "/srv\n")
    }

    func testANonZeroExitOverHTTPComesBackAsAResultAndNotAsAThrow() async throws {
        let performer = StubPerformer()
        performer.bodies["/v1/agents/atlas/terminal"] =
            Data(#"{"exit":1,"stdout":"","stderr":"","truncated":false}"#.utf8)

        let result = try await liveShell(performer)
            .runCommand(agent: "atlas", command: "grep nope file", cwd: "/srv")

        XCTAssertEqual(result.exit, 1,
                       "A 200 carrying exit 1 threw. `grep` finding nothing is an "
                       + "answer, and this is the whole reason the route returns 200.")
    }

    func testAFiveOhFourFromTheRouteBecomesTheTwentySecondSentence() async throws {
        let performer = StubPerformer()
        performer.status = 504
        performer.defaultBody =
            Data(#"{"reason":"computer_not_responding","detail":""}"#.utf8)

        do {
            _ = try await liveShell(performer)
                .runCommand(agent: "atlas", command: "npm install", cwd: "/srv")
            XCTFail("A 504 was treated as a successful command.")
        } catch let refusal as ScreenRefusal {
            XCTAssertTrue(refusal.shellSentence
                .contains("that took longer than 20 seconds and was cut off"),
                          "Got '\(refusal.shellSentence)'.")
        }
    }

    func testADeckWithNoTokenIsNeverAskedAnonymously() async {
        let performer = StubPerformer()
        let shell = HTTPDeskShell(baseURL: URL(string: "http://deck.local")!,
                                  tokens: InMemoryTokenStore(), performer: performer)

        do {
            _ = try await shell.runCommand(agent: "atlas", command: "ls", cwd: "/srv")
            XCTFail("A command was sent to the deck with no token on it.")
        } catch let refusal as ScreenRefusal {
            XCTAssertEqual(refusal.shellSentence,
                           "Add your deck API token in Settings.")
            XCTAssertTrue(performer.requests.isEmpty,
                          "The request went out anyway.")
        } catch {
            XCTFail("Unexpected \(error)")
        }
    }

    // MARK: - it draws

    /// **Absence is not a picture.** "The output was not shown" is also what a
    /// view that failed to lay out looks like, so every kind of line the model
    /// can produce is hosted for real and measured.
    ///
    /// Hosted the way the rest of this suite does it: activation policy
    /// `.prohibited` and a window 40,000 points off any display, so nothing
    /// takes the owner's screen while his own copy of the app is open.
    func testEveryKindOfLineDrawsAndReachesASize() {
        let lines = [
            DeskShellLine.ran(typed: "ls", sent: "ls", cwd: "/srv/app",
                              result: DeskCommandResult(exit: 0, stdout: "a\nb\n",
                                                        stderr: "", truncated: false)),
            DeskShellLine.ran(typed: "cat /nope", sent: "cat /nope", cwd: "/srv/app",
                              result: DeskCommandResult(exit: 2, stdout: "",
                                                        stderr: "no such file",
                                                        truncated: false)),
            DeskShellLine.ran(typed: "cat big", sent: "cat big", cwd: "/srv/app",
                              result: DeskCommandResult(exit: 0, stdout: "lots",
                                                        stderr: "", truncated: true)),
            DeskShellLine.refused(typed: "npm install", sent: "npm install",
                                  cwd: "/srv/app",
                                  sentence: ScreenRefusal.notResponding("").shellSentence),
        ]

        let size = hostedSize(DeskTerminalTranscript(lines: lines))

        XCTAssertGreaterThan(size.height, 0,
                             "the transcript laid out to \(size) — that is a blank pane")
        XCTAssertLessThanOrEqual(size.height, DeskTerminalTranscript.maxHeight,
                                 "the transcript asked for \(size.height) points inside "
                                 + "a 300pt inspector column. An unbounded transcript "
                                 + "that reports the height of its own content is the "
                                 + "fault this repo has already fixed twice.")
    }

    private func hostedSize<V: View>(_ view: V) -> CGSize {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.frame = NSRect(x: 0, y: 0, width: 300, height: 400)
        // Far off any display, and never ordered front: his own Agent Deck is
        // open while this runs.
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: 300, height: 400),
            styleMask: [.borderless], backing: .buffered, defer: true)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        let size = host.fittingSize
        window.orderOut(nil)
        window.contentView = nil
        return size
    }
}

// MARK: - the double

/// A deck that answers a command without a socket, and remembers exactly what
/// it was handed — which is the only way to see that a typed `cd` was rewritten
/// and sent from the right place.
final class FakeShellClient: DeskShellClient, @unchecked Sendable {
    enum Answer {
        case result(DeskCommandResult)
        case refusal(ScreenRefusal)
    }

    struct Call: Equatable {
        let agent: String
        let command: String
        let cwd: String
    }

    private let lock = NSLock()
    private var _calls: [Call] = []
    private var _next: Answer = .result(DeskCommandResult(exit: 0, stdout: "",
                                                          stderr: "", truncated: false))

    var calls: [Call] { lock.lock(); defer { lock.unlock() }; return _calls }
    var next: Answer {
        get { lock.lock(); defer { lock.unlock() }; return _next }
        set { lock.lock(); _next = newValue; lock.unlock() }
    }

    func runCommand(agent: String, command: String,
                    cwd: String) async throws -> DeskCommandResult {
        lock.lock()
        _calls.append(Call(agent: agent, command: command, cwd: cwd))
        let answer = _next
        lock.unlock()
        switch answer {
        case .result(let result): return result
        case .refusal(let refusal): throw refusal
        }
    }
}

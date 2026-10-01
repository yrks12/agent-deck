import Foundation

/// **"i don't have its file syste, and terminal we need it to be like anydesk."**
///
/// `POST /v1/agents/{name}/terminal` has been on the deck the whole time and
/// not one line of this app had ever called it. This file is the client half:
/// the route, the directory the route refuses to remember, and the sentences
/// its refusals are turned into.
///
/// **The route is stateless, and that is the whole design.** Every call is a
/// fresh `bash -lc`. There is no session, nothing stays open, and nothing on
/// the far side remembers where the last command was run. Four consequences,
/// each of which is a way a client of this route goes wrong, and each of which
/// has a test in `DeskShellTests`:
///
/// **1. A non-zero exit is a result, not a failure.** The deck answers `200`
/// carrying `{"exit":2,"stdout":"","stderr":"no such file"}` whenever the
/// command *ran* — the command disagreeing is what happened, not an error in
/// asking. `grep` finding nothing exits 1 and that is the answer. A client
/// that throws here shows him an error where a result belongs and loses the
/// stderr that says why.
///
/// **2. This side owns the working directory.** A typed `cd` cannot survive
/// the call, so it is never sent as typed: it goes as `cd X && pwd` *from the
/// directory we are currently in*, and the deck's own `pwd` — `stdout`
/// trimmed — becomes the new one. Resolving `~`, `..` or a symlink here would
/// be this app guessing about a filesystem it cannot see.
///
/// **3. A `cd` that fails moves nothing**, because nothing moved over there
/// either.
///
/// **4. Twenty seconds, then it is cut off.** The deck's ceiling, and it is
/// not negotiable from here. `npm install` will not finish inside it. He must
/// read that as a sentence — `computer_not_responding` is a slug, and a slug
/// on screen is a bug report he has to translate.
///
/// This file imports no UI framework, so all four are exercisable with no
/// window and no deck.

// MARK: - the wire

/// `200 -> {"exit":N,"stdout":"…","stderr":"…","truncated":bool}`.
///
/// **`truncated` is optional on the way in.** A deck that omits it has not
/// said the output was cut, and a decode that failed over a missing boolean
/// would throw away the exit code and both streams — everything he asked for
/// — over the one field that is only ever a footnote.
public struct DeskCommandResult: Equatable, Sendable, Decodable {
    public let exit: Int
    public let stdout: String
    public let stderr: String
    public let truncated: Bool

    public init(exit: Int, stdout: String, stderr: String, truncated: Bool) {
        self.exit = exit
        self.stdout = stdout
        self.stderr = stderr
        self.truncated = truncated
    }

    private enum CodingKeys: String, CodingKey { case exit, stdout, stderr, truncated }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        exit = try container.decode(Int.self, forKey: .exit)
        stdout = try container.decodeIfPresent(String.self, forKey: .stdout) ?? ""
        stderr = try container.decodeIfPresent(String.self, forKey: .stderr) ?? ""
        truncated = try container.decodeIfPresent(Bool.self, forKey: .truncated) ?? false
    }
}

/// The one route.
///
/// Its own protocol rather than another member on `DeckClient`, for the reason
/// `AgentScreenClient` gives: every existing conformer would otherwise grow a
/// stub for a surface it has nothing to do with, and a stub that throws is a
/// silent failure waiting for a caller.
///
/// `cwd` is a parameter and not state, because the route has no state to hold
/// it in. Whoever calls this is the only thing that knows where the last
/// command landed.
public protocol DeskShellClient: Sendable {
    /// `POST /v1/agents/{name}/terminal` with `{"command": …, "cwd": …}`.
    ///
    /// Throws `ScreenRefusal` when the deck refused to run it at all. It does
    /// **not** throw when the command ran and exited non-zero: that is a
    /// `DeskCommandResult` with a non-zero `exit`.
    func runCommand(agent: String, command: String,
                    cwd: String) async throws -> DeskCommandResult
}

// MARK: - one line of the transcript

/// What one typed command left on screen, worked out once, away from SwiftUI
/// so it can be asserted on.
///
/// Every state a command can end in is one of these — there is no fifth shape
/// and no case where a command leaves nothing behind. A command that was
/// refused is a line too; a refusal that scrolled past silently is
/// indistinguishable from a command that was never sent.
public struct DeskShellLine: Identifiable, Equatable, Sendable {
    /// **Output that stops without saying so reads as output that ended.**
    /// `truncated: true` means the deck stopped copying, and a directory
    /// listing cut at the ceiling looks exactly like a short directory.
    public static let cutMark = "⋯ cut off here — there was more output than the deck sends back."
    /// A command that worked and printed nothing must not draw a blank: a blank
    /// is also what a command that never ran looks like.
    public static let noOutput = "(no output)"

    public let id: UUID
    /// Where it ran. Kept per line, because the prompt moves and a transcript
    /// that only shows the *current* directory misreads its own history.
    public let cwd: String
    /// What he typed.
    public let typed: String
    /// What actually went down the wire. Different from `typed` only for a
    /// `cd`, and shown in the panel's tooltip so the rewrite is never a secret.
    public let sent: String
    /// Nil when the deck refused to run it at all.
    public let exitCode: Int?
    /// Nil when the command ran — *including* when it ran and failed.
    public let refusalSentence: String?
    public let transcriptBody: String
    /// "Exited 2", or nil on a clean exit. Success needs no announcement.
    public let statusLine: String?
    public let spokenLabel: String

    /// The command ran. Whatever it exited with, this is a result.
    public static func ran(typed: String, sent: String, cwd: String,
                           result: DeskCommandResult,
                           id: UUID = UUID()) -> DeskShellLine {
        var body = [result.stdout, result.stderr]
            .map { $0.trimmingCharacters(in: .newlines) }
            .filter { !$0.isEmpty }
            .joined(separator: "\n")
        if body.isEmpty { body = noOutput }
        if result.truncated { body += "\n" + cutMark }

        var spoken = result.exit == 0
            ? "\(typed). Finished."
            : "\(typed). Exited \(result.exit)."
        if result.truncated { spoken += " The output was cut off." }

        return DeskShellLine(
            id: id, cwd: cwd, typed: typed, sent: sent,
            exitCode: result.exit,
            refusalSentence: nil,
            transcriptBody: body,
            statusLine: result.exit == 0 ? nil : "Exited \(result.exit)",
            spokenLabel: spoken)
    }

    /// The deck would not run it. The sentence is the body — there is no output
    /// to show and an empty box under a refused command says nothing.
    public static func refused(typed: String, sent: String, cwd: String,
                               sentence: String,
                               id: UUID = UUID()) -> DeskShellLine {
        DeskShellLine(
            id: id, cwd: cwd, typed: typed, sent: sent,
            exitCode: nil,
            refusalSentence: sentence,
            transcriptBody: sentence,
            statusLine: nil,
            spokenLabel: "\(typed). \(sentence)")
    }
}

// MARK: - what a refusal says on this surface

/// The terminal route refuses in the same vocabulary the screen routes do —
/// same container, same slugs — so `ScreenRefusal` reads the wire for both and
/// there is one place that knows what `computer_not_running` means.
///
/// The **sentences** are this surface's own, which is why they are here and not
/// there: `ScreenRefusal.sentence` says *"The computer stopped answering"* for
/// `computer_not_responding`, which is true of a dead screen feed and wrong
/// about a command. A command that hit the ceiling did not find a dead machine
/// — it ran too long and was cut off, and the difference decides whether he
/// waits or types something shorter.
extension ScreenRefusal {
    public var shellSentence: String {
        switch self {
        case .notResponding:
            // The ceiling is the deck's, stated in words. He must never be
            // shown `computer_not_responding` or a 504 for this.
            return "Nothing came back: that took longer than "
                + "\(DeskShellModel.ceilingSeconds) seconds and was cut off. A long "
                + "job such as an install cannot finish here — start it in the "
                + "background and look at it afterwards."
        case .noComputerYet(let detail):
            return withShellDetail("This desk has no computer running yet, so there "
                                   + "is nothing to run a command on.", detail)
        case .dockerUnavailable(let detail):
            return withShellDetail("Docker is not answering on the deck's machine, so "
                                   + "no desk can run anything right now.", detail)
        case .badInput(let detail):
            return withShellDetail("The deck would not accept that command.", detail)
        case .inputRefused(let detail):
            return withShellDetail("The computer would not run that.", detail)
        case .noFrame(let detail):
            return withShellDetail("The computer is up, but it did not answer.", detail)
        case .unknownAgent(let detail):
            return withShellDetail("That desk is no longer on this deck.", detail)
        case .unauthorized:
            return "The deck rejected this token. Set a different one in Settings."
        case .authNotConfigured:
            return "This deck is closed until its own API token is set on the server."
        case .missingToken:
            return "Add your deck API token in Settings."
        case .transport(let detail):
            return withShellDetail("Could not reach the deck.", detail)
        case .other(let status, _, let detail):
            return withShellDetail("The deck answered \(status).", detail)
        }
    }
}

/// The deck's own detail kept, because "the machine is not up" without naming
/// the container is half a message.
private func withShellDetail(_ sentence: String, _ detail: String) -> String {
    let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
    return trimmed.isEmpty ? sentence : "\(sentence) \(trimmed)"
}

// MARK: - the panel's model

/// **The terminal panel's own model. It does not touch `DeckStore`.**
///
/// Same rule as `AgentComputerModel`: `DeckStore.composerDraft` is
/// `@Published`, so anything publishing on the store rebuilds every view
/// observing it. This object publishes only to the panel that owns it.
@MainActor
public final class DeskShellModel: ObservableObject {
    /// The deck's wall clock on a command, stated here so the sentence and the
    /// number cannot drift apart. **Not a setting** — it is the server's, and
    /// this app only reports it.
    public static let ceilingSeconds = 20

    /// What the panel says about itself before he finds out by typing `vim`.
    ///
    /// A box with a prompt in it promises a shell. This one is not a shell,
    /// and the honest thing is to say so in the panel rather than let him
    /// discover it from a command that hangs for twenty seconds and dies.
    public static let natureLine =
        "One command at a time, each run on its own. Nothing stays open between "
        + "them, so vim, top and ssh cannot work here — and anything longer than "
        + "\(ceilingSeconds) seconds is cut off."

    /// **Where every desk's terminal opens: its own computer's home.** The
    /// route runs inside `deck-desk-<name>`, whose home is this bind mount —
    /// `server/sandbox.py` `DESK_HOME`, pinned by tests/test_terminal_home.py.
    /// MEASURED 2026-09-30: opening at the desk's *host* workspace instead made
    /// every first command an OCI "chdir to cwd … no such file", because that
    /// path does not exist inside the container.
    public static let computerHome = "/home/agent"

    public let desk: String
    public let displayName: String

    /// Everything run in this panel, oldest first.
    @Published public private(set) var transcript: [DeskShellLine] = []
    /// Where the next command will run. **The client's, because the route has
    /// nowhere to keep it.**
    @Published public private(set) var cwd: String
    /// A command is out. The panel disables its field on this rather than
    /// leaving a second command to race the first through a stateless route.
    @Published public private(set) var isBusy = false

    private let client: DeskShellClient

    public init(desk: String, displayName: String, client: DeskShellClient,
                startingCwd: String) {
        self.desk = desk
        self.displayName = displayName
        self.client = client
        self.cwd = startingCwd
    }

    /// **What a typed `cd` is actually sent as, or nil if it is not one.**
    ///
    /// `cd X` alone becomes `cd X && pwd`: the `&&` means a `cd` that failed
    /// never reaches `pwd`, so a non-zero exit is exactly "we did not move".
    ///
    /// A `cd` welded to anything else — `cd /tmp && ls` — is **not** probed and
    /// is sent verbatim. Appending `&& pwd` there would print the listing and
    /// the directory into one stream and this would read the last line of `ls`
    /// as a folder. It is also the honest answer: that `cd` genuinely does not
    /// survive the call, so the prompt genuinely has not moved.
    public static func directoryProbe(for typed: String) -> String? {
        let trimmed = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed == "cd" || trimmed.hasPrefix("cd ") else { return nil }
        let welds: [Character] = ["&", "|", ";", "\n", "`", "$", ">", "<"]
        guard !trimmed.contains(where: { welds.contains($0) }) else { return nil }
        return "\(trimmed) && pwd"
    }

    /// Run one line. Never throws: everything the deck can say ends up on
    /// screen as a line of the transcript, because a command that vanished
    /// silently is the one outcome he cannot act on.
    public func run(_ typed: String) async {
        let command = typed.trimmingCharacters(in: .whitespacesAndNewlines)
        // Nothing typed is nothing to run. A blank `bash -lc` is a round trip
        // that can only ever answer with an empty box.
        guard !command.isEmpty else { return }

        let probe = Self.directoryProbe(for: command)
        let sent = probe ?? command
        let from = cwd

        isBusy = true
        defer { isBusy = false }

        do {
            let result = try await client.runCommand(agent: desk, command: sent, cwd: from)
            transcript.append(.ran(typed: command, sent: sent, cwd: from, result: result))
            guard probe != nil, result.exit == 0 else { return }
            // The deck's own `pwd`, trimmed — never this app's arithmetic on
            // the path it was given. An answer that is not an absolute path is
            // not a directory, and taking it would leave the prompt blank and
            // every later command running somewhere nobody chose.
            let landed = result.stdout.trimmingCharacters(in: .whitespacesAndNewlines)
            if landed.hasPrefix("/") { cwd = landed }
        } catch let refusal as ScreenRefusal {
            transcript.append(.refused(typed: command, sent: sent, cwd: from,
                                       sentence: refusal.shellSentence))
        } catch {
            transcript.append(.refused(
                typed: command, sent: sent, cwd: from,
                sentence: ScreenRefusal.transport(error.localizedDescription).shellSentence))
        }
    }
}

// MARK: - the deck, over HTTP

/// `POST /v1/agents/{name}/terminal`, on the same bearer token the rest of the
/// app already holds.
///
/// **A struct of its own rather than an extension on `HTTPDeckClient`.** That
/// was the intended shape and it does not compile: `HTTPDeckClient`'s
/// `baseURL`, `tokens`, `performer` and `makeRequest` are all `private`, which
/// in Swift means the same *file* — an extension in this one can see none of
/// them. Rather than edit that file, this builds the same request from the
/// same public pieces, and the app hands one of these to the store beside the
/// deck client.
public struct HTTPDeskShell: DeskShellClient {
    private let baseURL: URL
    private let tokens: TokenStore
    private let performer: RequestPerformer

    public init(baseURL: URL, tokens: TokenStore,
                performer: RequestPerformer = URLSessionPerformer()) {
        self.baseURL = baseURL
        self.tokens = tokens
        self.performer = performer
    }

    public func runCommand(agent: String, command: String,
                           cwd: String) async throws -> DeskCommandResult {
        let request = try makeShellRequest(agent: agent, command: command, cwd: cwd)

        let data: Data
        let response: HTTPURLResponse
        do {
            (data, response) = try await performer.perform(request)
        } catch let error as DeckError {
            if case .transport(let detail) = error { throw ScreenRefusal.transport(detail) }
            throw ScreenRefusal.transport(error.userFacingText)
        } catch {
            throw ScreenRefusal.transport(error.localizedDescription)
        }

        // **Only a non-2xx is a refusal.** A 200 carrying a non-zero `exit` is
        // a command that ran and disagreed, and it comes back as a result.
        guard (200..<300).contains(response.statusCode) else {
            throw ScreenRefusal(status: response.statusCode, body: data)
        }
        do {
            return try DeckCoding.decoder.decode(DeskCommandResult.self, from: data)
        } catch {
            throw ScreenRefusal.transport("the deck's terminal answer did not decode: "
                                          + String(describing: error))
        }
    }

    private func makeShellRequest(agent: String, command: String,
                                  cwd: String) throws -> URLRequest {
        // Refused here rather than sent anonymously, exactly as the rest of
        // the adapter does: an unauthenticated request to this deck is never a
        // useful thing to have done.
        guard let token = try? tokens.token(), !token.isEmpty else {
            throw ScreenRefusal.missingToken
        }
        guard var components = URLComponents(url: baseURL, resolvingAgainstBaseURL: false) else {
            throw ScreenRefusal.transport("bad base URL")
        }
        components.path = "/v1/agents/\(agent)/terminal"
        guard let url = components.url else {
            throw ScreenRefusal.transport("could not build the terminal route for \(agent)")
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        do {
            // Serialised, never interpolated. He types quotes, backticks and
            // `$` at this prompt and every one of them is a character.
            request.httpBody = try JSONSerialization.data(
                withJSONObject: ["command": command, "cwd": cwd])
        } catch {
            throw ScreenRefusal.transport("could not encode that command")
        }
        return request
    }
}

// MARK: - the fixture

/// A deck with no Docker at all, answering the way the real one does when a
/// desk has no machine — so the panel's refusal path is on screen in the
/// fixture app rather than only against a live box.
extension FixtureDeckClient: DeskShellClient {
    public func runCommand(agent: String, command: String,
                           cwd: String) async throws -> DeskCommandResult {
        guard agents.contains(where: { $0.name == agent }) else {
            throw ScreenRefusal.unknownAgent("no desk named '\(agent)'")
        }
        guard agent == "chief" else {
            throw ScreenRefusal.noComputerYet("deck-desk-\(agent) is not up")
        }
        // `pwd` is the one command the fixture can answer honestly, because the
        // answer is the directory it was handed. Everything else would be this
        // app inventing a filesystem, which is the thing the panel exists to
        // stop doing.
        if command == "pwd" || command.hasSuffix("&& pwd") {
            return DeskCommandResult(exit: 0, stdout: cwd + "\n", stderr: "",
                                     truncated: false)
        }
        return DeskCommandResult(
            exit: 127, stdout: "",
            stderr: "this is the fixture — no machine is running, so '\(command)' "
                + "was not run anywhere",
            truncated: false)
    }
}

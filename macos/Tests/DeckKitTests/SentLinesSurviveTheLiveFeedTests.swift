import XCTest
@testable import DeckKit
@testable import DeckUI

/// **His sent lines and the desk's replies stay on screen while he watches.**
///
/// His words, 2026-10-01: *"sometimes I need to get out of the chat and come
/// back, otherwise my messages disappear, and also the agents' messages."*
///
/// The deck answers a send with the stored message — real id, real cursor —
/// and, until this fix, never streamed that line (the send's own refresh ate
/// the frame). Both apps threw the answer away: the Mac drew a single text-keyed
/// stand-in, so a second send erased the first; the phone appended it to a
/// list the next stream update replaced wholesale. Either way the line was gone
/// until a reopen fetched the page.
@MainActor
final class SentLinesSurviveTheLiveFeedTests: XCTestCase {

    private func owned(_ id: String, _ text: String, at offset: TimeInterval) -> Message {
        var line = LiveDeckClient.reply(id, text, at: offset)
        line.author = DeckOwner.name
        line.role = .owner
        return line
    }

    /// DETECTOR (Mac): send two lines, then the desk answers on the stream.
    func testTwoSentLinesAndTheReplyAreAllOnScreenInOrder() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        client.sendResults = [owned("m2", "ship it", at: 10), owned("m3", "and tell Acme", at: 11)]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript == ["any updates?"] }

        store.composerDraft = "ship it"
        await store.submitComposer()
        store.composerDraft = "and tell Acme"
        await store.submitComposer()
        client.push(.message(LiveDeckClient.reply("m4", "done, both", at: 20), readOnly: false))
        await waitUntil("the reply is drawn") { store.transcript.contains("done, both") }

        XCTAssertEqual(
            store.transcript, ["any updates?", "ship it", "and tell Acme", "done, both"],
            "a line he sent vanished from the open chat")
    }

    /// The same line said twice is two lines: "ok" today is not yesterday's "ok".
    func testRepeatingAnEarlierLineStillShowsTheNewOne() async {
        let client = LiveDeckClient.withChief(saying: "ok")
        client.sendResults = [owned("m2", "ok", at: 10)]
        let store = DeckStore(client: client)
        await store.loadRoster()
        await waitUntil("the thread opens") { store.transcript == ["ok"] }

        store.composerDraft = "ok"
        await store.submitComposer()

        XCTAssertEqual(store.transcript, ["ok", "ok"])
    }

    /// DETECTOR (the sync both apps share, and the phone's whole path): a line
    /// the deck confirmed is in every later update, not only until the next one.
    func testAConfirmedSendIsInEveryLaterUpdate() async throws {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let sync = ConversationSync(client: client, threadID: "direct:chief")
        let updates = await sync.updates()
        let run = Task { try await sync.run() }
        defer { run.cancel() }
        var iterator = updates.makeAsyncIterator()
        _ = await iterator.next()  // the page

        await sync.accept(owned("m2", "ship it", at: 10))
        client.push(.message(LiveDeckClient.reply("m3", "on it", at: 20), readOnly: false))

        var last: [String] = []
        while let update = await iterator.next() {
            last = update.messages.map(\.text)
            if last.contains("on it") { break }
        }
        XCTAssertEqual(last, ["any updates?", "ship it", "on it"])
    }

    /// DETECTOR (both apps): a reply the deck sends while the thread's history
    /// is still loading. The page was taken before it; the stream must not be
    /// opened only after it, or nothing ever carries it until a reopen.
    func testAReplySentWhileTheHistoryLoadsIsNotLost() async {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        client.duringFetch = {
            client.push(.message(LiveDeckClient.reply("m2", "mid-load", at: 5), readOnly: false))
        }
        let store = DeckStore(client: client)
        await store.loadRoster()

        await waitUntil("the reply sent during the load is drawn") {
            store.transcript == ["any updates?", "mid-load"]
        }
    }

    /// DETECTOR: a send accepted while the stream is down must not move the
    /// resume point. The reconnect fetches `since=` what the STREAM and the
    /// pages delivered; resuming from his own sent line skipped the desk's
    /// reply that landed just before it, and only a reopen brought it back.
    func testASendWhileTheStreamIsDownDoesNotSkipTheDesksReply() async throws {
        let client = LiveDeckClient.withChief(saying: "any updates?")
        let sync = ConversationSync(
            client: client, threadID: "direct:chief",
            backoff: { _ in try await Task.sleep(nanoseconds: 150_000_000) })
        let updates = await sync.updates()
        let run = Task { try await sync.run() }
        defer { run.cancel() }
        var iterator = updates.makeAsyncIterator()
        _ = await iterator.next()  // the page

        client.dropStream()
        let desk = LiveDeckClient.reply("m2", "while you were away", at: 15)
        let mine = owned("m3", "ship it", at: 20)
        client.page.messages += [desk, mine]  // the deck's transcript
        await sync.accept(mine)               // his send, answered during the outage
        let watchdog = Task { try await Task.sleep(nanoseconds: 2_000_000_000); run.cancel() }
        defer { watchdog.cancel() }

        var last: [String] = []
        while let update = await iterator.next() {
            last = update.messages.map(\.text)
            if last.contains("while you were away") { break }
        }
        XCTAssertEqual(last, ["any updates?", "while you were away", "ship it"])
    }

    private func waitUntil(
        _ what: String, within timeout: TimeInterval = 3,
        file: StaticString = #filePath, line: UInt = #line, _ condition: () -> Bool
    ) async {
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            if condition() { return }
            try? await Task.sleep(nanoseconds: 2_000_000)
        }
        XCTFail("timed out after \(timeout)s waiting for: \(what)", file: file, line: line)
    }
}

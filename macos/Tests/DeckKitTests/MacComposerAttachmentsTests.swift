import AppKit
import XCTest
import UniformTypeIdentifiers
@testable import DeckKit
@testable import DeckUI

/// Owner, 2026-09-30: **"also on Mac I can't paste images or files."**
///
/// MEASURED before this change (`testBeforeThisChangeAPastedPictureWasNothing`
/// keeps the measurement): the composer is a plain-text field, so ⌘V of a
/// screenshot pasted nothing at all, and ⌘V of a file copied in Finder pasted
/// its *name* as words. And the [+] put a path from THIS Mac into the text --
/// a path the desk, on the box, cannot open.
///
/// Now a paste, a drop, or the [+] goes through the same tray the phone uses
/// (`AttachmentTray`): uploaded to the deck, sent as `Attached image: <path>`.
@MainActor
final class MacComposerAttachmentsTests: XCTestCase {

    // MARK: what ⌘V finds

    private func board() -> NSPasteboard { NSPasteboard(name: .init("deck-test-\(UUID().uuidString)")) }

    private func png(_ w: Int = 40, _ h: Int = 30) -> Data {
        let rep = NSBitmapImageRep(bitmapDataPlanes: nil, pixelsWide: w, pixelsHigh: h, bitsPerSample: 8,
                                   samplesPerPixel: 4, hasAlpha: true, isPlanar: false,
                                   colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        return rep.representation(using: .png, properties: [:])!
    }

    func testBeforeThisChangeAPastedPictureWasNothing() {
        // The field editor the composer's plain TextField pastes with.
        let pb = board()
        pb.clearContents()
        pb.setData(png(), forType: .png)
        let editor = NSTextView()
        editor.isFieldEditor = true
        editor.isRichText = false
        editor.importsGraphics = false
        XCTAssertFalse(editor.readSelection(from: pb), "a plain-text field takes nothing from a picture")
        XCTAssertEqual(editor.string, "")
    }

    func testAScreenshotOnTheClipboardBecomesOneImage() throws {
        // ⌘⌃⇧4 puts a PNG (and a TIFF) on the clipboard.
        let pb = board()
        pb.clearContents()
        let data = png()
        let item = NSPasteboardItem()
        item.setData(data, forType: .png)
        item.setData(NSImage(data: data)!.tiffRepresentation!, forType: .tiff)
        pb.writeObjects([item])
        let found = try XCTUnwrap(MacPasteboardAttachments.read(pb))
        XCTAssertEqual(found.count, 1)
        XCTAssertEqual(found[0].kind, .image)
        XCTAssertEqual(found[0].mimeType, "image/png")
        XCTAssertEqual(found[0].filename, "pasted-1.png")
        XCTAssertEqual(found[0].data, data, "a small PNG goes as it is")
    }

    func testFilesCopiedInFinderBecomeThoseFilesNotTheirIcon() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        let a = dir.appendingPathComponent("report.pdf")
        let b = dir.appendingPathComponent("plan.png")
        try Data("%PDF-1.7".utf8).write(to: a)
        try png(3000, 2000).write(to: b)
        let pb = board()
        pb.clearContents()
        pb.writeObjects([a as NSURL, b as NSURL])
        // Finder also puts the names as words and the icon as a picture.
        pb.addTypes([.string, .tiff], owner: nil)
        pb.setString("report.pdf\nplan.png", forType: .string)
        pb.setData(NSImage(size: NSSize(width: 16, height: 16)).tiffRepresentation ?? Data(), forType: .tiff)
        let found = try XCTUnwrap(MacPasteboardAttachments.read(pb))
        XCTAssertEqual(found.map(\.filename), ["report.pdf", "plan.png"])
        XCTAssertEqual(found.map(\.kind), [.file, .image])
        XCTAssertEqual(found[1].data, try Data(contentsOf: b), "a file is sent byte for byte")
    }

    func testWordsOnTheClipboardAreLeftToTheTextField() {
        let pb = board()
        pb.clearContents()
        pb.setString("just words", forType: .string)
        XCTAssertNil(MacPasteboardAttachments.read(pb))
    }

    func testFinderOrderIsFileBeforePicture() {
        XCTAssertEqual(PasteClassifier.classify([UTType.fileURL.identifier, UTType.tiff.identifier,
                                                 UTType.utf8PlainText.identifier]), .file)
    }

    // MARK: the store sends what the tray holds

    private func loadedStore(_ uploader: AttachmentUploading, sendFails: Bool = false) async -> (DeckStore, ScriptedDeckClient) {
        let client = ScriptedDeckClient()
        client.rosterPayload = RosterPayload(agents: [makeAgent("chief")],
                                             threads: [makeThread("direct:chief", agent: "chief")],
                                             sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:chief", messages: [],
                                    isReadOnly: false, participants: ["owner", "chief"])]
        client.feeds = [.emitThenFinish([])]
        if sendFails { client.sendError = .transport("connection refused") }
        else { client.sendResult = makeMessage("mX", cursor: "9-mX") }
        let store = DeckStore(client: client, uploader: uploader)
        await store.loadRoster()
        await store.settle()
        return (store, client)
    }

    private func onScreen(_ store: DeckStore) -> [String] {
        guard case .loaded(let screen) = store.thread else { return [] }
        return screen.messages.map(\.text)
    }

    private let shot = PreparedAttachment(data: Data("a".utf8), filename: "a.png", mimeType: "image/png", kind: .image)

    private func sends(_ client: ScriptedDeckClient) -> Int {
        client.calls.filter { if case .send = $0 { return true }; return false }.count
    }

    private func waitUntil(_ condition: @MainActor () -> Bool) async throws {
        let end = Date().addingTimeInterval(2)
        while !condition() {
            if Date() > end { return XCTFail("timed out") }
            try await Task.sleep(nanoseconds: 10_000_000)
        }
    }

    func testHisWordsAndThePathOfEachAttachmentGoTogetherAndTheTrayEmpties() async throws {
        let gate = UploadGate()
        let (store, _) = await loadedStore(gate)
        store.attach([shot])
        let tray = try XCTUnwrap(store.composerTray)
        await gate.waitForStart(count: 1)
        gate.finish(name: "a.png")
        try await waitUntil { tray.isReady }
        store.composerDraft = "what is wrong here?"
        await store.submitComposer()
        XCTAssertTrue(onScreen(store).contains("what is wrong here?\n\nAttached image: /box/a.png"),
                      "on screen: \(onScreen(store))")
        XCTAssertEqual(store.composerDraft, "")
        XCTAssertTrue(tray.isEmpty)
    }

    func testAPictureAloneIsEnoughToSend() async throws {
        let gate = UploadGate()
        let (store, _) = await loadedStore(gate)
        store.attach([shot])
        await gate.waitForStart(count: 1)
        gate.finish(name: "a.png")
        try await waitUntil { store.composerTray?.isReady == true }
        XCTAssertTrue(store.composerCanSend)
        await store.submitComposer()
        XCTAssertTrue(onScreen(store).contains("Attached image: /box/a.png"))
    }

    func testNothingIsSentWhileAnAttachmentIsStillUploading() async throws {
        let gate = UploadGate()
        let (store, client) = await loadedStore(gate)
        store.attach([shot])
        await gate.waitForStart(count: 1)
        store.composerDraft = "look"
        XCTAssertFalse(store.composerCanSend)
        await store.submitComposer()
        XCTAssertEqual(sends(client), 0)
        XCTAssertEqual(store.composerDraft, "look")
    }

    func testAFailedSendKeepsHisWordsAndHisAttachments() async throws {
        let gate = UploadGate()
        let (store, _) = await loadedStore(gate, sendFails: true)
        store.attach([shot])
        await gate.waitForStart(count: 1)
        gate.finish(name: "a.png")
        try await waitUntil { store.composerTray?.isReady == true }
        store.composerDraft = "look"
        await store.submitComposer()
        XCTAssertEqual(store.composerDraft, "look")
        XCTAssertEqual(store.composerTray?.items.count, 1)
    }
}

import XCTest
@testable import DeckKit

/// **A keychain read never freezes the window.**
///
/// Measured 2026-09-30 on the owner's Mac (`sample` of the installed app): the
/// main thread sat in `SecItemCopyMatching` under `DeckStore.loadRoster`,
/// waiting on a keychain dialog behind a locked screen. Every rebuild of a
/// self-signed app gets a new code hash, the login keychain's partition list
/// does not know it, and macOS asks again — so the whole app froze until he
/// typed his password. `kSecUseAuthenticationUIFail` does NOT stop that read
/// from blocking (measured with two probe builds), so the only fix is to never
/// make the read where blocking hurts.
///
/// The gate: one background read per item, its result kept for the process;
/// the main thread waits a few milliseconds at most, then gets "Waiting for
/// keychain access…" and a `.deckCredentialsChanged` when the read lands.
final class KeychainReadGateTests: XCTestCase {

    final class SlowRead: @unchecked Sendable {
        let release = DispatchSemaphore(value: 0)
        let lock = NSLock()
        var calls = 0
        var onMain: [Bool] = []
        var value: String? = "tok"
        func read() throws -> String? {
            lock.withLock { calls += 1; onMain.append(Thread.isMainThread) }
            release.wait()
            return lock.withLock { value }
        }
    }

    func testTheMainThreadIsNeverBlockedByASlowRead() throws {
        let gate = KeychainReadGate(mainThreadBudget: 0.02, waitingAfter: 0.05)
        let slow = SlowRead()
        let start = Date()
        XCTAssertThrowsError(try gate.value(key: "k", onMainThread: true, read: slow.read)) { error in
            XCTAssertTrue(KeychainReadGate.isWaiting(error), "\(error)")
            XCTAssertEqual((error as? DeckError), .transport(KeychainReadGate.waitingText))
        }
        XCTAssertLessThan(Date().timeIntervalSince(start), 0.5, "the main thread waited on the keychain")
        slow.release.signal()
    }

    func testTheReadItselfRunsOffMainAndOnlyOnce() throws {
        let gate = KeychainReadGate(mainThreadBudget: 0.02, waitingAfter: 0.05)
        let slow = SlowRead()
        _ = try? gate.value(key: "k", onMainThread: true, read: slow.read)
        _ = try? gate.value(key: "k", onMainThread: true, read: slow.read)
        slow.release.signal()
        let value = try gate.value(key: "k", onMainThread: false, read: slow.read)
        XCTAssertEqual(value, "tok")
        XCTAssertEqual(slow.calls, 1, "a second ask must join the read in flight, not start another dialog")
        XCTAssertEqual(slow.onMain, [false])
        XCTAssertEqual(try gate.value(key: "k", onMainThread: true, read: slow.read), "tok", "kept for the process")
    }

    func testWaitingIsAnnouncedAndTheWindowIsToldToRetryWhenItLands() throws {
        let gate = KeychainReadGate(mainThreadBudget: 0.01, waitingAfter: 0.05)
        let slow = SlowRead()
        let waiting = expectation(forNotification: KeychainReadGate.waitingChanged, object: gate) { _ in gate.isWaiting }
        _ = try? gate.value(key: "k", onMainThread: true, read: slow.read)
        wait(for: [waiting], timeout: 2)
        XCTAssertTrue(gate.isWaiting)

        let retry = expectation(forNotification: .deckCredentialsChanged, object: nil)
        slow.release.signal()
        wait(for: [retry], timeout: 2)
        XCTAssertFalse(gate.isWaiting)
    }

    func testAFailedReadIsNotKeptSoTheNextAskTriesAgain() throws {
        let gate = KeychainReadGate(mainThreadBudget: 0.5, waitingAfter: 1)
        var fail = true
        let read: () throws -> String? = {
            if fail { throw DeckError.transport("keychain read failed (-25293)") }
            return "tok"
        }
        XCTAssertThrowsError(try gate.value(key: "k", onMainThread: false, read: read))
        fail = false
        XCTAssertEqual(try gate.value(key: "k", onMainThread: false, read: read), "tok")
    }

    func testAWriteReplacesWhatIsKept() throws {
        let gate = KeychainReadGate()
        XCTAssertEqual(try gate.value(key: "k", onMainThread: false, read: { "old" }), "old")
        gate.store(key: "k", "new")
        XCTAssertEqual(try gate.value(key: "k", onMainThread: true, read: { XCTFail("re-read"); return nil }), "new")
        gate.store(key: "k", nil)
        XCTAssertNil(try gate.value(key: "k", onMainThread: true, read: { XCTFail("re-read"); return "x" }))
    }

    func testTheKeychainStoreGoesThroughTheGate() throws {
        let src = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/DeckKit/TokenStore.swift")
        let text = try String(contentsOf: src, encoding: .utf8)
        XCTAssertTrue(text.contains("KeychainReadGate.shared.value("), "KeychainTokenStore reads the keychain directly")
    }

    func testTheWindowSaysWhatToDoWhileTheKeychainWaits() throws {
        let main = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
            .deletingLastPathComponent().appendingPathComponent("Sources/DeckApp/DeckAppMain.swift")
        let text = try String(contentsOf: main, encoding: .utf8)
        XCTAssertTrue(text.contains("KeychainReadGate.waitingChanged"), "nothing shows the keychain wait")
        XCTAssertTrue(text.contains("KeychainReadGate.waitingHint"))
    }
}

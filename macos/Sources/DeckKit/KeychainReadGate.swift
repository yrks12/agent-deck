import Foundation

/// **Keychain reads that cannot freeze the window.**
///
/// MEASURED 2026-09-30 on the owner's Mac: a rebuilt app sat with its main
/// thread in `SecItemCopyMatching` (under `DeckStore.loadRoster`) waiting on
/// "Agent Deck wants to use your confidential information" behind a locked
/// screen — the whole app frozen. Why it asks at all: the login keychain keeps
/// a *partition list* per item, and a self-signed build's partition is its
/// code hash (`cdhash:…`), new on every build — the deck token's list held 28
/// of them. `kSecUseAuthenticationUIFail` does not make that read fail fast; it
/// still blocks (measured with two probe builds). So the read must never be
/// made where blocking hurts.
///
/// One background read per item, its result kept for the process (a write
/// through `store` replaces it). A caller on the main thread waits at most
/// `mainThreadBudget`, then gets `DeckError.transport(waitingText)`; when the
/// read lands the gate posts `.deckCredentialsChanged`, which is what already
/// makes the window ask the deck again. While a read is stuck past
/// `waitingAfter`, `isWaiting` is true and `waitingChanged` is posted, so the
/// window can say what to do instead of looking dead.
public final class KeychainReadGate: @unchecked Sendable {
    public static let shared = KeychainReadGate()

    public static let waitingText = "Waiting for keychain access…"
    public static let waitingHint =
        "Unlock this Mac and click Always Allow on \"Agent Deck wants to use your confidential information\"."
    public static let waitingChanged = Notification.Name("\(DeckIdentity.bundleID).keychainWaitingChanged")

    public static func isWaiting(_ error: Error) -> Bool {
        (error as? DeckError) == .transport(waitingText)
    }

    private enum Slot {
        case loading(DispatchGroup)
        case loaded(String?)
    }

    private let lock = NSLock()
    private var slots: [String: Slot] = [:]
    private var lastError: [String: Error] = [:]
    private var missedOnMain: Set<String> = []
    private var stuck: Set<String> = []
    private let mainThreadBudget: TimeInterval
    private let waitingAfter: TimeInterval
    private let queue = DispatchQueue(label: "\(DeckIdentity.bundleID).keychain", qos: .userInitiated,
                                      attributes: .concurrent)

    public init(mainThreadBudget: TimeInterval = 0.05, waitingAfter: TimeInterval = 1) {
        self.mainThreadBudget = mainThreadBudget
        self.waitingAfter = waitingAfter
    }

    /// True while a read has been stuck past `waitingAfter` (a dialog is up).
    public var isWaiting: Bool { lock.withLock { !stuck.isEmpty } }

    public func value(key: String, onMainThread: Bool = Thread.isMainThread,
                      read: @escaping @Sendable () throws -> String?) throws -> String? {
        enum Step { case have(String?), wait(DispatchGroup) }
        let step: Step = lock.withLock {
            switch slots[key] {
            case .loaded(let value)?:
                return .have(value)
            case .loading(let g)?:
                if onMainThread { missedOnMain.insert(key) }
                return .wait(g)
            case nil:
                let g = DispatchGroup()
                g.enter()
                slots[key] = .loading(g)
                if onMainThread { missedOnMain.insert(key) }
                start(key, g, read)
                return .wait(g)
            }
        }
        guard case .wait(let group) = step else {
            if case .have(let value) = step { return value }
            return nil
        }
        if onMainThread {
            guard group.wait(timeout: .now() + mainThreadBudget) == .success else {
                throw DeckError.transport(Self.waitingText)
            }
        } else {
            group.wait()
        }
        return try lock.withLock {
            if case .loaded(let value)? = slots[key] { return value }
            throw lastError[key] ?? DeckError.transport("keychain read failed")
        }
    }

    /// A write (or delete, `nil`) this process made: what every reader gets now.
    public func store(key: String, _ value: String?) {
        lock.withLock { slots[key] = .loaded(value) }
    }

    private func start(_ key: String, _ group: DispatchGroup, _ read: @escaping @Sendable () throws -> String?) {
        queue.async { [self] in
            let result = Result { try read() }
            let (wasMissed, wasStuck) = lock.withLock { () -> (Bool, Bool) in
                switch result {
                case .success(let value): slots[key] = .loaded(value); lastError[key] = nil
                case .failure(let error): slots[key] = nil; lastError[key] = error
                }
                return (missedOnMain.remove(key) != nil, stuck.remove(key) != nil)
            }
            group.leave()
            DispatchQueue.main.async {
                if wasStuck { NotificationCenter.default.post(name: Self.waitingChanged, object: self) }
                if wasMissed { NotificationCenter.default.post(name: .deckCredentialsChanged, object: nil) }
            }
        }
        queue.asyncAfter(deadline: .now() + waitingAfter) { [self] in
            let nowStuck = lock.withLock { () -> Bool in
                guard case .loading(let g)? = slots[key], g === group else { return false }
                return stuck.insert(key).inserted
            }
            if nowStuck {
                DispatchQueue.main.async { NotificationCenter.default.post(name: Self.waitingChanged, object: self) }
            }
        }
    }
}

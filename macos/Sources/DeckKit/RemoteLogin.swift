import Foundation
#if canImport(UserNotifications)
import UserNotifications
#endif

/// **The phone asks the Mac to sign a desk in.**
///
/// The phone cannot read the Mac's Chrome or use its passkey, so it files a
/// request on `POST /v1/logins/requests`; the Mac app, already connected,
/// claims it on a long-poll and runs the sign-in it already knows. Cookies go
/// from the Mac to `/v1/logins` and nowhere else — a request row carries a
/// count, a node name and a sentence, never a value.
public enum LoginMethod: String, Hashable, Sendable, Codable {
    case chrome, passkey
}

/// One row of `/v1/logins/requests`, exactly the deck's `FIELDS`.
public struct LoginRequest: Hashable, Sendable, Decodable {
    public let id: String
    public let desk: String
    public let origin: String
    public let host: String
    public let method: LoginMethod
    public let handoff: String?
    /// queued | claimed | done | failed | expired
    public let status: String
    /// signed_in | opened, on done.
    public let outcome: String?
    /// Which Mac claimed it.
    public let node: String?
    public let desks: Int?
    public let detail: String?

    public init(id: String, desk: String, origin: String, host: String, method: LoginMethod,
                handoff: String?, status: String, outcome: String?, node: String?,
                desks: Int?, detail: String?) {
        self.id = id; self.desk = desk; self.origin = origin; self.host = host
        self.method = method; self.handoff = handoff; self.status = status
        self.outcome = outcome; self.node = node; self.desks = desks; self.detail = detail
    }

    public var isFinished: Bool { ["done", "failed", "expired"].contains(status) }
}

/// The four calls. Separate from `DeckClient`, like `LoginSharingClient`: a
/// transport that predates the route does not conform, and nothing offers it.
public protocol LoginRequestClient: Sendable {
    func fileLoginRequest(desk: String, origin: String, method: LoginMethod,
                          handoff: String?) async throws -> LoginRequest
    func loginRequest(id: String) async throws -> LoginRequest
    /// The Mac's long-poll: claims the oldest waiting request for `node`.
    func nextLoginRequest(node: String, wait: TimeInterval) async throws -> LoginRequest?
    func reportLoginRequest(id: String, status: String, outcome: String?, desks: Int,
                            detail: String?) async throws
}

/// What the phone's card says while the Mac works.
public enum RemoteSignInPhase: Hashable, Sendable {
    case waitingForMac
    case signedIn(node: String, desks: Int)
    case finishOnMac(node: String, host: String)
    case macOffline
    case failed(String)

    public init(_ row: LoginRequest) {
        let node = row.node ?? "your Mac"
        switch row.status {
        case "done":
            self = row.outcome == "opened"
                ? .finishOnMac(node: node, host: row.host)
                : .signedIn(node: node, desks: row.desks ?? 0)
        case "failed":
            self = .failed(row.detail ?? "Your Mac could not sign in to \(row.host).")
        case "expired":
            self = row.node == nil
                ? .macOffline
                : .failed("\(node) took the request but didn't finish in time.")
        default:
            self = .waitingForMac
        }
    }

    public var line: String {
        switch self {
        case .waitingForMac: return "Asking your Mac…"
        case .signedIn(let node, _): return "Signed in from your Mac ✓ (\(node))"
        case .finishOnMac(let node, let host): return "Finish signing in to \(host) on your Mac (\(node))"
        case .macOffline: return "Your Mac is offline"
        case .failed(let why): return why
        }
    }

    public var canRetry: Bool {
        switch self {
        case .macOffline, .failed: return true
        default: return false
        }
    }

    public var isWorking: Bool { self == .waitingForMac }
}

/// **The phone's half**: file, then follow the request until it ends.
public struct PhoneSignIn: Sendable {
    let client: LoginRequestClient
    let pause: @Sendable () async -> Void
    /// Hard stop past the deck's two-minute expiry, in polls.
    let maxPolls: Int

    public init(client: LoginRequestClient,
                pause: @escaping @Sendable () async -> Void = {
                    try? await Task.sleep(nanoseconds: 1_500_000_000)
                },
                maxPolls: Int = 100) {
        self.client = client
        self.pause = pause
        self.maxPolls = maxPolls
    }

    /// Returns the final phase; `update` sees every one on the way.
    public func run(item: AttentionItem, method: LoginMethod,
                    update: @MainActor (RemoteSignInPhase) -> Void) async -> RemoteSignInPhase {
        guard let url = item.signInOnMacURL, let desk = item.deskName else {
            let end = RemoteSignInPhase.failed("This card is not a sign-in your Mac can do.")
            await update(end)
            return end
        }
        var row: LoginRequest
        do {
            row = try await client.fileLoginRequest(desk: desk, origin: url.absoluteString,
                                                    method: method, handoff: item.askID)
        } catch {
            let end = RemoteSignInPhase.failed(
                (error as? DeckError)?.userFacingText ?? "Could not reach the deck.")
            await update(end)
            return end
        }
        await update(.waitingForMac)
        var polls = 0
        while !row.isFinished, polls < maxPolls, !Task.isCancelled {
            await pause()
            polls += 1
            if let next = try? await client.loginRequest(id: row.id) { row = next }
        }
        let end = row.isFinished ? RemoteSignInPhase(row) : .macOffline
        await update(end)
        return end
    }
}

/// **The Mac's half**: run what the phone asked for, report a count.
public actor MacLoginRequestHandler {
    private let requests: LoginRequestClient
    private let importer: MacLoginImport?
    private let resolveHandoff: @Sendable (String) async -> Void
    private let openPasskey: @Sendable (LoginRequest) async throws -> Void
    private let notify: @Sendable (String) async -> Void

    public init(requests: LoginRequestClient, importer: MacLoginImport?,
                resolveHandoff: @escaping @Sendable (String) async -> Void,
                openPasskey: @escaping @Sendable (LoginRequest) async throws -> Void,
                notify: @escaping @Sendable (String) async -> Void) {
        self.requests = requests
        self.importer = importer
        self.resolveHandoff = resolveHandoff
        self.openPasskey = openPasskey
        self.notify = notify
    }

    public func handle(_ request: LoginRequest) async {
        switch request.method {
        case .chrome:
            guard let importer else {
                await report(request, "failed", detail: "This Mac has no browser login it can read.")
                return
            }
            do {
                // Exactly the requested site: `run` filters to it.
                let share = try await importer.run(site: request.host)
                if let id = request.handoff { await resolveHandoff(id) }
                await report(request, "done", outcome: "signed_in", desks: share.desks)
            } catch {
                await report(request, "failed", detail: MacLoginImport.sentence(for: error))
            }
        case .passkey:
            do {
                try await openPasskey(request)
                await notify("Finish signing in to \(request.host) on your Mac")
                await report(request, "done", outcome: "opened")
            } catch let refused as SignInRefused {
                await report(request, "failed", detail: refused.sentence)
            } catch {
                await report(request, "failed", detail: MacSignIn.sentence(for: error))
            }
        }
    }

    /// Claims and handles requests until cancelled. A deck that is down or
    /// does not have the route is retried slowly, never hammered.
    public func listen(node: String, wait: TimeInterval = 25) async {
        while !Task.isCancelled {
            do {
                if let request = try await requests.nextLoginRequest(node: node, wait: wait) {
                    await handle(request)
                }
            } catch {
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
    }

    private func report(_ request: LoginRequest, _ status: String, outcome: String? = nil,
                        desks: Int = 0, detail: String? = nil) async {
        try? await requests.reportLoginRequest(id: request.id, status: status, outcome: outcome,
                                               desks: desks, detail: detail)
    }
}

/// A refusal the Mac explains in its own sentence.
public struct SignInRefused: Error, Equatable, Sendable {
    public let sentence: String
    public init(_ sentence: String) { self.sentence = sentence }
}

#if canImport(UserNotifications)
/// "Finish signing in to X on your Mac" — the one thing the phone's passkey
/// request shows on the Mac. Inside the app bundle only (`swift test` has no
/// notification centre, and posts nothing).
public enum MacSignInNotice {
    public static func post(_ text: String) async {
        guard await MainActor.run(body: { OwnerNotifier.isAvailable }) else { return }
        let content = UNMutableNotificationContent()
        content.title = "Agent Deck"
        content.body = text
        content.sound = .default
        try? await UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: "signin.\(UUID().uuidString)", content: content, trigger: nil))
    }
}
#endif

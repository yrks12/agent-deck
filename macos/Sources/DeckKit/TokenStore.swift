import Foundation
import Security

/// Where the deck's bearer token lives. Behind a protocol because the Keychain
/// is unavailable to an unsigned test bundle, and because a token must never be
/// baked into a build.
public protocol TokenStore: Sendable {
    func token() throws -> String?
    func setToken(_ token: String) throws
    func removeToken() throws
}

/// The real one: a generic password item in the login Keychain. Nothing is
/// written to disk in plaintext, and nothing is ever logged.
public struct KeychainTokenStore: TokenStore {
    private let service: String
    private let account: String

    public init(service: String = DeckIdentity.bundleID, account: String = "deck-api-token") {
        self.service = service
        self.account = account
    }

    private var baseQuery: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    public func token() throws -> String? {
        var query = baseQuery
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        switch status {
        case errSecSuccess:
            guard let data = item as? Data, let value = String(data: data, encoding: .utf8) else {
                return nil
            }
            return value.isEmpty ? nil : value
        case errSecItemNotFound:
            return nil
        default:
            throw DeckError.transport("keychain read failed (\(status))")
        }
    }

    public func setToken(_ token: String) throws {
        let data = Data(token.utf8)
        let update: [String: Any] = [kSecValueData as String: data]
        let status = SecItemUpdate(baseQuery as CFDictionary, update as CFDictionary)

        if status == errSecItemNotFound {
            var insert = baseQuery
            insert[kSecValueData as String] = data
            insert[kSecAttrAccessible as String] = kSecAttrAccessibleAfterFirstUnlock
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw DeckError.transport("keychain write failed (\(addStatus))")
            }
        } else if status != errSecSuccess {
            throw DeckError.transport("keychain update failed (\(status))")
        }
    }

    public func removeToken() throws {
        let status = SecItemDelete(baseQuery as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw DeckError.transport("keychain delete failed (\(status))")
        }
    }
}

/// For tests and for previews. Never reaches a build the user runs.
public final class InMemoryTokenStore: TokenStore, @unchecked Sendable {
    private let lock = NSLock()
    private var value: String?

    public init(token: String? = nil) {
        self.value = token
    }

    public func token() throws -> String? {
        lock.lock()
        defer { lock.unlock() }
        return value
    }

    public func setToken(_ token: String) throws {
        lock.lock()
        defer { lock.unlock() }
        value = token
    }

    public func removeToken() throws {
        lock.lock()
        defer { lock.unlock() }
        value = nil
    }
}

public extension Notification.Name {
    /// Posted when the token this app authenticates with has been saved or
    /// removed.
    ///
    /// The roster loads once, from a `.task` on the root view, and nothing
    /// else ever re-asks. Without this signal the window behind the Settings
    /// panel keeps whatever failure it drew while the Keychain was still
    /// empty -- and "Not connected yet" sitting beside a token you have just
    /// pasted reads as "that token was wrong". It is the first thing anyone
    /// does with this app, and it looked broken.
    static let deckCredentialsChanged = Notification.Name("\(DeckIdentity.bundleID).credentialsChanged")
}

import Foundation

public extension DeckConnection {
    /// The saved TLS pin, iff `url` is the saved deck. A launch that names a
    /// different deck (`DECK_URL`) is a different server and never inherits it.
    func savedPin(for url: URL) -> String? {
        guard let saved, saved.url.absoluteString == url.absoluteString else { return nil }
        return saved.pin
    }

    /// The live client for `url`: pinned when this deck was paired with a pin,
    /// otherwise the ordinary transports.
    func client(for url: URL, tokens: TokenStore = KeychainTokenStore()) -> HTTPDeckClient {
        guard let pin = savedPin(for: url) else { return HTTPDeckClient(baseURL: url, tokens: tokens) }
        let session = PinnedTrust.session(pin: pin).session
        return HTTPDeckClient(baseURL: url, tokens: tokens,
                              performer: URLSessionPerformer(session: session),
                              sse: URLSessionSSETransport(session: session))
    }

    func shell(for url: URL, tokens: TokenStore = KeychainTokenStore()) -> HTTPDeskShell {
        guard let pin = savedPin(for: url) else { return HTTPDeskShell(baseURL: url, tokens: tokens) }
        return HTTPDeskShell(baseURL: url, tokens: tokens,
                             performer: URLSessionPerformer(session: PinnedTrust.session(pin: pin).session))
    }
}

import Foundation
import CoreGraphics
import ImageIO

/// **The desk's screen as a stream** — `WS /v1/agents/{name}/screen/stream`,
/// `docs/client-api.md` §15 and `server/screen_stream.py`.
///
/// MEASURED 2026-10-01 from this Mac over WireGuard against a probe desk: one
/// polled `screen.jpg` took 0.52-0.69 s, so polling peaked at 1.84 fps and a
/// typed key took 0.81-0.99 s to appear. Over the stream the same desk ran at
/// ~10 fps while it changed, sent nothing while it was still, and a typed key
/// showed in ~0.2 s.
///
/// The wire, in full:
/// * text `hello` first; text `tick` every 2 s with nothing new; text `input`
///   replies; text `error` with `fallback: "poll"` before the deck closes.
/// * binary frames: 4 bytes sequence, 4 bytes age in ms (big-endian), JPEG.
/// * this side sends `{"type":"ack","seq":N}` once frame N is **decoded**, and
///   `{"type":"input","id":N, …}` with the same body `screen/input` takes.
///
/// Polling stays: any failure to open, any `error`, any drop, and the model
/// goes back to `screen.jpg` and tries the stream again later.

/// One thing the deck said on the socket.
public enum ScreenStreamEvent: Equatable, Sendable {
    case hello(width: Int, height: Int, window: Int)
    case frame(seq: Int, age: TimeInterval, jpeg: Data)
    /// Nothing changed: the last frame is still the screen.
    case tick
    /// The answer to input `id`; nil refusal is success.
    case inputResult(id: Int, refusal: ScreenRefusal?)
    /// The deck is closing this socket and wants polling instead.
    case fallback(reason: String)

    /// A binary message: the 8-byte header and a JPEG, or nil. PURE.
    public static func parse(binary data: Data) -> ScreenStreamEvent? {
        let bytes = [UInt8](data)
        guard bytes.count > 10, bytes[8] == 0xFF, bytes[9] == 0xD8 else { return nil }
        func u32(_ at: Int) -> Int {
            Int(bytes[at]) << 24 | Int(bytes[at + 1]) << 16
                | Int(bytes[at + 2]) << 8 | Int(bytes[at + 3])
        }
        return .frame(seq: u32(0), age: TimeInterval(u32(4)) / 1000,
                      jpeg: Data(bytes[8...]))
    }

    /// A text message, or nil for one this client does not know. PURE.
    public static func parse(text: String) -> ScreenStreamEvent? {
        let raw = Data(text.utf8)
        guard let body = (try? JSONSerialization.jsonObject(with: raw)) as? [String: Any],
              let type = body["type"] as? String else { return nil }
        switch type {
        case "hello":
            guard let width = body["width"] as? Int, let height = body["height"] as? Int
            else { return nil }
            return .hello(width: width, height: height, window: body["window"] as? Int ?? 1)
        case "tick":
            return .tick
        case "input":
            let id = body["id"] as? Int ?? -1
            if body["ok"] as? Bool == true { return .inputResult(id: id, refusal: nil) }
            return .inputResult(id: id, refusal: ScreenRefusal(status: 400, body: raw))
        case "error":
            return .fallback(reason: body["reason"] as? String ?? "error")
        default:
            return nil
        }
    }
}

/// What this side puts on the socket. PURE.
public enum ScreenStreamWire {
    public static func ack(_ seq: Int) -> String {
        "{\"type\":\"ack\",\"seq\":\(seq)}"
    }

    /// `screen/input`'s own body, with `type` and `id` added — not a second
    /// spelling of the gestures.
    public static func input(_ input: ScreenInput, id: Int) throws -> String {
        let encoded = try DeckCoding.encoder.encode(input)
        guard var body = try JSONSerialization.jsonObject(with: encoded) as? [String: Any]
        else { throw ScreenRefusal.transport("could not encode that gesture") }
        body["type"] = "input"
        body["id"] = id
        let data = try JSONSerialization.data(withJSONObject: body, options: [.sortedKeys])
        return String(decoding: data, as: UTF8.self)
    }
}

/// One open socket.
public protocol ScreenStreamSession: AnyObject, Sendable {
    /// The next thing the deck said. Throws when the socket is gone.
    func next() async throws -> ScreenStreamEvent
    func ack(_ seq: Int) async
    func send(_ input: ScreenInput, id: Int) async throws
    func close()
}

/// A client that can open the stream. `HTTPDeckClient` is one; a client that
/// is not simply polls.
public protocol AgentScreenStreaming: Sendable {
    func openScreenStream(agent: String) async throws -> ScreenStreamSession
}

// MARK: - decoding, off the main thread

/// A decoded frame. A class so the pixels are shared, never copied, between
/// the model and every view drawing them; equal only to itself.
public final class DecodedScreenImage: @unchecked Sendable, Equatable {
    public let cgImage: CGImage
    public init(_ cgImage: CGImage) { self.cgImage = cgImage }
    public static func == (lhs: DecodedScreenImage, rhs: DecodedScreenImage) -> Bool {
        lhs === rhs
    }
}

public enum ScreenFrameDecoder {
    /// The JPEG decoded into pixels **now**, on a background thread, so the
    /// view only draws them. Without `ShouldCacheImmediately` ImageIO decodes
    /// lazily — on the main thread, at draw time, which is the stutter this
    /// exists to remove. `probe` is told whether it ran on the main thread.
    public static func decode(_ jpeg: Data,
                              probe: (@Sendable (Bool) -> Void)? = nil) async -> DecodedScreenImage? {
        await Task.detached(priority: .userInitiated) {
            probe?(pthread_main_np() != 0)
            guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil),
                  let image = CGImageSourceCreateImageAtIndex(
                      source, 0, [kCGImageSourceShouldCacheImmediately: true] as CFDictionary)
            else { return nil }
            return DecodedScreenImage(image)
        }.value
    }
}

// MARK: - URLSession

/// The socket, on `URLSessionWebSocketTask`. The bearer token rides on the
/// upgrade request exactly as on every other route.
public final class URLSessionScreenStream: ScreenStreamSession, @unchecked Sendable {
    private let task: URLSessionWebSocketTask

    public init(request: URLRequest, session: URLSession = .shared) {
        task = session.webSocketTask(with: request)
        task.maximumMessageSize = 8 * 1024 * 1024
        task.resume()
    }

    public func next() async throws -> ScreenStreamEvent {
        while true {
            let message = try await task.receive()
            switch message {
            case .data(let data):
                if let event = ScreenStreamEvent.parse(binary: data) { return event }
            case .string(let text):
                if let event = ScreenStreamEvent.parse(text: text) { return event }
            @unknown default:
                continue
            }
        }
    }

    public func ack(_ seq: Int) async {
        try? await task.send(.string(ScreenStreamWire.ack(seq)))
    }

    public func send(_ input: ScreenInput, id: Int) async throws {
        try await task.send(.string(try ScreenStreamWire.input(input, id: id)))
    }

    public func close() {
        task.cancel(with: .normalClosure, reason: nil)
    }
}

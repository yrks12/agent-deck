import Foundation

/// One `text/event-stream` event.
public struct SSEEvent: Hashable, Sendable {
    public var name: String?
    public var data: String
    public var id: String?

    public init(name: String?, data: String, id: String?) {
        self.name = name
        self.data = data
        self.id = id
    }
}

/// Incremental parser for `text/event-stream`.
///
/// A socket hands over bytes, not events, and the split lands wherever TCP
/// decides — mid-field, mid-value, mid-word. Everything not yet terminated by a
/// blank line stays in `buffer` until the rest of it arrives.
public struct SSEParser: Sendable {
    /// Scalars, not Characters. Swift treats "\r\n" as a *single* Character, so
    /// searching a String for "\n" silently misses every CRLF line ending — and
    /// then no event ever terminates.
    private var buffer: [Unicode.Scalar] = []
    private var name: String?
    private var dataLines: [String] = []
    private var id: String?

    private static let newline = Unicode.Scalar(10 as UInt8)
    private static let carriageReturn = Unicode.Scalar(13 as UInt8)

    public init() {}

    public mutating func consume(_ chunk: String) -> [SSEEvent] {
        buffer.append(contentsOf: chunk.unicodeScalars)
        var events: [SSEEvent] = []

        // Only complete lines can be interpreted; the tail goes back in the
        // buffer for the next chunk.
        while let newline = buffer.firstIndex(of: Self.newline) {
            var scalars = Array(buffer[buffer.startIndex..<newline])
            buffer.removeFirst(newline + 1)
            if scalars.last == Self.carriageReturn { scalars.removeLast() }
            let line = String(String.UnicodeScalarView(scalars))

            if line.isEmpty {
                if let event = takeEvent() { events.append(event) }
                continue
            }
            // A line starting with ':' is a comment — the keep-alive heartbeat.
            guard !line.hasPrefix(":") else { continue }

            let (field, value) = Self.split(line)
            switch field {
            case "event": name = value
            case "data": dataLines.append(value)
            case "id": id = value
            default: break  // `retry:` and anything newer are not this client's business
            }
        }
        return events
    }

    private mutating func takeEvent() -> SSEEvent? {
        defer {
            name = nil
            dataLines = []
            id = nil
        }
        guard !dataLines.isEmpty else { return nil }
        return SSEEvent(name: name, data: dataLines.joined(separator: "\n"), id: id)
    }

    private static func split(_ line: String) -> (String, String) {
        guard let colon = line.firstIndex(of: ":") else { return (line, "") }
        let field = String(line[line.startIndex..<colon])
        var value = String(line[line.index(after: colon)...])
        // The spec strips exactly one leading space, not all whitespace.
        if value.hasPrefix(" ") { value.removeFirst() }
        return (field, value)
    }
}

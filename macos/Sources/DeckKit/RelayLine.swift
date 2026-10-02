import Foundation

/// Parsing for the two thread-id shapes §6 defines. Ids are *compared* and
/// *split* here, never constructed: the deck issues them and a client that
/// builds one has invented a thread.
public enum ThreadID {
    public static let directPrefix = "direct:"
    public static let peerPrefix = "peer:"

    /// The desk a `direct:` thread belongs to, or `nil` for anything else.
    public static func desk(ofDirect id: String) -> String? {
        guard id.hasPrefix(directPrefix) else { return nil }
        let name = String(id.dropFirst(directPrefix.count))
        return name.isEmpty ? nil : name
    }

    /// The two names in `peer:<lower>|<higher>`. `nil` when the id is not a
    /// peer id, or is one this client cannot read — a name may not contain
    /// `|`, so anything other than exactly two non-empty halves is malformed.
    public static func peerParticipants(of id: String) -> (String, String)? {
        guard id.hasPrefix(peerPrefix) else { return nil }
        let halves = id.dropFirst(peerPrefix.count)
            .split(separator: "|", omittingEmptySubsequences: false)
            .map(String.init)
        guard halves.count == 2, !halves[0].isEmpty, !halves[1].isEmpty else { return nil }
        return (halves[0], halves[1])
    }

    /// The participant of a peer id that is not `name`.
    public static func peer(in id: String, otherThan name: String) -> String? {
        guard let (first, second) = peerParticipants(of: id) else { return nil }
        if first == name { return second }
        if second == name { return first }
        return nil
    }
}

/// How one line of a `direct:` thread is drawn — §6.2's table, as a value.
///
/// A dispatch and a reply are traffic the owner is **overhearing**. They are
/// never the desk answering him, and the whole point of stating that here,
/// once, is that no view can decide it differently.
public enum RelayAttribution: Hashable, Sendable {
    /// `message.thread_id == page.thread_id` — the owner's line, or the desk's
    /// answer to him.
    case ordinary
    /// Differs, and this desk wrote it: `to <name>`.
    case dispatch(to: String, threadID: String)
    /// Differs, and somebody else wrote it: `from <name>`.
    case reply(from: String, threadID: String)

    /// Said when the peer id carries no name this client can read. It is still
    /// not the desk talking to the owner, and saying so vaguely beats promoting
    /// it to a plain bubble — that is the failure §6.2 exists to prevent.
    public static let unnamedPeer = "another desk"

    /// `nil` for an ordinary bubble; there is nothing to put in front of it.
    public var label: String? {
        switch self {
        case .ordinary: return nil
        case .dispatch(let name, _): return "to \(name)"
        case .reply(let name, _): return "from \(name)"
        }
    }

    /// The `peer:` thread this line came from — the full record, and so the
    /// natural destination for a tap.
    public var peerThreadID: String? {
        switch self {
        case .ordinary: return nil
        case .dispatch(_, let id), .reply(_, let id): return id
        }
    }

    public var isRelayed: Bool { self != .ordinary }
}

public enum RelayLine {

    /// §6.2's table, and nothing else. `displayName` resolves a wire name
    /// through the roster, so the label is a desk's name and never an id.
    public static func attribution(
        for message: Message,
        page pageThreadID: String,
        desk: String,
        displayName: (String) -> String
    ) -> RelayAttribution {
        // `thread_id` is optional on the wire and decodes to "" when absent.
        // An absent field is a missing fact, not a difference: comparing it
        // would relabel every line of the conversation as overheard traffic.
        guard !message.threadID.isEmpty, message.threadID != pageThreadID else {
            return .ordinary
        }

        let name = peerName(of: message, desk: desk).map(displayName)
            ?? RelayAttribution.unnamedPeer
        return message.author == desk
            ? .dispatch(to: name, threadID: message.threadID)
            : .reply(from: name, threadID: message.threadID)
    }

    /// "The participant of the `peer:` id that is not this desk" — §6.2.
    ///
    /// The fallback covers a pair this desk is not in, which the contract's
    /// fence says cannot reach this thread. Naming it after the desk would
    /// invent an org edge, so it is named after whoever did not write it.
    private static func peerName(of message: Message, desk: String) -> String? {
        if let peer = ThreadID.peer(in: message.threadID, otherThan: desk) { return peer }
        return ThreadID.peer(in: message.threadID, otherThan: message.author)
    }
}

/// One drawable line: the record, and how §6.2 says to attribute it.
///
/// The transcript holds these rather than bare messages so a message and its
/// attribution cannot come apart, and so there is exactly one array to count.
public struct TranscriptRow: Identifiable, Hashable, Sendable {
    public var message: Message
    public var attribution: RelayAttribution

    public var id: String { message.id }

    public init(message: Message, attribution: RelayAttribution) {
        self.message = message
        self.attribution = attribution
    }

    /// The one sentence a screen reader gets. Relay traffic says what it is
    /// before it says what was said, because "Chief: page_views new: 0" with
    /// no attribution is exactly the sentence §6.2 forbids.
    public func spokenLabel(timestamp: String) -> String {
        let said = message.spokenLabel(timestamp: timestamp)
        switch attribution {
        case .ordinary: return said
        case .dispatch(let name, _): return "Dispatch to \(name). \(said)"
        case .reply(let name, _): return "Reply from \(name). \(said)"
        }
    }
}

public enum Transcript {

    /// Attributes a page of messages against the thread they were asked for.
    ///
    /// Order is the server's and is left alone. Ids are unique here as well as
    /// in `ConversationStore`: the same record is returned by the desk's
    /// thread, the peer's thread and the stream with one id, and it may be
    /// drawn once.
    public static func rows(
        _ messages: [Message],
        page pageThreadID: String,
        desk: String,
        displayName: (String) -> String = { $0 }
    ) -> [TranscriptRow] {
        var seen = Set<String>()
        seen.reserveCapacity(messages.count)
        return messages.compactMap { message in
            guard seen.insert(message.id).inserted else { return nil }
            return TranscriptRow(
                message: message,
                attribution: RelayLine.attribution(
                    for: message, page: pageThreadID, desk: desk, displayName: displayName
                )
            )
        }
    }
}

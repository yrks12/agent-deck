import Foundation

/// **What the call screen writes under the agent's face.** "ount to sign
/// into.They…" — the old caption was the last 160 characters of every reply
/// glued together: it started mid-word and ran sentences into each other.
///
/// Now: only finished sentences of the reply being spoken, a space after
/// every one, and when they do not fit, the oldest sentences go first. It
/// changes once per sentence, not once per streamed token — a redraw per
/// sentence instead of dozens a second.
public enum CallCaption {
    public static let limit = 160

    public static func shown(_ transcript: String) -> String {
        let sentences = finishedSentences(transcript)
        var kept: [String] = []
        var length = 0
        for sentence in sentences.reversed() {
            let added = sentence.count + (kept.isEmpty ? 0 : 1)
            if length + added > limit { break }
            kept.insert(sentence, at: 0)
            length += added
        }
        if kept.isEmpty, let last = sentences.last {
            return wordTail(last)
        }
        return kept.joined(separator: " ")
    }

    /// Sentences that ended with . ! ? (not a decimal point), spaces tidied.
    static func finishedSentences(_ text: String) -> [String] {
        let chars = Array(text)
        var sentences: [String] = []
        var current = ""
        for (i, ch) in chars.enumerated() {
            current.append(ch)
            guard ".!?".contains(ch) else { continue }
            let next = i + 1 < chars.count ? chars[i + 1] : nil
            let previous = i > 0 ? chars[i - 1] : nil
            // 2.5, v1.2: a point between digits is not an end.
            if ch == ".", let next, next.isNumber, previous?.isNumber == true { continue }
            // "?!", "..." — the end is the last of the run.
            if let next, ".!?".contains(next) { continue }
            let sentence = current.split(whereSeparator: \.isWhitespace).joined(separator: " ")
            if !sentence.isEmpty { sentences.append(sentence) }
            current = ""
        }
        return sentences
    }

    private static func wordTail(_ sentence: String) -> String {
        var words: [Substring] = []
        var length = 1  // the ellipsis
        for word in sentence.split(separator: " ").reversed() {
            let added = word.count + (words.isEmpty ? 0 : 1)
            if length + added > limit { break }
            words.insert(word, at: 0)
            length += added
        }
        return "…" + words.joined(separator: " ")
    }
}

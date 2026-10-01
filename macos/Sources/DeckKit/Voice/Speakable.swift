import Foundation
import NaturalLanguage

/// **Text a desk wrote → the words worth saying out loud.**
///
/// A reply read verbatim on a call spells out `~/Projects/acme/server/app.py`
/// a character at a time, reads a code block line by line and recites a URL.
/// So what is spoken is the first two sentences of the prose, with every code
/// block, inline command, link address, path and file name taken out and the
/// markdown decoration dropped. The chat still carries the whole message;
/// this only decides what the speaker says.
public enum Speakable {

    /// `nil` when nothing speakable is left — a reply that was only code.
    public static func text(_ raw: String, sentences limit: Int = 2) -> String? {
        let lines = clean(raw)
        var picked: [String] = []
        for line in lines {
            for sentence in sentences(in: line) where picked.count < limit {
                picked.append(sentence)
            }
            if picked.count >= limit { break }
        }
        let spoken = picked.joined(separator: " ")
        return spoken.isEmpty ? nil : spoken
    }

    /// `"he"` when the text is mostly Hebrew letters, else `"en"`. Picks the
    /// voice: an English voice reading Hebrew says nothing intelligible.
    public static func language(of text: String) -> String {
        var hebrew = 0, latin = 0
        for scalar in text.unicodeScalars {
            if (0x0590...0x05FF).contains(scalar.value) { hebrew += 1 }
            else if scalar.properties.isAlphabetic && scalar.isASCII { latin += 1 }
        }
        return hebrew > latin ? "he" : "en"
    }

    // MARK: - Cleaning

    /// Prose lines, markdown and machine text removed, each ending in
    /// punctuation so a heading or a list item reads as its own sentence.
    static func clean(_ raw: String) -> [String] {
        var text = raw.replacingOccurrences(of: "\r\n", with: "\n")
        // Fenced code blocks, whole: ``` … ``` and ~~~ … ~~~ (an unclosed
        // fence runs to the end, as markdown renders it).
        text = replace(#"(?s)(```|~~~).*?(\1|\z)"#, in: text, with: "\n")
        // [words](address) keeps the words.
        text = replace(#"\[([^\]]+)\]\([^)]*\)"#, in: text, with: "$1")
        // Inline code: a plain word or two survives (`green`), a command does not.
        text = replaceInlineCode(in: text)
        // Addresses.
        text = replace(#"(?i)\b(?:https?|ftp|file|ssh)://\S+"#, in: text, with: "")
        text = replace(#"(?i)\bwww\.\S+"#, in: text, with: "")
        // Paths: absolute, home, relative with a slash, and bare file names.
        // A path's own trailing full stop is the sentence's, so it stays; and
        // "and/or" or "24/7" is not a path.
        text = replace(#"(?<![\w/])(?:~|\.{1,2})?/[^\s,;:)\]]*[^\s,;:)\].!?]"#, in: text, with: "")
        text = replace(#"\b[\w.-]+/[\w.-]+(?:/[\w.-]+)+|\b[\w.-]+/[\w-]+\.\w+"#, in: text, with: "")
        text = replace(#"\b[\w-]+\.(?:py|swift|js|jsx|ts|tsx|md|json|sh|ya?ml|toml|txt|plist|html|css|log|env|lock|cfg|ini|go|rs|rb|java|kt|c|h|m|cpp|sql|csv|png|jpe?g|pdf|zip)\b"#,
                       in: text, with: "")

        var lines: [String] = []
        for rawLine in text.components(separatedBy: "\n") {
            var line = rawLine
            line = replace(#"^\s{0,3}#{1,6}\s*"#, in: line, with: "")          // headings
            line = replace(#"^\s*>\s?"#, in: line, with: "")                   // quotes
            line = replace(#"^\s*(?:[-*+•]|\d+[.)])\s+"#, in: line, with: "")  // list markers
            line = replace(#"^\s*\|?(?:\s*:?-{3,}:?\s*\|?)+\s*$"#, in: line, with: "") // table rules
            line = line.replacingOccurrences(of: "|", with: " ")
            line = replace(#"(\*\*|__|\*|_{1,2}(?=\w)|(?<=\w)_{1,2}|~~)"#, in: line, with: "")
            line = replace(#"\(\s*\)|\[\s*\]"#, in: line, with: "")           // emptied brackets
            line = replace(#"\s+([,.;:!?])"#, in: line, with: "$1")            // "at ." → "at."
            line = replace(#"([,;:])(?=[,.;:!?])"#, in: line, with: "")        // ",." → "."
            line = replace(#"\s{2,}"#, in: line, with: " ")
            line = line.trimmingCharacters(in: .whitespaces)
            line = replace(#"^[,.;:—–-]+\s*"#, in: line, with: "")
            guard line.contains(where: { $0.isLetter || $0.isNumber }) else { continue }
            if let last = line.last, !".!?…".contains(last) {
                line = replace(#"[,;:—–-]+$"#, in: line, with: "")
                line += "."
            }
            lines.append(line)
        }
        return lines
    }

    /// Sentences of one line, as the language's own tokenizer splits them —
    /// so "e.g." and "3.5" do not end a sentence, in English or Hebrew.
    static func sentences(in line: String) -> [String] {
        let tokenizer = NLTokenizer(unit: .sentence)
        tokenizer.string = line
        var out: [String] = []
        tokenizer.enumerateTokens(in: line.startIndex..<line.endIndex) { range, _ in
            let sentence = line[range].trimmingCharacters(in: .whitespacesAndNewlines)
            if sentence.contains(where: { $0.isLetter || $0.isNumber }) { out.append(sentence) }
            return true
        }
        return out
    }

    private static func replaceInlineCode(in text: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: "`([^`\n]*)`") else { return text }
        var result = text
        for match in regex.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let whole = Range(match.range, in: result),
                  let inner = Range(match.range(at: 1), in: text) else { continue }
            let content = String(text[inner])
            let words = content.split(separator: " ")
            let plain = words.count <= 3 && content.allSatisfy { $0.isLetter || $0 == " " || $0 == "-" }
            result.replaceSubrange(whole, with: plain ? content : "")
        }
        return result
    }

    private static func replace(_ pattern: String, in text: String, with template: String) -> String {
        guard let regex = try? NSRegularExpression(pattern: pattern, options: [.anchorsMatchLines]) else {
            return text
        }
        return regex.stringByReplacingMatches(
            in: text, range: NSRange(text.startIndex..., in: text), withTemplate: template)
    }
}

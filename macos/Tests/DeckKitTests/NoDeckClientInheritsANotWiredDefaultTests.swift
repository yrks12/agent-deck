import XCTest
@testable import DeckKit

/// **A polite default on a protocol requirement is a route that can go unwired
/// without anything failing.**
///
/// `DeckClient` gives `handoffs()` and `resolveHandoff(id:outcome:)` defaults
/// so a client written against an older deck degrades instead of crashing.
/// That default is *also* exactly what a transport nobody finished looks like,
/// and the two are indistinguishable from above: an empty page means "nothing
/// is stuck" and "I never asked" with the same value.
///
/// It has already cost twice. `HTTPDeckClient` shipped without them and the
/// tray drew approvals and no handoffs on a deck that was serving them.
/// `HandoffsReachTheRealDeckTests` then pinned `HTTPDeckClient` — **one
/// conformer** — while `FixtureDeckClient` sat on the same inherited default
/// the whole time, so `DECK_FIXTURE=1` and every demo build drew a tray with no
/// handoffs in it; `ScriptedDeckClient` did too, so every test using it was
/// quietly measuring the default rather than a deck.
///
/// So this sweeps the **class**, and derives both halves from the source rather
/// than from a list somebody has to remember to extend:
///
/// 1. the silent defaults are `protocol DeckClient`'s requirements ∩ the
///    methods `extension DeckClient` implements — a default on something that
///    is *not* a requirement (the `messages(threadID:since:)` convenience) is
///    just a helper and cannot hide an unwired route;
/// 2. the conformers are every `struct` / `class` / `actor` in this package
///    declared `: … DeckClient …`, in `Sources` and in `Tests` both.
///
/// Add a protocol method tomorrow with a polite default and this fails for
/// every transport that has not been taught it, by name, on the day it lands.
final class NoDeckClientInheritsANotWiredDefaultTests: XCTestCase {

    // MARK: 1 — THE SWEEP

    func testEveryDeckClientWiresEveryRouteTheProtocolQuietlyDefaults() throws {
        let contract = try SwiftScan.read(
            Self.packageRoot.appendingPathComponent("Sources/DeckKit/DeckClient.swift"))
        let required = SwiftScan.functions(
            in: contract.body(afterFirst: #"\bprotocol\s+DeckClient\b"#))
        let defaulted = SwiftScan.functions(
            in: contract.body(afterFirst: #"\bextension\s+DeckClient\b"#))
        let silent = required.intersection(defaulted).sorted()

        XCTAssertEqual(
            silent, ["handoffs()", "resolveHandoff(id:outcome:)"],
            "the set of requirements DeckClient answers on a conformer's behalf "
            + "has changed. That is the whole hazard this file exists for — "
            + "check the new one is wired everywhere, then update this list. "
            + "Requirements seen: \(required.sorted()); defaulted: \(defaulted.sorted())")

        let files = try Self.swiftFiles()
        var conformers: [(name: String, file: String, region: String)] = []
        for file in files {
            let source = try SwiftScan.read(file)
            for name in source.typesDeclaredConforming(to: "DeckClient") {
                let region = source.body(ofTypeNamed: name)
                    + "\n" + Self.extensionBodies(of: name, across: files)
                conformers.append((name, file.lastPathComponent, region))
            }
        }

        XCTAssertTrue(
            conformers.contains { $0.name == "HTTPDeckClient" }
                && conformers.contains { $0.name == "FixtureDeckClient" },
            "the sweep found neither shipping transport, so it is sweeping "
            + "nothing: \(conformers.map(\.name).sorted())")
        XCTAssertGreaterThanOrEqual(
            conformers.count, 5,
            "only \(conformers.count) conformers found — the scan is missing the "
            + "test doubles, and a double sitting on the default is a whole "
            + "suite measuring the default instead of a deck: "
            + "\(conformers.map(\.name).sorted())")

        var unwired: [String] = []
        for conformer in conformers {
            let wired = SwiftScan.functions(in: conformer.region)
            for route in silent where !wired.contains(route) {
                unwired.append("\(conformer.name) (\(conformer.file)) → \(route)")
            }
        }

        XCTAssertEqual(
            unwired, [],
            "these DeckClients inherit DeckClient's not-wired default, so they "
            + "answer 'nothing is stuck' whether or not anything is — the same "
            + "value a wired one gives for a quiet deck, with no way to tell "
            + "them apart from above:\n  " + unwired.joined(separator: "\n  "))
    }

    // MARK: 2 — the fixture answers with something worth looking at

    /// `DECK_FIXTURE=1` is how the whole UI is built and reviewed with no deck
    /// running, and a demo build is what he is shown. A fixture that serves
    /// approvals and no handoffs teaches everyone who looks at it that half the
    /// tray does not exist.
    func testTheFixtureServesAHandoffAHumanWouldActuallyHaveToGoAndDo() async throws {
        let page = try await FixtureDeckClient().handoffs()

        let handoff = try XCTUnwrap(
            page.handoffs.first,
            "the fixture tray draws approvals and no handoffs, on a build whose "
            + "entire job is to show what the app looks like")

        XCTAssertFalse(handoff.needs.isEmpty, "a card that does not say what he has to do")
        XCTAssertFalse(
            handoff.state.isEmpty,
            "`state` is the load-bearing field — without it the card says 'I "
            + "need you' and nothing about whether money is already being spent")
        XCTAssertFalse(handoff.place.isEmpty, "it has to say where he goes")
        XCTAssertEqual(
            handoff.options.map(\.outcome), [.done, .skipped],
            "both verbs, and they are different instructions: `done` orders a "
            + "re-check, `skipped` orders abandonment")
        XCTAssertTrue(
            handoff.options.allSatisfy { !$0.summary.isEmpty },
            "an option that does not say what it tells the desk")
    }

    /// And his verb travels: the fixture answers the way the deck does rather
    /// than throwing the protocol's refusal, so the tray can be driven end to
    /// end with nothing running.
    func testTheFixtureTakesHisVerbAndDropsTheCard() async throws {
        let client = FixtureDeckClient()
        let waiting = try await client.handoffs().handoffs
        let handoff = try XCTUnwrap(waiting.first)

        let settled = try await client.resolveHandoff(id: handoff.id, outcome: .done)
        XCTAssertEqual(settled.outcome, "resolved")
        XCTAssertTrue(settled.resumed, "the desk has to be told to carry on")

        let after = try await client.handoffs().handoffs.map(\.id)
        XCTAssertFalse(
            after.contains(handoff.id),
            "the fixture kept re-raising a step he just did, so the demo tray "
            + "invites the same answer for ever")
    }

    // MARK: scanning this package's own source

    private static var packageRoot: URL {
        URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()   // DeckKitTests
            .deletingLastPathComponent()   // Tests
            .deletingLastPathComponent()   // package root
    }

    private static func swiftFiles() throws -> [URL] {
        ["Sources", "Tests"].flatMap { folder -> [URL] in
            let root = packageRoot.appendingPathComponent(folder)
            guard let walk = FileManager.default.enumerator(
                at: root, includingPropertiesForKeys: nil) else { return [] }
            return walk.compactMap { $0 as? URL }.filter { $0.pathExtension == "swift" }
        }
    }

    /// Everything added to the type elsewhere: a conformance is often split
    /// across an extension, and a route wired there is wired.
    private static func extensionBodies(of name: String, across files: [URL]) -> String {
        files.compactMap { try? SwiftScan.read($0).extensionBodies(of: name) }
            .joined(separator: "\n")
    }
}

// MARK: - a small, offset-stable Swift scanner

/// Enough of a reader to answer two questions about real source: what is inside
/// a given declaration, and what functions that declaration declares.
///
/// Comments and string literals are blanked **in place** before anything else,
/// so a doc comment quoting `func handoffs()` cannot be mistaken for one, and
/// the `{` inside this package's JSON fixture literals cannot unbalance a brace
/// count. Blanking preserves length, so every offset still lines up.
struct SwiftScan {
    /// The source with comments and literals blanked out.
    let text: String
    private let chars: [Character]

    init(_ source: String) {
        chars = SwiftScan.blankCommentsAndLiterals(Array(source))
        text = String(chars)
    }

    static func read(_ url: URL) throws -> SwiftScan {
        SwiftScan(try String(contentsOf: url, encoding: .utf8))
    }

    /// The braced body that follows the first match of `pattern`.
    func body(afterFirst pattern: String) -> String {
        guard let match = matches(pattern).first,
              let open = indexOfBrace(from: match.range.location + match.range.length)
        else { return "" }
        return braced(from: open)
    }

    /// Every `struct` / `class` / `actor` declared here whose inheritance
    /// clause names `protocolName`. Extensions are deliberately not counted as
    /// declarations — they are folded in as bodies instead.
    func typesDeclaredConforming(to protocolName: String) -> [String] {
        let pattern = #"\b(?:struct|class|actor)\s+([A-Za-z_][A-Za-z0-9_]*)\s*(?:<[^>{]*>)?\s*:([^{]*)\{"#
        return matches(pattern).compactMap { match -> String? in
            guard let name = group(1, of: match), let inherits = group(2, of: match),
                  inherits.range(of: #"\b\#(protocolName)\b"#, options: .regularExpression) != nil
            else { return nil }
            return name
        }
    }

    func body(ofTypeNamed name: String) -> String {
        body(afterFirst: #"\b(?:struct|class|actor)\s+\#(name)\b"#)
    }

    func extensionBodies(of name: String) -> String {
        matches(#"\bextension\s+\#(name)\b"#)
            .compactMap { match -> String? in
                guard let open = indexOfBrace(from: match.range.location + match.range.length)
                else { return nil }
                return braced(from: open)
            }
            .joined(separator: "\n")
    }

    /// `name(label:label:)` for every `func` declared in `source`, so an
    /// override is matched on its shape rather than on its name alone.
    static func functions(in source: String) -> Set<String> {
        let chars = Array(source)
        var found: Set<String> = []
        var index = 0
        while index < chars.count {
            guard let afterKeyword = word("func", in: chars, from: index) else { break }
            index = afterKeyword
            guard let name = identifier(in: chars, from: &index) else { continue }
            skipGenerics(in: chars, from: &index)
            skipSpace(in: chars, from: &index)
            guard index < chars.count, chars[index] == "(",
                  let close = matching(")", of: "(", in: chars, from: index)
            else { continue }
            found.insert("\(name)(\(labels(of: String(chars[(index + 1)..<close]))))")
            index = close + 1
        }
        return found
    }

    // MARK: the mechanics

    private func matches(_ pattern: String) -> [NSTextCheckingResult] {
        guard let regex = try? NSRegularExpression(
            pattern: pattern, options: [.dotMatchesLineSeparators]) else { return [] }
        return regex.matches(
            in: text, range: NSRange(location: 0, length: (text as NSString).length))
    }

    private func group(_ index: Int, of match: NSTextCheckingResult) -> String? {
        let range = match.range(at: index)
        guard range.location != NSNotFound else { return nil }
        return (text as NSString).substring(with: range)
    }

    /// UTF-16 offsets from `NSRegularExpression` and `Character` offsets are
    /// not the same thing, so everything after a match is walked on the
    /// `String` itself rather than on the array.
    private func indexOfBrace(from utf16Offset: Int) -> String.Index? {
        guard let start = String.Index(
            String.UTF16View.Index(utf16Offset: utf16Offset, in: text), within: text)
        else { return nil }
        var cursor = start
        while cursor < text.endIndex {
            if text[cursor] == "{" { return cursor }
            if text[cursor] == ";" { return nil }
            cursor = text.index(after: cursor)
        }
        return nil
    }

    private func braced(from open: String.Index) -> String {
        var depth = 0
        var cursor = open
        while cursor < text.endIndex {
            if text[cursor] == "{" { depth += 1 }
            if text[cursor] == "}" {
                depth -= 1
                if depth == 0 { return String(text[text.index(after: open)..<cursor]) }
            }
            cursor = text.index(after: cursor)
        }
        return ""
    }

    private static func matching(
        _ close: Character, of open: Character, in chars: [Character], from start: Int
    ) -> Int? {
        var depth = 0
        var index = start
        while index < chars.count {
            if chars[index] == open { depth += 1 }
            if chars[index] == close {
                depth -= 1
                if depth == 0 { return index }
            }
            index += 1
        }
        return nil
    }

    /// The offset just past a whole-word occurrence of `keyword`.
    private static func word(_ keyword: String, in chars: [Character], from start: Int) -> Int? {
        let needle = Array(keyword)
        var index = start
        while index + needle.count <= chars.count {
            if Array(chars[index..<(index + needle.count)]) == needle,
               index == 0 || !isIdentifier(chars[index - 1]),
               index + needle.count == chars.count || !isIdentifier(chars[index + needle.count]) {
                return index + needle.count
            }
            index += 1
        }
        return nil
    }

    private static func isIdentifier(_ character: Character) -> Bool {
        character.isLetter || character.isNumber || character == "_"
    }

    private static func skipSpace(in chars: [Character], from index: inout Int) {
        while index < chars.count, chars[index].isWhitespace { index += 1 }
    }

    private static func skipGenerics(in chars: [Character], from index: inout Int) {
        skipSpace(in: chars, from: &index)
        guard index < chars.count, chars[index] == "<" else { return }
        var depth = 0
        while index < chars.count {
            if chars[index] == "<" { depth += 1 }
            if chars[index] == ">" {
                depth -= 1
                if depth == 0 { index += 1; return }
            }
            index += 1
        }
    }

    private static func identifier(in chars: [Character], from index: inout Int) -> String? {
        skipSpace(in: chars, from: &index)
        var name = ""
        while index < chars.count, isIdentifier(chars[index]) {
            name.append(chars[index])
            index += 1
        }
        return name.isEmpty ? nil : name
    }

    /// `id: String, outcome: HandoffOutcome` → `id:outcome:`. Splits on
    /// top-level commas only, so a closure or a tuple in a type does not add
    /// one.
    private static func labels(of params: String) -> String {
        var depth = 0
        var pieces: [String] = []
        var current = ""
        for character in params {
            switch character {
            case "(", "[", "<": depth += 1
            case ")", "]", ">": depth -= 1
            default: break
            }
            if character == ",", depth == 0 {
                pieces.append(current)
                current = ""
            } else {
                current.append(character)
            }
        }
        pieces.append(current)
        return pieces.compactMap { piece -> String? in
            let words = piece.trimmingCharacters(in: .whitespacesAndNewlines)
                .split(whereSeparator: { $0.isWhitespace || $0 == ":" })
            guard let first = words.first else { return nil }
            return String(first) + ":"
        }.joined()
    }

    /// Blanks `//`, `/* */`, `"""…"""` and `"…"` with spaces, keeping length —
    /// and keeping newlines, so a reported offset still lands on its own line.
    private static func blankCommentsAndLiterals(_ source: [Character]) -> [Character] {
        var out = source
        var index = 0

        func blank(_ range: Range<Int>) {
            for position in range where out[position] != "\n" { out[position] = " " }
        }
        func pair(at position: Int) -> String {
            position + 1 < source.count ? String(source[position...position + 1]) : ""
        }
        func triple(at position: Int) -> String {
            position + 2 < source.count ? String(source[position...position + 2]) : ""
        }

        while index < source.count {
            if pair(at: index) == "//" {
                var end = index
                while end < source.count, source[end] != "\n" { end += 1 }
                blank(index..<end)
                index = end
            } else if pair(at: index) == "/*" {
                var end = index + 2
                var depth = 1
                while end + 1 < source.count, depth > 0 {
                    if String(source[end...end + 1]) == "/*" { depth += 1; end += 2 }
                    else if String(source[end...end + 1]) == "*/" { depth -= 1; end += 2 }
                    else { end += 1 }
                }
                end = min(end, source.count)
                blank(index..<end)
                index = end
            } else if triple(at: index) == "\"\"\"" {
                var end = index + 3
                while end + 2 < source.count, String(source[end...end + 2]) != "\"\"\"" { end += 1 }
                end = min(end + 3, source.count)
                blank(index..<end)
                index = end
            } else if source[index] == "\"" {
                var end = index + 1
                while end < source.count, source[end] != "\"", source[end] != "\n" {
                    if source[end] == "\\" { end += 1 }
                    end += 1
                }
                end = min(end + 1, source.count)
                blank(index..<end)
                index = end
            } else {
                index += 1
            }
        }
        return out
    }
}

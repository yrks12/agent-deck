import Foundation

/// The outcome of typing in the sidebar's search field.
///
/// `emptyTitle` is the reason this type exists. A filtered list that matched
/// nothing looks exactly like a roster that failed to load, and the difference
/// matters: one is "type something else", the other is "your deck is down".
public struct SidebarSearchResult: Equatable, Sendable {
    public var query: String
    public var snapshot: SidebarSnapshot
    /// False for a blank field: not searching is not the same as searching and
    /// matching everything.
    public var isSearching: Bool
    public var matchCount: Int
    public var emptyTitle: String?
    public var emptyDescription: String?

    public init(
        query: String,
        snapshot: SidebarSnapshot,
        isSearching: Bool,
        matchCount: Int,
        emptyTitle: String?,
        emptyDescription: String?
    ) {
        self.query = query
        self.snapshot = snapshot
        self.isSearching = isSearching
        self.matchCount = matchCount
        self.emptyTitle = emptyTitle
        self.emptyDescription = emptyDescription
    }
}

/// Filters the roster the sidebar already holds. No request: the whole roster
/// is in memory by contract, so a search is a pass over it.
public enum SidebarSearch {

    public static let emptyDescription =
        "Search looks at the name, the title and the last message. Try a shorter word."

    /// Built from the *payload*, not from an already-folded snapshot: rows
    /// hidden under "+ N more unreads" are still on this deck, and a search
    /// that could not see them would be quietly wrong.
    public static func result(payload: RosterPayload, query rawQuery: String) -> SidebarSearchResult {
        let query = rawQuery.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !query.isEmpty else {
            return SidebarSearchResult(
                query: "",
                snapshot: SidebarSnapshot.build(from: payload),
                isSearching: false,
                matchCount: payload.agents.count,
                emptyTitle: nil,
                emptyDescription: nil
            )
        }

        // Filter first, then build: the fold is a display rule and a filtered
        // list has nothing left to fold.
        let full = SidebarSnapshot.build(from: payload)
        let sections = full.sections.compactMap { section -> SidebarSection? in
            let rows = section.rows.filter { matches($0, query: query) }
            guard !rows.isEmpty else { return nil }
            return SidebarSection(name: section.name, rows: rows, overflowUnreadCount: 0)
        }
        // The chief sits above the roster and so is not in `sections` at all.
        // Filtering only those would make the one desk he talks to the one desk
        // he cannot search for.
        let chief = full.chief.flatMap { matches($0, query: query) ? $0 : nil }
        let matchCount = sections.reduce(0) { $0 + $1.rows.count } + (chief == nil ? 0 : 1)

        return SidebarSearchResult(
            query: query,
            snapshot: SidebarSnapshot(
                // The favourites row is not a search result; while filtering,
                // the list is the only answer on screen.
                pinned: [],
                sections: sections,
                totalUnread: sections.flatMap(\.rows).reduce(0) { $0 + $1.unreadCount }
                    + (chief?.unreadCount ?? 0),
                chief: chief
            ),
            isSearching: true,
            matchCount: matchCount,
            emptyTitle: matchCount == 0 ? "No agents match “\(query)”" : nil,
            emptyDescription: matchCount == 0 ? emptyDescription : nil
        )
    }

    /// Name, title and preview — the three things actually on the row.
    static func matches(_ row: SidebarRow, query: String) -> Bool {
        let haystack = [
            row.agent.name,
            row.agent.displayName,
            row.agent.title,
            row.preview.line,
        ]
        return haystack.contains {
            $0.range(of: query, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
    }
}

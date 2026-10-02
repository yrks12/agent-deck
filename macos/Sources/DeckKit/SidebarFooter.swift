import Foundation

/// The two rows at the bottom of the sidebar, matching the reference's layout.
///
/// The first row opens Connectors & Skills: the store where MCP connectors and
/// skills are found and put on one desk, some, or all of them
/// (`docs/connectors.md`). It replaced a "Plugins — None" line.
public struct SidebarFooter: Equatable, Sendable {
    public var storeTitle: String
    public var storeDetail: String
    public var storeIsAvailable: Bool
    public var accountTitle: String
    public var accountDetail: String
    /// Feeds the same shape-and-colour face the roster rows use.
    public var accountLook: AvatarLook

    /// **The footer, made once.**
    ///
    /// `make` is derived from one constant — the owner's name — so its answer
    /// cannot change while the app is running. It was being called inside
    /// `SidebarView.body`, which meant an FNV hash over the name, a shape, a
    /// tint and an initials string rebuilt every time anything at all on the
    /// deck published, and a freshly-made value handed to the footer subtree
    /// on every pass. Measured at 10 makes across 10 draws; see
    /// `SidebarBodyWorkTests`.
    public static let current = make()

    /// **The footer for whoever the deck says he is**, made once per name: his
    /// name from `X-Deck-Owner-Name` when the deck has sent one, "Owner"
    /// otherwise. Cached on the name, so a sidebar redraw costs a lock and a
    /// string compare, not a rebuilt footer (`SidebarBodyWorkTests`).
    public static func forCurrentOwner() -> SidebarFooter {
        let name = DeckOwner.displayName
        cache.lock.lock(); defer { cache.lock.unlock() }
        if let made = cache.footer, cache.name == name { return made }
        var footer = make()
        if let name {
            footer.accountTitle = name
            footer.accountLook = AvatarLook.forName(name)
        }
        cache.name = name
        cache.footer = footer
        return footer
    }

    private final class Cache: @unchecked Sendable {
        let lock = NSLock()
        var name: String?
        var footer: SidebarFooter?
    }
    private static let cache = Cache()

    /// Still a function, because the owner is a parameter for the tests that
    /// pin what a different name draws. The app uses `current`.
    /// Counted; free unless `DECK_DIAGNOSE=1`.
    public static func make(owner: String = DeckOwner.name) -> SidebarFooter {
        Diagnostics.count("sidebar.footer.make")
        return SidebarFooter(
            storeTitle: "Connectors & Skills",
            storeDetail: "Add tools and skills to your desks",
            storeIsAvailable: true,
            accountTitle: Agent(name: owner, title: "").displayName,
            accountDetail: "Signed in on this Mac",
            accountLook: AvatarLook.forName(owner)
        )
    }
}

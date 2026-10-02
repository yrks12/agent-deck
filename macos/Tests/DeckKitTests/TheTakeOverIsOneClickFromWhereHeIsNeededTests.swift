import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// The desk's own folder, absolute the way `POST /terminal` needs it. A file
/// constant rather than a static on the (main-actor) test class, so it can be a
/// default argument below.
private let atlasWorkspace = "/home/deckop/.claude/agent-bus/workspaces/atlas"

/// **"i need to be able to controll the google chrom and the temrinal we should
/// have like i use his screen when it needs me."**
///
/// The take-over shipped and he still cannot reach it. Read the code rather
/// than guessing why:
///
/// * `AgentScreenPanel` drew its Open button inside `if presentation.showsOpen
///   && isHovering` — **the only way in existed solely while the pointer was on
///   top of it.** Nothing on screen said it was there.
/// * the panel is mounted in `SettingsPanelView`'s `Form`, *below* the avatar,
///   the blocked sentence, four Profile rows and the Notifications switch, in a
///   column `Theme.inspectorWidth` caps at 340 points. On his 949-point window
///   the thumbnail starts below the fold, so there was nothing to hover over
///   until he scrolled to it.
/// * a desk that stops and asks for him says so in `DeskStatusStrip` and in the
///   inspector's blocked section, and **neither of them offered anything to do
///   about it.**
///
/// So this file pins four properties, none of which needs a pixel:
///
/// 1. from a desk that is asking for him, the way in exists **by name**;
/// 2. **nothing opens by itself** — a desk entering `NEEDS_YOU` presents no
///    stage, because taking his screen unbidden is worse than a click;
/// 3. and the good signal beside it: **invoking the named action does present
///    it**, so 2 cannot pass by nothing ever opening;
/// 4. the stage is sized against the window it has to fit — big enough to use
///    in his 1208x949 window, and still inside the 900x560 floor `DeckAppMain`
///    lets him drag to.
///
/// **No screen is taken.** Activation policy `.prohibited`, borderless windows
/// parked 40,000pt off every display, never ordered front, every wait bounded
/// at 0.4s.
@MainActor
final class TheTakeOverIsOneClickFromWhereHeIsNeededTests: XCTestCase {

    /// The window he screenshotted, and the smallest one `DeckAppMain`'s
    /// `.frame(minWidth: 900, minHeight: 560)` lets him drag to. A stage that
    /// only fits the first is unusable on the second.
    static let hisWindow = CGSize(width: 1208, height: 949)
    static let smallestWindow = CGSize(width: 900, height: 560)

    /// `dialog_unrelayed`: seated, running, and frozen on a dialog nothing
    /// relayed — §3.1's case, and the one where looking at the desk's own
    /// screen is the only thing that can move it.
    static let frozen = Blocked(
        what: "waiting on a permission dialog", reason: "dialog_unrelayed")

    static let deskWorkspace = atlasWorkspace

    // MARK: - the deck under test

    /// One desk, stopped and asking for him, on a deck that can serve both a
    /// screen and a shell. Everything below is measured against this.
    private func stoppedDeck(
        canServeScreen: Bool = true,
        canServeShell: Bool = true,
        workspace: String = atlasWorkspace
    ) async -> DeckStore {
        let client = ScriptedDeckClient()
        let atlas = Agent(name: "atlas", title: "Researcher", section: "Work",
                          state: .needsYou, workspace: workspace, blocked: Self.frozen)
        client.rosterPayload = RosterPayload(
            agents: [atlas],
            threads: [makeThread("direct:atlas", agent: "atlas")],
            sectionOrder: ["Work"])
        client.pages = [MessagePage(threadID: "direct:atlas", messages: [],
                                    isReadOnly: false, participants: ["owner", "atlas"])]
        client.feeds = [.emitThenFinish([])]
        let store = DeckStore(
            client: client,
            shell: canServeShell ? FakeShellClient() : nil,
            screens: canServeScreen ? FakeScreenClient(status: Self.screenStatus) : nil,
            approvalPollInterval: 600)
        await store.loadRoster()
        await store.settle()
        store.select(agent: "atlas", threadID: "direct:atlas")
        await store.settle()
        return store
    }

    static let screenStatus = AgentScreenStatus(
        desk: "atlas", isRunning: true, image: "agent-deck/desk-computer:1",
        container: "deck-desk-atlas", display: ":99", width: 1280, height: 800,
        staleAfter: 30, generatedAt: Date())

    // MARK: - 0. the good signal: the desk really is asking for him

    func testTheDeskUnderTestIsTheOneAskingForHim() async {
        let store = await stoppedDeck()

        guard let agent = store.agents["atlas"] else {
            return XCTFail("the roster never loaded, so every assertion in this file "
                           + "is measuring an empty deck")
        }
        XCTAssertEqual(agent.state, .needsYou)
        XCTAssertNotNil(agent.blocked,
                        "the desk under test is not blocked, so this file is not "
                        + "asking the question it says it is asking")
        // Asked with the stream up, because `DeskStatus` reports the connection
        // ahead of anything the desk said and the scripted feed in this file
        // closes as soon as it has emitted. What is under test is the desk, not
        // the socket.
        XCTAssertEqual(
            DeskStatus.make(agent: agent, connection: .live, approvals: [],
                            isSending: false)?.tone,
            .waitingOnYou,
            "the open conversation does not report this desk as waiting on him, so "
            + "the strip under test would never draw the way in at all")
    }

    // MARK: - 1. the route exists, by name, from the needs-you state

    func testAStoppedDeskOffersTheScreenAndTheTerminalByName() async {
        let store = await stoppedDeck()
        let titles = store.takeoverOffers.map(\.title)

        XCTAssertTrue(
            titles.contains(Takeover.screenTitle),
            "a desk stopped and asking for him offers \(titles) — no '"
            + "\(Takeover.screenTitle)'. The screen is the one thing that can say "
            + "what the dialog on that machine is asking, and there is no named way "
            + "to reach it from the state that needs it.")
        XCTAssertTrue(
            titles.contains(Takeover.terminalTitle),
            "a desk stopped and asking for him offers \(titles) — no '"
            + "\(Takeover.terminalTitle)'. Watching the machine and being able to do "
            + "something to it are two halves of the same ask.")
    }

    /// **And the offer is never a button that leads nowhere.** A deck with no
    /// agent's-computer routes has nothing to show; a desk whose working
    /// directory the deck never stated has nowhere for a command to run, and
    /// `POST /terminal` takes an absolute path this app must not invent.
    func testNothingIsOfferedThatTheDeckCannotActuallyServe() async {
        let noScreen = await stoppedDeck(canServeScreen: false)
        XCTAssertFalse(
            noScreen.takeoverOffers.contains { $0.focus == .screen },
            "a deck that cannot serve a screen still offers a take-over, so the "
            + "button opens a black rectangle")

        let noShell = await stoppedDeck(canServeShell: false)
        XCTAssertFalse(
            noShell.takeoverOffers.contains { $0.focus == .terminal },
            "a deck that cannot run a command still offers its terminal")

        let noCwd = await stoppedDeck(workspace: "")
        XCTAssertFalse(
            noCwd.takeoverOffers.contains { $0.focus == .terminal },
            "a desk whose working directory the deck never stated still offers a "
            + "terminal — the first command would have to run in a home folder this "
            + "app invented")
        XCTAssertTrue(
            noCwd.takeoverOffers.contains { $0.focus == .screen },
            "losing the terminal took the screen with it")
    }

    // MARK: - 2. nothing opens by itself

    func testADeskThatStopsOpensNothingOnItsOwn() async {
        let store = await stoppedDeck()

        XCTAssertNil(
            store.takeover,
            "a desk entering NEEDS_YOU presented the take-over by itself. Taking his "
            + "screen unbidden is worse than making him click: he is mid-sentence in "
            + "something else and a modal over the whole window takes the keyboard "
            + "with it.")

        // And it stays shut while the deck keeps talking about that desk.
        await store.loadApprovals()
        await store.settle()
        XCTAssertNil(store.takeover,
                     "the take-over opened itself on a later poll instead of on the "
                     + "first one, which is the same defect one tick later")
    }

    // MARK: - 3. the good signal: invoking it DOES present the stage

    func testInvokingTheNamedActionPresentsTheStageForThatDesk() async {
        let store = await stoppedDeck()

        store.takeOver(focus: .screen)
        guard let opened = store.takeover else {
            return XCTFail("'\(Takeover.screenTitle)' was invoked and nothing was "
                           + "presented, so the rule above passes because nothing "
                           + "ever opens — which is the shipped defect, not the fix")
        }
        XCTAssertEqual(opened.desk, "atlas")
        XCTAssertEqual(opened.displayName, "Atlas")
        XCTAssertEqual(opened.workspace, Self.deskWorkspace,
                       "the stage opened without the desk's working directory, so its "
                       + "terminal has nowhere to run")

        store.endTakeover()
        XCTAssertNil(store.takeover, "the stage cannot be closed")

        store.takeOver(focus: .terminal)
        XCTAssertEqual(store.takeover?.focus, .terminal,
                       "'\(Takeover.terminalTitle)' opened the screen instead")
        XCTAssertEqual(store.takeover?.showsTerminal, true,
                       "the terminal route opened a stage with no terminal in it")
    }

    /// A route the deck cannot serve does not open an empty stage either.
    func testAnUnservableRouteOpensNothing() async {
        let store = await stoppedDeck(canServeScreen: false)
        store.takeOver(focus: .screen)
        XCTAssertNil(store.takeover,
                     "a deck with no screen routes opened a take-over anyway")
    }

    // MARK: - 4. it is offered where the problem is announced

    /// **The strip on the conversation is where a stopped desk announces
    /// itself**, and it is the one surface he is already looking at. Measured
    /// as height, off-display: a strip carrying the offers is taller than the
    /// same strip without them, and a desk that is *not* asking for him is not
    /// given them at all.
    func testTheStripDrawsTheWayInWhenHeIsNeededAndNotOtherwise() async {
        let store = await stoppedDeck()
        let offers = store.takeoverOffers
        XCTAssertFalse(offers.isEmpty, "there is nothing to draw, so this measures nothing")

        guard let waiting = DeskStatus.make(agent: store.agents["atlas"], connection: .live,
                                            approvals: [], isSending: false) else {
            return XCTFail("the stopped desk has no status at all")
        }
        var quietDesk = makeAgent("atlas")
        quietDesk.state = .working
        guard let quiet = DeskStatus.make(agent: quietDesk, connection: .live,
                                          approvals: [], isSending: false) else {
            return XCTFail("a working desk has no status at all")
        }
        XCTAssertEqual(waiting.tone, .waitingOnYou)
        XCTAssertEqual(quiet.tone, .working)

        let bare = height(DeskStatusStrip(status: waiting, offers: [], takeOver: { _ in }))
        let offered = height(DeskStatusStrip(status: waiting, offers: offers, takeOver: { _ in }))
        XCTAssertGreaterThan(
            offered, bare + 16,
            "the strip that announces a stopped desk is \(Int(offered))pt tall with "
            + "the offers and \(Int(bare))pt without them, so it is not drawing them: "
            + "the one surface he is already looking at when a desk stops still gives "
            + "him nothing to press.")

        let quietBare = height(DeskStatusStrip(status: quiet, offers: [], takeOver: { _ in }))
        let quietOffered = height(DeskStatusStrip(status: quiet, offers: offers, takeOver: { _ in }))
        XCTAssertEqual(
            quietOffered, quietBare, accuracy: 0.5,
            "a desk that is working draws the take-over buttons too. The strip is on "
            + "the conversation permanently and its height is what sizes the "
            + "transcript's ScrollView — a control that is always there is one he "
            + "stops seeing on the day it matters.")
    }

    /// **And the thumbnail's own way in is on screen without a pointer on it.**
    /// The shipped panel drew Open inside `isHovering`, so a person who had
    /// never happened to rest the mouse on a 300-point picture had no reason to
    /// believe there was anything to open.
    func testTheWayIntoTheStageIsOnScreenWithoutHoveringOverIt() {
        let shot = AgentScreenFrame(jpeg: Self.jpeg, serverAge: 0.2, display: ":99",
                                    receivedAt: Date())
        func panel(_ state: AgentScreenState, hovering: Bool) -> AgentScreenPanelBody {
            AgentScreenPanelBody(
                presentation: AgentScreenPresentation(desk: "Atlas", state: state),
                isHovering: hovering, workspace: Self.deskWorkspace, onOpen: {})
        }

        // Calibration: these two states differ in exactly one thing — whether
        // there is a machine to open at all.
        XCTAssertTrue(panel(.live(shot), hovering: false).presentation.showsOpen)
        XCTAssertFalse(panel(.noComputer, hovering: false).presentation.showsOpen)

        // `.frame(width:)` and not a constraint: the picture is an aspect-ratio
        // box inside a flexible frame, so it only has a height once something
        // has told it a width — which in the app is the inspector column.
        func measured(_ state: AgentScreenState, hovering: Bool) -> CGFloat {
            height(panel(state, hovering: hovering).equatable().frame(width: 300),
                   width: 300)
        }

        let cold = measured(.live(shot), hovering: false)
        let nothingToOpen = measured(.noComputer, hovering: false)
        XCTAssertGreaterThan(
            cold, nothingToOpen + 16,
            "with the pointer nowhere near it, a panel over a running machine is "
            + "\(Int(cold))pt tall and one over a desk with no machine is "
            + "\(Int(nothingToOpen))pt — the same. The way in only exists under the "
            + "pointer, so nothing on screen says it is there.")

        let warm = measured(.live(shot), hovering: true)
        XCTAssertEqual(
            warm, cold, accuracy: 0.5,
            "the panel grows a control when the pointer arrives, which is the "
            + "hover-discovery defect drawn twice")
    }

    // MARK: - 5. the stage is sized against the window it must fit

    func testTheStageUsesHisWindowAndStillFitsTheSmallestOne() {
        XCTAssertLessThanOrEqual(
            Takeover.Stage.minWidth, Self.smallestWindow.width,
            "the take-over demands \(Int(Takeover.Stage.minWidth))pt of width inside a "
            + "window he is allowed to drag to \(Int(Self.smallestWindow.width)) — a "
            + "sheet wider than its window is clipped by AppKit and the controls on "
            + "the far edge become unreachable")
        XCTAssertLessThanOrEqual(
            Takeover.Stage.minHeight, Self.smallestWindow.height,
            "the take-over demands \(Int(Takeover.Stage.minHeight))pt of height inside "
            + "a \(Int(Self.smallestWindow.height))pt window")

        XCTAssertLessThanOrEqual(
            Takeover.Stage.idealWidth, Self.hisWindow.width,
            "the take-over asks for \(Int(Takeover.Stage.idealWidth))pt of width in a "
            + "\(Int(Self.hisWindow.width))pt window")
        XCTAssertLessThanOrEqual(
            Takeover.Stage.idealHeight, Self.hisWindow.height,
            "the take-over asks for \(Int(Takeover.Stage.idealHeight))pt of height in "
            + "a \(Int(Self.hisWindow.height))pt window")

        // **And it has to be worth opening.** The complaint is that the picture
        // in the inspector is too small to use; a stage that is not several
        // times that column is the same picture in a different frame.
        XCTAssertGreaterThanOrEqual(
            Takeover.Stage.idealWidth, Theme.inspectorWidth.upperBound * 3,
            "the take-over asks for \(Int(Takeover.Stage.idealWidth))pt against an "
            + "inspector column that is already \(Int(Theme.inspectorWidth.upperBound))"
            + "pt wide, so opening it barely changes what he can see")
    }

    /// The real view, hosted, at the width the stage asks for: what it reports
    /// as its ideal size is what AppKit sizes the sheet from, and it may not be
    /// taller than the window the sheet is inside.
    func testTheStageItselfNeverAsksForMoreThanHisWindow() {
        let request = TakeoverRequest(
            desk: "atlas", displayName: "Atlas", workspace: Self.deskWorkspace,
            focus: .screen)
        let stage = TakeoverStageView(
            request: request,
            screens: FakeScreenClient(status: Self.screenStatus),
            shells: FakeShellClient(),
            onClose: {})

        let ideal = idealSize(AnyView(stage), atWidth: Takeover.Stage.idealWidth)
        XCTAssertGreaterThan(
            ideal.height, 200,
            "the take-over never laid out to a real size at all, so the bound below "
            + "is passing over nothing")
        XCTAssertLessThanOrEqual(
            ideal.height, Self.hisWindow.height,
            "the take-over asks for \(Int(ideal.height))pt of height inside his "
            + "\(Int(Self.hisWindow.height))pt window. A sheet taller than the window "
            + "it is presented from is clipped, and Done goes off the bottom with it.")
    }

    // MARK: - harness

    /// The height a view lays out to at a given width, off-display. Arithmetic,
    /// not drawn: `fittingSize` is worked out and answers identically on a
    /// screenless window.
    private func height<V: View>(_ view: V, width: CGFloat = 380) -> CGFloat {
        idealSize(AnyView(view), atWidth: width).height
    }

    private func idealSize(_ view: AnyView, atWidth width: CGFloat) -> CGSize {
        NSApplication.shared.setActivationPolicy(.prohibited)
        let host = NSHostingView(rootView: view)
        host.translatesAutoresizingMaskIntoConstraints = false
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: width, height: 949),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        let container = NSView(frame: NSRect(x: 0, y: 0, width: width, height: 949))
        window.contentView = container
        container.addSubview(host)
        NSLayoutConstraint.activate([
            host.leadingAnchor.constraint(equalTo: container.leadingAnchor),
            host.topAnchor.constraint(equalTo: container.topAnchor),
            host.widthAnchor.constraint(equalToConstant: width),
        ])
        window.orderBack(nil)
        container.layoutSubtreeIfNeeded()
        // Bounded on purpose: an unbounded pump in this suite once held the
        // SwiftPM lock for an hour and stopped ten other files.
        let began = Date()
        while Date().timeIntervalSince(began) < 0.3 {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.005))
        }
        container.layoutSubtreeIfNeeded()
        let fitting = host.fittingSize
        let intrinsic = host.intrinsicContentSize
        window.orderOut(nil)
        window.contentView = nil
        return CGSize(width: max(max(0, intrinsic.width), fitting.width),
                      height: max(max(0, intrinsic.height), fitting.height))
    }

    /// The smallest thing `NSImage` will accept as a JPEG. No picture of
    /// anybody's signed-in browser is ever committed to this repo.
    static let jpeg: Data = {
        let image = NSImage(size: NSSize(width: 8, height: 5))
        image.lockFocus()
        NSColor(white: 0.2, alpha: 1).drawSwatch(in: NSRect(x: 0, y: 0, width: 8, height: 5))
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation,
              let rep = NSBitmapImageRep(data: tiff),
              let jpeg = rep.representation(using: .jpeg, properties: [:]) else {
            return Data([0xFF, 0xD8, 0xFF, 0xE0])
        }
        return jpeg
    }()
}

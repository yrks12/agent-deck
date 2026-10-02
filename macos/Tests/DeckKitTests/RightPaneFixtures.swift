import XCTest
import AppKit
import SwiftUI
@testable import DeckKit
@testable import DeckUI

/// Fixture data and a hosting harness for the right pane (slice D3).
///
/// Everything here is what the deck really sends, field for field, so the pane
/// is drawn and measured over the shapes it will meet — a secure handoff, a
/// couple of routines, a running machine — and not over hand-built stand-ins.
enum RightPaneFixture {

    @MainActor static func agent(_ name: String = "cos") -> Agent {
        var agent = makeAgent(name, title: "Chief of staff")
        agent.detail = "Runs the company. Hires the rest."
        return agent
    }

    /// The handoff from the reference screenshot, through the same decoder and
    /// the same `AttentionItem.make` the store uses.
    @MainActor static func handoff(desk: String = "cos") throws -> AttentionItem {
        let page = try DeckCoding.decoder.decode(HandoffsPage.self, from: Data("""
        {"handoffs":[{"id":"h1","ts":1756000100,"agent":"\(desk)",
          "asked_by":"bfffdc59-5f34-4b6b-b304-300c80cb3c25","desk_known":true,"kind":"login",
          "needs":"Sign in to Microsoft 365 admin (initech.example), then hand back",
          "state":"The tenant is created and nothing has been billed yet.",
          "where":"admin.microsoft.com","evidence":"waiting on the sign-in page",
          "status":"waiting","options":[
            {"reply":"done","available":true,
             "summary":"I'm done, continue - the desk goes back and checks the step worked"},
            {"reply":"skipped","available":true,
             "summary":"Skip this step - the desk abandons that path for good"}]}]}
        """.utf8))
        let first = try XCTUnwrap(page.handoffs.first)
        return AttentionItem.make(handoff: first, agents: [desk: agent(desk)])
    }

    static func routines() throws -> [Routine] {
        try DeckCoding.decoder.decode(RoutinesResponse.self, from: Data("""
        {"routines":[
          {"id":"r1","agent":"cos","prompt":"Morning briefing: what moved overnight",
           "trigger":{"kind":"cron","spec":"0 8 * * 1-5","tz":"Europe/London"},
           "enabled":true,"next_run_at":1757000000},
          {"id":"r2","agent":"cos","prompt":"Globex sales check",
           "trigger":{"kind":"cron","spec":"0 9 * * 1-5","tz":"Europe/London"},
           "enabled":true,"next_run_at":1757003600}]}
        """.utf8)).routines
    }

    @MainActor static func store() -> DeckStore {
        DeckStore(client: ScriptedDeckClient(), approvalPollInterval: 600)
    }

    /// A frame the deck could plausibly send: a browser-shaped picture, so a
    /// screenshot of the pane shows what the thumbnail is for.
    @MainActor static func frameJPEG(width: Int = 640, height: Int = 400) -> Data {
        let image = NSImage(size: NSSize(width: width, height: height))
        image.lockFocus()
        NSColor(white: 0.97, alpha: 1).setFill()
        NSRect(x: 0, y: 0, width: width, height: height).fill()
        NSColor(white: 0.86, alpha: 1).setFill()
        NSRect(x: 0, y: height - 36, width: width, height: 36).fill()
        NSColor.systemBlue.setFill()
        NSBezierPath(ovalIn: NSRect(x: width / 2 - 40, y: height / 2 - 20, width: 80, height: 80)).fill()
        NSColor(white: 0.75, alpha: 1).setFill()
        NSRect(x: width / 2 - 90, y: height / 2 - 60, width: 180, height: 14).fill()
        image.unlockFocus()
        guard let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let jpeg = rep.representation(using: .jpeg, properties: [:]) else { return Data() }
        return jpeg
    }

    struct StubScreens: AgentScreenClient {
        let jpeg: Data
        @MainActor init() { jpeg = RightPaneFixture.frameJPEG() }
        func screenStatus(agent: String) async throws -> AgentScreenStatus {
            AgentScreenStatus(desk: agent, isRunning: true, image: "agent-desktop",
                              container: "desk-\(agent)", display: ":99", width: 1280,
                              height: 800, staleAfter: 60, generatedAt: Date())
        }
        func screenFrame(agent: String) async throws -> AgentScreenFrame {
            AgentScreenFrame(jpeg: jpeg, serverAge: 0.2, display: ":99", receivedAt: Date())
        }
        func sendScreenInput(agent: String, _ input: ScreenInput) async throws {}
    }

    /// The pane exactly as `SettingsPanelView` builds it, from values.
    @MainActor static func pane(
        agent: Agent? = nil, attention: [AttentionItem] = [],
        routines: [Routine] = [], screens: AgentScreenClient? = nil,
        store: DeckStore? = nil
    ) -> SettingsPanelBody {
        SettingsPanelBody(
            agent: agent ?? Self.agent(), settingsError: nil, routines: routines,
            routinesProblem: nil, routinesEmptyMessage: nil,
            attention: attention, macSignIns: [:], delivery: .current,
            store: store ?? Self.store(),
            screens: screens, shells: nil)
    }

    // MARK: harness

    /// Hosts `view` in a never-shown window at `size`, laid out.
    @MainActor static func host<V: View>(_ view: V, size: CGSize) -> (NSHostingView<AnyView>, NSWindow) {
        NSApplication.shared.setActivationPolicy(.prohibited)
        // Dark, on the window colour: the pane is judged against the dark
        // reference shots, and a borderless window has no background of its own.
        let host = NSHostingView(rootView: AnyView(
            view.background(Color(nsColor: .windowBackgroundColor))
                .environment(\.colorScheme, .dark)
                .environment(\.controlActiveState, .key)))
        host.frame = NSRect(origin: .zero, size: size)
        let window = NSWindow(
            contentRect: NSRect(x: -40_000, y: -40_000, width: size.width, height: size.height),
            styleMask: [.borderless], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.appearance = NSAppearance(named: .darkAqua)
        window.contentView = host
        window.orderBack(nil)
        host.layoutSubtreeIfNeeded()
        return (host, window)
    }

    /// Every word the view draws or speaks: text fields, button titles and
    /// accessibility labels, walked through both the view tree and the
    /// accessibility tree (SwiftUI draws most text without an NSView).
    @MainActor static func words(in view: NSView) -> [String] {
        var found: [String] = []
        func visit(_ element: Any) {
            if let ax = element as? NSAccessibilityProtocol {
                if let label = ax.accessibilityLabel(), !label.isEmpty { found.append(label) }
                if let value = ax.accessibilityValue as? String, !value.isEmpty { found.append(value) }
                if let title = ax.accessibilityTitle(), !title.isEmpty { found.append(title) }
            }
            if let view = element as? NSView {
                for sub in view.subviews { visit(sub) }
            }
            for child in (element as? NSAccessibilityProtocol)?.accessibilityChildren() ?? [] { visit(child) }
        }
        visit(view)
        return found
    }
}

import Vision

extension RightPaneFixture {
    /// The pixels the view really draws, at 2x.
    @MainActor static func render(_ host: NSView) -> NSBitmapImageRep? {
        host.layoutSubtreeIfNeeded()
        guard let rep = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { return nil }
        host.cacheDisplay(in: host.bounds, to: rep)
        return rep
    }

    static func png(_ rep: NSBitmapImageRep) -> Data? { rep.representation(using: .png, properties: [:]) }

    /// Text recognised in the rendered pixels, each with its box in view
    /// points (origin top-left). What a person could read off the screen.
    static func readText(_ rep: NSBitmapImageRep) -> [(text: String, box: CGRect)] {
        guard let cg = rep.cgImage else { return [] }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        request.usesLanguageCorrection = false
        try? VNImageRequestHandler(cgImage: cg).perform([request])
        let w = CGFloat(rep.size.width), h = CGFloat(rep.size.height)
        return (request.results ?? []).compactMap { obs in
            guard let top = obs.topCandidates(1).first else { return nil }
            let b = obs.boundingBox
            return (top.string, CGRect(x: b.minX * w, y: (1 - b.maxY) * h,
                                       width: b.width * w, height: b.height * h))
        }
    }
}

extension RightPaneFixture {
    /// Writes a rendered pane where the overhaul's captures live.
    @MainActor static func saveCapture(_ rep: NSBitmapImageRep, named name: String) throws {
        let dir = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
            .appendingPathComponent("UITests/Artifacts/overhaul")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try png(rep)?.write(to: dir.appendingPathComponent(name))
    }
}

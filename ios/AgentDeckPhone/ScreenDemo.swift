#if DEBUG
import SwiftUI
import UIKit
import DeckKit

/// **A drawn computer, for simulator screenshots of the agent's screen.**
///
/// Debug builds only. `DECK_SCREEN_DEMO=1` opens the agent's screen on a
/// synthetic 1280x800 sign-in page drawn here — never a captured frame, which
/// would be a photograph of a signed-in browser. Taps are accepted and
/// dropped. Two more variables drive it with no hands:
///
///   DECK_SCREEN_DEMO_TAP=x,y   a tap at that display pixel, as if he had
///                              tapped the field (nothing is sent anywhere)
///   DECK_SCREEN_DEMO_KEYS=1    the keyboard opened after the tap
///   DECK_SCREEN_DEMO_KEYS=2    opened, then closed again two seconds later
enum ScreenDemo {
    static var isOn: Bool { ProcessInfo.processInfo.environment["DECK_SCREEN_DEMO"] == "1" }

    static var tap: DisplayPoint? {
        guard let raw = ProcessInfo.processInfo.environment["DECK_SCREEN_DEMO_TAP"] else { return nil }
        let parts = raw.split(separator: ",").compactMap { Int($0) }
        return parts.count == 2 ? DisplayPoint(x: parts[0], y: parts[1]) : nil
    }

    static var opensKeyboard: Bool { ["1", "2"].contains(ProcessInfo.processInfo.environment["DECK_SCREEN_DEMO_KEYS"]) }
    static var closesKeyboard: Bool { ProcessInfo.processInfo.environment["DECK_SCREEN_DEMO_KEYS"] == "2" }

    static var root: some View {
        AgentScreenView(route: ScreenRoute(desk: "atlas", displayName: "Atlas", threadID: "direct:atlas"),
                        client: DemoScreenClient(), messages: FixtureDeckClient())
    }
}

private struct DemoScreenClient: AgentScreenClient {
    private static let jpeg: Data = MainActor.assumeIsolated { drawPage() }

    func screenStatus(agent: String) async throws -> AgentScreenStatus {
        AgentScreenStatus(desk: agent, isRunning: true, image: "demo", container: "demo",
                          display: ":99", width: 1280, height: 800, staleAfter: 30, generatedAt: Date())
    }

    func screenFrame(agent: String) async throws -> AgentScreenFrame {
        let jpeg = await MainActor.run { Self.jpeg }
        return AgentScreenFrame(jpeg: jpeg, serverAge: 0.2, display: ":99", receivedAt: Date())
    }

    func sendScreenInput(agent: String, _ input: ScreenInput) async throws {}

    /// A browser with a tab bar, an address bar and a sign-in form whose
    /// password field sits at about (640, 520).
    @MainActor private static func drawPage() -> Data {
        let format = UIGraphicsImageRendererFormat()
        format.scale = 1
        let image = UIGraphicsImageRenderer(size: CGSize(width: 1280, height: 800), format: format).image { ctx in
            let c = ctx.cgContext
            UIColor(white: 0.95, alpha: 1).setFill(); c.fill(CGRect(x: 0, y: 0, width: 1280, height: 800))
            UIColor(white: 0.82, alpha: 1).setFill(); c.fill(CGRect(x: 0, y: 0, width: 1280, height: 40))
            UIColor.white.setFill(); c.fill(CGRect(x: 10, y: 6, width: 220, height: 34))
            UIColor(white: 0.9, alpha: 1).setFill(); c.fill(CGRect(x: 0, y: 40, width: 1280, height: 44))
            UIColor.white.setFill(); c.fill(CGRect(x: 90, y: 47, width: 1100, height: 30))
            func text(_ s: String, _ x: CGFloat, _ y: CGFloat, _ size: CGFloat, _ color: UIColor = .black) {
                (s as NSString).draw(at: CGPoint(x: x, y: y), withAttributes: [
                    .font: UIFont.systemFont(ofSize: size), .foregroundColor: color])
            }
            text("Sign in - Google Accounts", 22, 13, 13)
            text("accounts.google.com/signin/v2/challenge/pwd", 100, 52, 14, .darkGray)
            UIColor.white.setFill(); c.fill(CGRect(x: 440, y: 200, width: 400, height: 460))
            text("Google", 590, 230, 30, .systemBlue)
            text("Welcome", 580, 290, 26)
            text("atlas@example.com", 565, 335, 15, .darkGray)
            text("Enter your password", 470, 470, 13, .darkGray)
            UIColor.systemBlue.setStroke(); c.setLineWidth(2)
            c.stroke(CGRect(x: 470, y: 495, width: 340, height: 48))
            text("Forgot password?", 470, 580, 14, .systemBlue)
            UIColor.systemBlue.setFill(); c.fill(CGRect(x: 730, y: 600, width: 80, height: 36))
            text("Next", 752, 608, 15, .white)
        }
        return image.jpegData(compressionQuality: 0.85) ?? Data()
    }
}
#endif

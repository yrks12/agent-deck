import AppKit
import Darwin
import XCTest

/// **A test that leaves AppKit animations running leaves a thread behind for
/// each one, and the pool is finite.**
///
/// Measured on a hung `swift test`: 64 worker threads parked in
/// `-[NSAnimation _runBlocking]` (started by ordering a window out with the
/// default fade), the Vision text-recognition tests then waiting forever on a
/// worker that never came. The animations never finish in a screenless run, so
/// the threads are never returned.
///
/// `ThreadLeakGuard.measure` counts this process's threads around a body and
/// fails if the body left more of them than `allowance`. It is a guard on the
/// *cause*, so it fails in the test that leaks, not 500 tests later in an
/// unrelated one.
enum ThreadLeakGuard {
    /// Live threads in this process.
    static func threadCount() -> Int {
        var list: thread_act_array_t?
        var count: mach_msg_type_number_t = 0
        guard task_threads(mach_task_self_, &list, &count) == KERN_SUCCESS, let list else { return 0 }
        defer {
            vm_deallocate(mach_task_self_, vm_address_t(UInt(bitPattern: list)),
                          vm_size_t(Int(count) * MemoryLayout<thread_t>.size))
        }
        return Int(count)
    }

    /// Windows that are still ordered in. A window a test made and did not
    /// order out or close (closed windows can linger in `NSApp.windows` until
    /// their autorelease pool drains, so those are not counted).
    static func windowCount() -> Int { NSApp?.windows.filter(\.isVisible).count ?? 0 }

    /// Pumps the main run loop for `seconds` so deferred teardown can happen.
    @MainActor static func settle(_ seconds: TimeInterval = 0.3) {
        let began = Date()
        while Date().timeIntervalSince(began) < seconds {
            RunLoop.current.run(mode: .default, before: Date().addingTimeInterval(0.01))
        }
    }

    /// Runs `body`, then fails if threads or windows grew past the allowance.
    @MainActor static func assertNoLeak(
        allowance: Int = 12, file: StaticString = #filePath, line: UInt = #line,
        _ body: () throws -> Void
    ) rethrows {
        settle(0.1)
        let threadsBefore = threadCount()
        let windowsBefore = windowCount()
        try body()
        settle(0.5)
        let grew = threadCount() - threadsBefore
        XCTAssertLessThanOrEqual(
            grew, allowance,
            "the body left \(grew) more threads running than it started with "
            + "(allowance \(allowance)). AppKit animations that never finish each hold a "
            + "worker; enough of them starve every later test. Give test windows "
            + "`animationBehavior = .none` and close them.", file: file, line: line)
        XCTAssertLessThanOrEqual(
            windowCount(), windowsBefore,
            "the body left windows open", file: file, line: line)
    }
}

extension NSWindow {
    /// A borderless window that cannot animate. Use for every off-screen window
    /// a test hosts a view in; `orderOut`/`close` on a default window starts an
    /// `NSAnimation` whose thread never returns without a display.
    static func offscreenForTest(contentRect: NSRect) -> NSWindow {
        let window = NSWindow(contentRect: contentRect, styleMask: [.borderless],
                              backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.animationBehavior = .none
        return window
    }
}

/// The guard for the guard: a plain `NSWindow`, ordered in and out with none of
/// the per-window opt-outs, must not leave threads behind. It passes only
/// because `DeckTestSupport` switches window animation off process-wide.
@MainActor
final class DefaultWindowsLeaveNoThreadsTests: XCTestCase {
    func testAPlainWindowOrderedInAndOutLeavesNoThreadBehind() {
        NSApplication.shared.setActivationPolicy(.prohibited)
        ThreadLeakGuard.assertNoLeak {
            for _ in 0..<20 {
                let window = NSWindow(
                    contentRect: NSRect(x: -40_000, y: -40_000, width: 100, height: 100),
                    styleMask: [.titled], backing: .buffered, defer: false)
                window.isReleasedWhenClosed = false
                window.orderBack(nil)
                window.orderOut(nil)
                window.close()
            }
        }
    }
}

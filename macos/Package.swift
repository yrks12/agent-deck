// swift-tools-version: 5.9
import PackageDescription

// Agent Deck for macOS. No third-party dependencies, by design and by rule:
// everything below ships with the OS (SwiftUI, AppKit, Foundation, Security).
// The ONE sanctioned exception is SwiftTerm, by owner ask (2026-10-01: "the
// terminal of each computer should look and feel like a terminal"): a real
// VT100/xterm emulator is not something to hand-roll. It is linked by DeckUI
// only; DeckKit still imports no UI framework and no package.
let package = Package(
    name: "DeckApp",
    platforms: [.macOS(.v14), .iOS(.v17)],
    products: [
        .executable(name: "DeckApp", targets: ["DeckApp"]),
        .library(name: "DeckKit", targets: ["DeckKit"]),
        .library(name: "DeckUI", targets: ["DeckUI"]),
    ],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm", exact: "1.20.0"),
    ],
    targets: [
        // Domain, transport and presentation logic. Imports no UI framework, so
        // every rule the tests care about is exercisable without a host app.
        .target(name: "DeckKit", path: "Sources/DeckKit"),
        // SwiftUI views. Deliberately dumb: they render what DeckKit decided.
        .target(name: "DeckUI",
                dependencies: ["DeckKit", .product(name: "SwiftTerm", package: "SwiftTerm")],
                path: "Sources/DeckUI"),
        .executableTarget(name: "DeckApp", dependencies: ["DeckKit", "DeckUI"], path: "Sources/DeckApp"),
        // DeckUI is here for `DeckStore` only — it is the one view-layer file
        // that holds state. The pure views are not unit-tested.
        // Test-only, load-time: turns NSWindow animations off for the whole test
        // process so leaked NSAnimation workers cannot exhaust the thread pool.
        .target(name: "DeckTestSupport", path: "Tests/DeckTestSupport"),
        .testTarget(
            name: "DeckKitTests",
            dependencies: ["DeckKit", "DeckUI", "DeckTestSupport",
                           .product(name: "SwiftTerm", package: "SwiftTerm")],
            path: "Tests/DeckKitTests"
        ),
    ]
)

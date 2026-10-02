import SwiftUI
import DeckKit

/// **Which of his Mac's displays the live view shows** — on the Mac app and
/// the phone alike. Nothing at all with one display; with two or more, one
/// menu: "Display 1 · Built-in", "Display 2 · PM1561P". Picking one moves the
/// picture and every tap to that display; the choice is remembered per Mac
/// (`MacDisplayMemory`).
public struct MacDisplayMenu<MenuLabel: View>: View {
    @ObservedObject private var model: AgentComputerModel
    private let label: (String) -> MenuLabel

    public init(model: AgentComputerModel, @ViewBuilder label: @escaping (String) -> MenuLabel) {
        self.model = model
        self.label = label
    }

    private var current: MacDisplayInfo? {
        MacDisplays.resolve(model.macDisplay, in: model.displays) ?? MacDisplays.resolve(nil, in: model.displays)
    }

    private var currentLabel: String {
        current.map { MacDisplays.label($0, in: model.displays) } ?? "Display"
    }

    public var body: some View {
        if model.showsDisplayPicker {
            Menu {
                ForEach(MacDisplays.ordered(model.displays)) { display in
                    let text = MacDisplays.label(display, in: model.displays)
                    Button {
                        Task { await model.chooseDisplay(display.isMain ? nil : display.id) }
                    } label: {
                        // Plain text, never `Label` (BaselineFallbackTests).
                        Text(display.id == current?.id ? "✓ " + text : text)
                    }
                }
            } label: {
                label(currentLabel)
            }
            .accessibilityLabel("Display: \(currentLabel). Choose which display to show")
        }
    }
}

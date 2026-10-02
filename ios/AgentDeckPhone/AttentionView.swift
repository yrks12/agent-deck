import SwiftUI
import DeckKit

/// **Needs your attention.** Every desk that is stopped on him — a permission
/// question (allow / deny) or a step only a human can take (done / skip) — in
/// one list, oldest first, the same order the Mac strip uses.
struct AttentionView: View {
    @EnvironmentObject private var store: PhoneStore
    @Binding var path: NavigationPath
    @State private var screen: ScreenRoute?

    var body: some View {
        ScrollView {
            LazyVStack(spacing: 12) {
                if store.attention.isEmpty {
                    CharacterState(size: 84, title: "Nothing needs you right now.",
                                   message: "Your desks are handling it. When one needs a yes, or a step only you can take, it lands here.")
                        .padding(.top, 110)
                } else {
                    ForEach(store.attention) { item in
                        AttentionCard(item: item, look: look(item), onOpen: { open(item) },
                                      remote: store.remoteSignIns[item.askID],
                                      canAskMac: store.canAskMac,
                                      canTakeOver: store.screenClient != nil && !store.isReadOnly,
                                      askMac: { method in Task { await store.askMac(method, for: item) } },
                                      takeOver: { takeOver(item) }) { action in
                            Task { await store.act(action, on: item) }
                        }
                    }
                }
            }
            .padding(16)
        }
        .background(PhoneTheme.canvas)
        .refreshable { await store.refreshAttention() }
        .fullScreenCover(item: $screen) { route in
            AgentScreenView(route: route, client: store.screenClient, messages: store.isReadOnly ? nil : store.client)
        }
        .navigationTitle("Attention")
        .navigationBarTitleDisplayMode(.large)
    }

    private func look(_ item: AttentionItem) -> AvatarLook {
        guard let name = item.deskName else { return AvatarLook.forName(item.askID) }
        return store.agents[name]?.look ?? AvatarLook.forName(name)
    }

    private func takeOver(_ item: AttentionItem) {
        guard let name = item.deskName else { return }
        screen = ScreenRoute(desk: name, displayName: store.agents[name]?.displayName ?? item.deskLine,
                             threadID: "direct:\(name)")
    }

    private func open(_ item: AttentionItem) {
        guard let name = item.deskName else { return }
        path.append(ThreadRoute(threadID: "direct:\(name)", agent: name))
    }
}

struct AttentionCard: View {
    let item: AttentionItem
    let look: AvatarLook
    let onOpen: () -> Void
    var remote: RemoteSignInPhase? = nil
    var canAskMac = false
    var canTakeOver = false
    var askMac: (LoginMethod) -> Void = { _ in }
    var takeOver: () -> Void = {}
    let act: (AttentionAction) -> Void

    private var surface: SignInCard.Surface { .phone(canAskMac: canAskMac) }
    /// The same ordered list the Mac's card draws (DeckKit's `SignInCard`).
    private var signInActions: [SignInCardAction] {
        SignInCard.actions(for: item, surface: surface, canTakeOver: canTakeOver)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 10) {
                AvatarView(look: look, attention: .waitingForYou, isDimmed: false, localAvatarURL: nil, size: 36)
                VStack(alignment: .leading, spacing: 1) {
                    Text(item.deskLine).font(.headline)
                    Text(kindLine).font(.caption.weight(.semibold)).foregroundStyle(PhoneTheme.waiting)
                }
                Spacer()
                if let at = item.at {
                    Text(SidebarTimestamp.short(at)).font(.caption).foregroundStyle(.secondary)
                }
            }
            what
            if !item.place.isEmpty {
                Label(item.place, systemImage: isHandoff ? "safari" : "folder")
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            actions
        }
        .padding(16)
        .card(radius: 20)
    }

    private var isHandoff: Bool {
        if case .handoff = item.need { return true }
        return false
    }

    private var kindLine: String {
        switch item.need {
        case .permission(let tool, _): return "Asks permission · \(tool)"
        case .handoff(let kind, _, _): return "Needs you to step in · \(kind)"
        }
    }

    @ViewBuilder
    private var what: some View {
        switch item.need {
        case .permission(_, let subject):
            Text(subject)
                .font(.system(.subheadline, design: .monospaced))
                .lineLimit(6)
                .padding(10)
                .frame(maxWidth: .infinity, alignment: .leading)
                .background(PhoneTheme.field, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        case .handoff(_, let needs, let state):
            VStack(alignment: .leading, spacing: 4) {
                Text(needs).font(.body.weight(.medium))
                if !state.isEmpty { Text(state).font(.subheadline).foregroundStyle(.secondary) }
            }
        }
    }

    private var actions: some View {
        VStack(spacing: 8) {
            if let remote { remoteLine(remote) }
            if isHandoff {
                // A sign-in card: one button per row, in the Mac's order.
                ForEach(Array(signInActions.enumerated()), id: \.element.id) { index, action in
                    if remote?.isWorking != true || action.method == nil {
                        signInButton(action, primary: index == 0)
                    }
                }
            } else {
                HStack(spacing: 8) {
                    ForEach(item.actions.filter(\.isAvailable)) { action in
                        Button { Haptics.tap(); act(action) } label: {
                            pill(action.label, primary: isPrimary(action), danger: isDanger(action))
                        }
                        .buttonStyle(.plain)
                        .accessibilityHint(action.summary)
                    }
                }
            }
            if let label = item.openDeskLabel {
                Button(action: onOpen) {
                    Label(label, systemImage: "arrow.up.right").font(.subheadline.weight(.medium))
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func signInButton(_ action: SignInCardAction, primary: Bool) -> some View {
        let label = SignInCard.label(action, item: item, surface: surface)
        var available = true
        if case .answer(let a) = action { available = a.isAvailable }
        return Button {
            Haptics.tap()
            switch action {
            case .chromeLogin, .freshPasskey: if let m = action.method { askMac(m) }
            case .answer(let a): act(a)
            case .takeOver: takeOver()
            }
        } label: {
            pill(label, primary: primary, danger: false)
        }
        .buttonStyle(.plain)
        .disabled(!available)
        .accessibilityHint(SignInCard.hint(action, item: item, surface: surface))
    }

    @ViewBuilder
    private func remoteLine(_ phase: RemoteSignInPhase) -> some View {
        HStack(spacing: 8) {
            switch phase {
            case .waitingForMac: ProgressView().controlSize(.small)
            case .signedIn, .finishOnMac: Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case .macOffline, .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            }
            Text(phase.line).font(.subheadline.weight(.medium))
            Spacer(minLength: 0)
            if phase.canRetry {
                Button("Try again") { askMac(lastMethod) }.font(.subheadline.weight(.semibold))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    /// Retry repeats the Mac-side method the card leads with.
    private var lastMethod: LoginMethod {
        signInActions.compactMap(\.method).first ?? .chrome
    }

    private func pill(_ text: String, primary: Bool, danger: Bool) -> some View {
        Text(text)
            .font(.subheadline.weight(.semibold))
            .lineLimit(1).minimumScaleFactor(0.8)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 11)
            .foregroundStyle(primary ? PhoneTheme.canvas : danger ? .red : .primary)
            .background(primary ? Color.primary : PhoneTheme.canvas.opacity(0.001),
                        in: RoundedRectangle(cornerRadius: 12, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous)
                .strokeBorder(primary ? .clear : PhoneTheme.cardStroke, lineWidth: 1))
    }

    private func isPrimary(_ a: AttentionAction) -> Bool {
        switch a.verb {
        case .permission(.once), .handoff(.done): return true
        default: return false
        }
    }

    private func isDanger(_ a: AttentionAction) -> Bool {
        if case .permission(.never) = a.verb { return true }
        return false
    }

}

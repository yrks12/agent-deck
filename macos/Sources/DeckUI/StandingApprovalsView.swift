import SwiftUI
import DeckKit

/// **Standing approvals** (§23): what each desk may do without asking, up to a
/// daily limit. Proposals from desks first, with one-tap Approve / Dismiss;
/// what is live now with today's use and one-tap Revoke; add and edit; and the
/// log of every action a policy covered.
///
/// One file for both apps (compiled into the iPhone target via
/// `ios/project.yml`); it only draws `StandingApprovalsModel` and
/// `StandingPresentation`.
public struct StandingApprovalsView: View {
    @StateObject private var model: StandingApprovalsModel
    let desks: [String]
    let onClose: (() -> Void)?
    @State private var tab = 0
    @State private var editing: Editing?

    struct Editing: Identifiable {
        let id: String
        let draft: StandingDraft
        let policyID: String?
    }

    public init(client: StandingApprovalsClient, desks: [String] = [], onClose: (() -> Void)? = nil) {
        _model = StateObject(wrappedValue: StandingApprovalsModel(client: client))
        self.desks = desks
        self.onClose = onClose
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            DeckPillPicker(selection: $tab, options: [(0, "Approvals"), (1, "Log")], label: "Show")
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.vertical, 10)
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    if let problem = model.problem {
                        FlatLabel(problem, systemImage: "exclamationmark.triangle")
                            .font(.callout).foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    if tab == 0 { policies } else { log }
                }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(DeckPalette.canvas)
        // Live usage: re-read while the screen is open.
        .task {
            while !Task.isCancelled {
                await model.load()
                try? await Task.sleep(nanoseconds: 15_000_000_000)
            }
        }
        .task(id: tab) { if tab == 1 { await model.loadAudit() } }
        .sheet(item: $editing) { e in
            StandingEditorView(model: model, draft: e.draft, policyID: e.policyID, desks: desks) { editing = nil }
        }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text(StandingPresentation.title).font(.title3.weight(.semibold)).accessibilityAddTraits(.isHeader)
            Spacer(minLength: 0)
            Button("Add") { editing = Editing(id: "new", draft: StandingDraft(desk: desks.first ?? "*"), policyID: nil) }
                .disabled(model.phase == .unavailable)
            if let onClose { Button("Done", action: onClose).keyboardShortcut(.cancelAction) }
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }

    @ViewBuilder private var policies: some View {
        switch model.phase {
        case .loading: ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
        case .unavailable: note(StandingPresentation.unavailable)
        case .failed(let text): note(text)
        case .loaded:
            let s = model.sections
            if s.proposed.isEmpty && s.active.isEmpty && s.ended.isEmpty { note(StandingPresentation.empty) }
            group("Proposed by your desks", s.proposed)
            group("Active", s.active)
            group("Ended", s.ended)
            Text(StandingPresentation.floor).font(.caption).foregroundStyle(.secondary)
        }
    }

    @ViewBuilder private func group(_ title: String, _ rows: [StandingPolicy]) -> some View {
        if !rows.isEmpty {
            Text(title).font(.headline).padding(.top, 4)
            ForEach(rows) { row($0) }
        }
    }

    private func row(_ p: StandingPolicy) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .top) {
                Text(StandingPresentation.heading(p)).font(.headline)
                Spacer(minLength: 8)
                Text(StandingPresentation.state(p)).font(.caption).foregroundStyle(.secondary)
            }
            Text(StandingPresentation.limits(p.limits, kind: p.kind)).font(.callout)
            if let scope = StandingPresentation.scope(p) {
                Text(scope).font(.system(.caption, design: .monospaced)).foregroundStyle(.secondary)
            }
            if !p.note.isEmpty { Text(p.note).font(.caption).foregroundStyle(.secondary) }
            if p.status == .active, let fill = StandingPresentation.fill(p) {
                ProgressView(value: fill).tint(fill >= 1 ? .orange : DeckPalette.ink)
                Text(StandingPresentation.usage(p).joined(separator: " · "))
                    .font(.caption).monospacedDigit().foregroundStyle(.secondary)
            }
            buttons(p)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private func buttons(_ p: StandingPolicy) -> some View {
        let busy = model.busy.contains(p.id)
        HStack(spacing: 8) {
            switch p.status {
            case .proposed:
                Button("Approve") { Task { await model.approve(p) } }.buttonStyle(.deckPrimary)
                Button("Dismiss") { Task { await model.revoke(p) } }.buttonStyle(.deckSecondary)
                Button("Edit") { edit(p) }.buttonStyle(.deckSecondary)
            case .active where !p.expired:
                Button("Revoke", role: .destructive) { Task { await model.revoke(p) } }.buttonStyle(.deckDestructive)
                Button("Edit") { edit(p) }.buttonStyle(.deckSecondary)
            default:
                EmptyView()
            }
            if busy { ProgressView().controlSize(.small) }
        }
        .disabled(busy)
    }

    private func edit(_ p: StandingPolicy) { editing = Editing(id: p.id, draft: StandingDraft(p), policyID: p.id) }

    @ViewBuilder private var log: some View {
        if model.audit.isEmpty {
            note(StandingPresentation.emptyLog)
        } else {
            ForEach(model.audit) { line in
                VStack(alignment: .leading, spacing: 2) {
                    HStack(alignment: .top) {
                        Text(StandingPresentation.auditTitle(line)).font(.callout).lineLimit(2)
                        Spacer(minLength: 8)
                        if let at = line.at { Text(SidebarTimestamp.short(at)).font(.caption).foregroundStyle(.secondary) }
                    }
                    Text(StandingPresentation.auditDetail(line)).font(.caption).foregroundStyle(.secondary)
                }
                .padding(.vertical, 6)
                .accessibilityElement(children: .combine)
                Divider()
            }
        }
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary)
            .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
    }
}

/// Add or edit one policy: desk, kind (fixed once made), pattern, limits, expiry.
struct StandingEditorView: View {
    @ObservedObject var model: StandingApprovalsModel
    @State var draft: StandingDraft
    let policyID: String?
    let desks: [String]
    let onDone: () -> Void
    @State private var count = ""
    @State private var dollars = ""
    @State private var recipients = ""
    @State private var expires = false
    @State private var expiry = Date().addingTimeInterval(7 * 86_400)

    var body: some View {
        VStack(spacing: 0) {
            // Drawn in the pane, never as the window's title: a view body that
            // writes the titlebar re-lays the window out on every pass.
            HStack {
                Button("Cancel", action: onDone).keyboardShortcut(.cancelAction)
                Spacer(minLength: 8)
                Text(policyID == nil ? "New standing approval" : "Edit standing approval")
                    .font(.headline).accessibilityAddTraits(.isHeader)
                Spacer(minLength: 8)
                Button("Save") { Task { if await model.save(filled, editing: policyID) { onDone() } } }
                    .keyboardShortcut(.defaultAction)
                    .disabled(model.busy.contains(policyID ?? "new"))
            }
            .padding(.horizontal, 16).padding(.vertical, 12)
            Divider()
            Form {
                Section {
                    Picker("Desk", selection: $draft.desk) {
                        Text(StandingPresentation.desk("*")).tag("*")
                        ForEach(deskChoices, id: \.self) { Text($0).tag($0) }
                    }
                    Picker("Action", selection: kindBinding) {
                        ForEach(StandingKind.all, id: \.wire) { Text(StandingPresentation.kindLabel($0)).tag($0.wire) }
                    }
                    .disabled(policyID != nil)
                    if draft.kind == .runCommand {
                        TextField("Commands, such as gh pr*", text: $draft.pattern)
                    }
                }
                Section("Daily limit") {
                    TextField("How many a day", text: $count)
                    TextField("Dollars a day", text: $dollars)
                    TextField("Only to (addresses, domains or hosts)", text: $recipients)
                    TextField("Only as account", text: $draft.limits.account)
                }
                Section {
                    HStack {
                        Text("Expires")
                        Spacer(minLength: 8)
                        Toggle("", isOn: $expires).labelsHidden().toggleStyle(.switch).tint(DeckPalette.working)
                            .accessibilityLabel("Expires")
                    }
                    if expires { DatePicker("On", selection: $expiry, in: Date()...) }
                    TextField("Note", text: $draft.note)
                }
                if let problem = model.problem {
                    Text(problem).font(.callout).foregroundStyle(.orange)
                }
            }
            .formStyle(.grouped)
        }
        .background(DeckPalette.canvas)
        #if os(macOS)
        .frame(minWidth: 440, minHeight: 520)
        #endif
        .onAppear {
            model.problem = nil
            count = draft.limits.countPerDay.map(String.init) ?? ""
            dollars = draft.limits.usdPerDay.map { String(format: "%.2f", $0) } ?? ""
            recipients = draft.limits.recipients.joined(separator: ", ")
            if let end = draft.expiresAt { expires = true; expiry = end }
        }
    }

    private var deskChoices: [String] {
        (desks + [draft.desk]).filter { $0 != "*" && !$0.isEmpty }.reduce(into: [String]()) {
            if !$0.contains($1) { $0.append($1) }
        }
    }

    private var kindBinding: Binding<String> {
        Binding(get: { draft.kind.wire }, set: { draft.kind = StandingKind(wire: $0) })
    }

    /// The form's text, read into the draft the deck is sent.
    private var filled: StandingDraft {
        var d = draft
        d.limits.countPerDay = StandingPresentation.count(count)
        d.limits.usdPerDay = StandingPresentation.dollars(dollars)
        d.limits.recipients = recipients.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
        d.expiresAt = expires ? expiry : nil
        if d.kind != .runCommand && d.pattern.isEmpty { d.pattern = "*" }
        return d
    }
}

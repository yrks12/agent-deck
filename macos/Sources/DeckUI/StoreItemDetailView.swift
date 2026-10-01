import SwiftUI
import DeckKit

/// One connector or skill, and the control that installs it.
///
/// The typed keys live in `draft` — this view's own state — and nowhere else.
/// `takeRequest()` hands them over once and empties them; leaving the detail
/// empties them too.
struct StoreItemDetailView: View {
    let item: StoreItem
    @ObservedObject var model: StorePanelModel
    let onBack: () -> Void

    @State private var draft: StoreInstallDraft
    @State private var isInstalling = false
    @State private var result: StoreInstallResult?
    @State private var failure: String?
    @State private var readme: String?

    init(item: StoreItem, model: StorePanelModel, onBack: @escaping () -> Void,
         result: StoreInstallResult? = nil) {
        self.item = item
        self.model = model
        self.onBack = onBack
        _draft = State(initialValue: StoreInstallDraft(item: item))
        _result = State(initialValue: result)
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Button(action: onBack) {
                    FlatLabel("All \(item.kind == "skill" ? "skills" : "connectors")",
                              systemImage: "chevron.left")
                }
                .buttonStyle(.plain)
                .foregroundStyle(.secondary)
                .keyboardShortcut("[", modifiers: .command)

                about
                Divider()
                installSection
                if let readme, !readme.isEmpty {
                    Divider()
                    Text(readme)
                        .font(.callout)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onDisappear { draft.clearSecrets() }
        .task {
            // Skills carry a README only on the item route.
            guard item.kind == "skill", readme == nil,
                  let full = try? await model.client.storeItem(id: item.id) else { return }
            readme = full.readme
        }
    }

    // MARK: about

    private var about: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .center, spacing: 12) {
                StoreKindIcon(kind: item.kind)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 6) {
                        Text(item.title).font(.title3.weight(.semibold))
                        StoreBadge(trust: item.trust)
                    }
                    Text(item.publisher + (item.version.map { " · \($0)" } ?? ""))
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
            Text(item.description)
                .fixedSize(horizontal: false, vertical: true)
            if !item.trustNote.isEmpty {
                Text(item.trustNote)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            StoreMetaRow(item: item)
            if !item.installedOn.isEmpty {
                Text("Installed on \(item.installedOn.joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.green)
            }
        }
    }

    // MARK: install

    @ViewBuilder
    private var installSection: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Install")
                .font(.subheadline.weight(.medium))
                .foregroundStyle(.secondary)
                .accessibilityAddTraits(.isHeader)

            if draft.showsOAuthNote {
                VStack(alignment: .leading, spacing: 4) {
                    FlatLabel(item.auth == "oauth" ? "OAuth sign-in isn't supported yet."
                                                   : "This one can't be installed yet.",
                              systemImage: "lock")
                        .font(.callout.weight(.medium))
                    if let why = item.whyNot {
                        Text(why).font(.caption).foregroundStyle(.secondary)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            } else {
                targetPicker
                ForEach(draft.secretFields, id: \.name) { field in
                    VStack(alignment: .leading, spacing: 3) {
                        SecureField(field.label + (field.required ? "" : " (optional)"),
                                    text: Binding(
                                        get: { draft.secretValue(for: field.name) },
                                        set: { draft.setSecret($0, for: field.name) }))
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel(field.label)
                        if !field.description.isEmpty {
                            Text(field.description).font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
                if !draft.secretFields.isEmpty {
                    Text("Keys go into the deck's vault and are only given to the desks you pick. This Mac forgets them as soon as they are sent.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                if draft.needsUnverifiedConfirm {
                    VStack(alignment: .leading, spacing: 6) {
                        FlatLabel(StoreBrowser.unverifiedWarning,
                                  systemImage: "exclamationmark.triangle.fill")
                            .font(.callout)
                            .foregroundStyle(.orange)
                            .fixedSize(horizontal: false, vertical: true)
                        HStack(spacing: 6) {
                            Toggle("", isOn: $draft.confirmedUnverified)
                                .labelsHidden()
                                .toggleStyle(.checkbox)
                                .accessibilityLabel("I understand, install it anyway")
                            Text("I understand, install it anyway")
                                .onTapGesture { draft.confirmedUnverified.toggle() }
                                .accessibilityHidden(true)
                        }
                    }
                    .padding(10)
                    .background(Color.orange.opacity(0.12), in: RoundedRectangle(cornerRadius: 8))
                }
                HStack(spacing: 10) {
                    Button {
                        Task { await install() }
                    } label: {
                        Text(item.installedOn.isEmpty ? "Install" : "Install again")
                            .frame(minWidth: 80)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(!draft.canInstall || isInstalling)
                    if isInstalling {
                        ProgressView().controlSize(.small)
                    } else if let problem = draft.problem {
                        Text(problem).font(.caption).foregroundStyle(.secondary)
                    }
                }
            }

            if let failure {
                FlatLabel(failure, systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.red)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let result {
                VStack(alignment: .leading, spacing: 3) {
                    FlatLabel("Installed", systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.callout.weight(.medium))
                    ForEach(result.reload, id: \.desk) { reload in
                        Text(StoreBrowser.reloadLine(reload))
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    private var targetPicker: some View {
        VStack(alignment: .leading, spacing: 6) {
            Picker("Install on", selection: Binding(
                get: { if case .allDesks = draft.target { return 0 } else { return 1 } },
                set: { draft.target = $0 == 0 ? .allDesks : .chosen([]) })) {
                Text("All desks").tag(0)
                Text("Chosen desks").tag(1)
            }
            .pickerStyle(.radioGroup)
            .horizontalRadioGroupLayout()
            if case .chosen(let chosen) = draft.target {
                if model.desks.isEmpty {
                    Text("No desks on the roster yet.").font(.caption).foregroundStyle(.secondary)
                }
                LazyVGrid(columns: [GridItem(.adaptive(minimum: 140), alignment: .leading)],
                          alignment: .leading, spacing: 4) {
                    ForEach(model.desks, id: \.self) { desk in
                        HStack(spacing: 6) {
                            Toggle("", isOn: Binding(
                                get: { chosen.contains(desk) },
                                set: { on in
                                    var next = chosen
                                    if on { next.insert(desk) } else { next.remove(desk) }
                                    draft.target = .chosen(next)
                                }))
                                .labelsHidden()
                                .toggleStyle(.checkbox)
                                .accessibilityLabel("Install on \(desk)")
                            Text(desk).accessibilityHidden(true)
                        }
                    }
                }
            }
        }
    }

    private func install() async {
        guard let request = draft.takeRequest() else { return }
        isInstalling = true
        failure = nil
        defer { isInstalling = false }
        do {
            result = try await model.install(request)
            draft.confirmedUnverified = false
        } catch {
            failure = StorePanelModel.deckError(error).userFacingText
        }
    }
}

// MARK: - Installed

/// Grouped by desk; each row has its version, Update when there is one, and
/// Remove.
struct StoreInstalledList: View {
    @ObservedObject var model: StorePanelModel

    var body: some View {
        Group {
            switch model.installed {
            case .loading:
                ProgressView("Loading what is installed")
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .empty:
                ContentUnavailableView(
                    "Nothing installed yet", systemImage: "shippingbox",
                    description: Text("Connectors and skills you install show up here, desk by desk."))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            case .failed(let error):
                StoreFailure(error: error) { Task { await model.loadInstalled() } }
            case .loaded(let page):
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 14) {
                        if let note = model.installedNote {
                            StoreNote(text: note, systemImage: "arrow.clockwise")
                        }
                        ForEach(page.desks.filter { !$0.items.isEmpty }, id: \.desk) { desk in
                            VStack(alignment: .leading, spacing: 6) {
                                Text(desk.desk)
                                    .font(.subheadline.weight(.medium))
                                    .foregroundStyle(.secondary)
                                    .accessibilityAddTraits(.isHeader)
                                ForEach(desk.items) { row in
                                    installedRow(row)
                                }
                            }
                        }
                    }
                    .padding(16)
                }
            }
        }
        .task { await model.loadInstalled() }
    }

    private func installedRow(_ row: StoreInstalled) -> some View {
        let busy = model.busyRows.contains(StorePanelModel.rowKey(row))
        return HStack(spacing: 12) {
            StoreKindIcon(kind: row.kind)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 6) {
                    Text(row.title).font(.body.weight(.medium)).lineLimit(1)
                    StoreBadge(trust: row.trust)
                }
                let version = StoreBrowser.versionText(row)
                Text(version.isEmpty ? (row.kind == "skill" ? "Skill" : "Connector")
                                     : version)
                    .font(.caption.monospaced())
                    .foregroundStyle(.secondary)
            }
            Spacer(minLength: 0)
            if busy { ProgressView().controlSize(.small) }
            Button("Update") { Task { await model.update(row) } }
                .disabled(!row.updateAvailable || busy)
                .help(row.updateAvailable ? "Update to the newest version" : "Up to date")
            Button("Remove", role: .destructive) { Task { await model.remove(row) } }
                .disabled(busy)
                .accessibilityLabel("Remove \(row.title) from \(row.desk)")
        }
        .padding(10)
        .background(.quaternary.opacity(0.6), in: RoundedRectangle(cornerRadius: 10))
    }
}

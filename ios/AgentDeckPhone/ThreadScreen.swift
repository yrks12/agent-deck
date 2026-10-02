import SwiftUI
import DeckKit

/// **A conversation with one desk.** Bubbles for what was said, grey captions
/// for what the deck said, decision cards he answers with one tap, and a
/// composer pinned above the keyboard.
struct ThreadScreen: View {
    @EnvironmentObject private var store: PhoneStore
    @Environment(\.phoneCalls) private var calls
    @Environment(\.phoneVoice) private var voice
    @StateObject private var model: ThreadModel
    @State private var draft = ""
    @State private var screen: ScreenRoute?
    @State private var picking: AttachSource?
    @State private var pickingFiles = false
    let agentName: String

    init(route: ThreadRoute, model: ThreadModel) {
        agentName = route.agent
        _model = StateObject(wrappedValue: model)
    }

    private var agent: Agent? { store.agents[agentName] }
    private var row: SidebarRow? { store.row(forAgent: agentName) }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(model.items) { item in
                        TranscriptItemView(item: item, model: model, agents: store.agents)
                            .id(item.id)
                    }
                    Color.clear.frame(height: 6).id("bottom")
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)
            }
            .scrollDismissesKeyboard(.interactively)
            .defaultScrollAnchor(.bottom)
            .onChange(of: model.items.last?.id) { _, _ in
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            // A tapped quote: to the original, a turn after the rollup it was
            // in has been opened.
            .onChange(of: model.scrollTarget) { _, target in
                guard let target else { return }
                DispatchQueue.main.async {
                    withAnimation(.easeOut(duration: 0.25)) { proxy.scrollTo(target.messageID, anchor: .center) }
                }
            }
            .overlay { if !model.loaded { ProgressView() } }
        }
        .background(PhoneTheme.canvas)
        .safeAreaInset(edge: .bottom) {
            VStack(spacing: 0) {
                if let voice { ListenStrip(player: voice.listen) }
                composer
            }
        }
        .environment(\.attachmentFetch, store.attachmentImages.map { client in { try await client.attachmentData(url: $0) } })
        .environment(\.mediaLoader, store.mediaLoader)
        // iPad (and in-app drag on iPhone): a picture or file dropped on the
        // conversation is attached like a picked one.
        .onDrop(of: [.image, .fileURL, .data], isTargeted: nil) { providers in
            guard model.tray != nil else { return false }
            Task {
                for (index, provider) in providers.enumerated() {
                    if let item = await AttachmentLoading.load(provider, origin: .pasted, fallbackName: "dropped-\(index + 1)") {
                        attach([item])
                    }
                }
            }
            return true
        }
        .sheet(item: $picking) { source in
            switch source {
            case .photos: PhotoPicker { attach($0) }.ignoresSafeArea()
            case .camera: CameraPicker { attach($0) }.ignoresSafeArea()
            }
        }
        .fileImporter(isPresented: $pickingFiles, allowedContentTypes: [.item], allowsMultipleSelection: true) { result in
            guard case .success(let urls) = result else { return }
            attach(urls.compactMap(AttachmentLoading.file(at:)))
        }
        .navigationBarTitleDisplayMode(.inline)
        .toolbarBackground(.visible, for: .navigationBar)
        .toolbar(.hidden, for: .tabBar)
        .toolbar {
            ToolbarItem(placement: .principal) { header }
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Haptics.tap()
                    screen = ScreenRoute(desk: agentName,
                                         displayName: agent?.displayName ?? Agent.displayName(forWireName: agentName),
                                         threadID: model.threadID)
                } label: {
                    Image(systemName: "display")
                }
                .accessibilityLabel("\(agent?.displayName ?? agentName)'s screen")
            }
            ToolbarItem(placement: .topBarTrailing) { SpokenRepliesButton() }
            ToolbarItem(placement: .topBarTrailing) {
                Button { store.placeCall(calls, agent: agentName) } label: {
                    Image(systemName: "phone.fill")
                }
                .accessibilityLabel("Call \(agent?.displayName ?? agentName)")
            }
        }
        .alert("Couldn't do that", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("OK", role: .cancel) {}
        } message: { Text(model.error ?? "") }
        .onAppear {
            wireVoice()
            model.start()
            store.markRead(agent: agentName)
            #if DEBUG
            // Demo/proof hook: `DECK_OPEN_SCREEN=<desk>` opens its screen on launch.
            if ProcessInfo.processInfo.environment["DECK_OPEN_SCREEN"] == agentName, screen == nil {
                screen = ScreenRoute(desk: agentName, displayName: agent?.displayName ?? agentName,
                                     threadID: model.threadID)
            }
            #endif
        }
        .onDisappear { model.stop() }
        .fullScreenCover(item: $screen) { route in
            AgentScreenView(route: route, client: store.screenClient, messages: store.isReadOnly ? nil : store.client)
        }
    }

    private var header: some View {
        HStack(spacing: 8) {
            if let row {
                AvatarView(look: row.look, attention: row.attention, isDimmed: row.agent.state == .offline,
                           localAvatarURL: nil, size: 28)
            }
            VStack(alignment: .leading, spacing: 0) {
                Text(agent?.displayName ?? Agent.displayName(forWireName: agentName))
                    .font(.subheadline.weight(.semibold))
                // What this desk is for, the same line the Mac's header draws.
                DeskSummaryText(text: agent?.summaryLine, font: .caption2)
                StatusLineText(line: status, font: .caption2)
            }
        }
        .accessibilityElement(children: .combine)
    }

    /// The same rule as the Mac's header: `ThreadHeaderStatus` in DeckKit.
    private var status: StatusLine {
        ThreadHeaderStatus.line(attention: row?.attention, connection: model.connection,
                                loaded: model.loaded, agentState: agent?.state.label ?? "")
    }

    /// His voice message lands in this thread, and the desk's answer to it is
    /// spoken when "Spoken replies" is on.
    private func wireVoice() {
        guard let voice else { return }
        let model = self.model
        let agent = self.agent
        model.onMessages = { desk, messages in
            voice.replies.sync(desk: desk, messages: messages)
            voice.listen.messages = messages
        }
        voice.notes.onSent = { [weak model] sent in
            guard let model, sent.threadID == model.threadID else { return }
            model.voiceNoteSent(sent)
            voice.replies.arm(desk: model.desk, after: sent.id, voice: agent?.voice)
        }
    }

    @ViewBuilder
    private var composer: some View {
        if let voice, !model.isReadOnlyThread {
            VoiceComposerSwitch(notes: voice.notes) { typingComposer } recording: {
                VoiceNoteRecordingBar(notes: voice.notes)
            }
        } else {
            typingComposer
        }
    }

    /// Voice when the box is empty, send once there are words — the Mac's
    /// rule too (`ComposerMode` in DeckKit). An attachment counts as words;
    /// send waits until every one has reached the deck.
    private var mode: ComposerMode {
        if let tray = model.tray, !tray.isEmpty {
            return .send(enabled: tray.isReady && !model.sending)
        }
        return ComposerMode.make(draft: draft, isReadOnly: model.isReadOnlyThread,
                                 canRecord: voice != nil, isSending: model.sending)
    }

    private func attach(_ items: [PreparedAttachment]) {
        guard let tray = model.tray, !items.isEmpty else { return }
        items.forEach { tray.add($0) }
        Haptics.tap()
    }

    @ViewBuilder
    private var typingComposer: some View {
        if model.isReadOnlyThread {
            Text(ThreadPresentation.viewOnlyFooter)
                .font(.footnote).foregroundStyle(.secondary)
                .frame(maxWidth: .infinity)
                .padding(.vertical, 12)
                .background(.bar)
        } else {
            VStack(spacing: 0) {
            if let target = model.replyDraft.target {
                PhoneReplyStrip(target: target) { model.cancelReply() }
            }
            if let tray = model.tray { TrayObserver(tray: tray) }
            HStack(alignment: .bottom, spacing: 8) {
                if model.tray != nil {
                    AttachMenu(pick: { picking = $0 }, files: { pickingFiles = true })
                }
                ComposerTextView(text: $draft, placeholder: "Message \(agent?.displayName ?? agentName)",
                                 maxLines: 6, onPaste: { attach($0) },
                                 focusToken: model.replyDraft.replyToID)
                    .background(PhoneTheme.field, in: RoundedRectangle(cornerRadius: 22, style: .continuous))
                switch mode {
                case .record:
                    if let voice { VoiceNoteRecordButton(notes: voice.notes, threadID: model.threadID) }
                case .send(let enabled):
                    Button {
                        let text = draft
                        Task { if await model.send(text) { draft = "" } }
                    } label: {
                        Image(systemName: model.sending ? "ellipsis" : "arrow.up")
                            .font(.system(size: 17, weight: .bold))
                            .foregroundStyle(PhoneTheme.canvas)
                            .frame(width: 38, height: 38)
                            .background(Circle().fill(enabled ? Color.primary : Color.secondary.opacity(0.5)))
                    }
                    .disabled(!enabled)
                    .accessibilityLabel("Send")
                case .viewOnly:
                    EmptyView()
                }
            }
            .padding(.horizontal, 12)
            .padding(.top, 8)
            .padding(.bottom, 8)
            }
            .background(.bar)
        }
    }
}

/// The tray, observed on its own so a progress tick redraws only it — and the
/// send button's enabled state with it.
private struct TrayObserver: View {
    @ObservedObject var tray: AttachmentTray
    var body: some View { AttachmentTrayView(tray: tray) }
}

/// One drawable row, exhaustively: a new kind of row is a compile error here.
struct TranscriptItemView: View {
    let item: TranscriptItem
    @ObservedObject var model: ThreadModel
    let agents: [String: Agent]

    var body: some View {
        switch item {
        case .dayBreak(let day):
            Text(day.text)
                .font(.caption2.weight(.semibold))
                .foregroundStyle(.secondary)
                .padding(.vertical, 14)
                .frame(maxWidth: .infinity)
        case .attribution(let line):
            HStack(spacing: 6) {
                AvatarView(look: look(line.desk), attention: .quiet, isDimmed: false, localAvatarURL: nil, size: 16)
                Text(line.text).font(.caption).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity)
            .padding(.top, 10).padding(.bottom, 4)
        case .rollup(let rollup):
            Button { model.toggle(rollup: rollup.id) } label: {
                HStack(spacing: 6) {
                    ForEach(rollup.faces.prefix(3), id: \.self) { face in
                        AvatarView(look: look(face), attention: .quiet, isDimmed: false, localAvatarURL: nil, size: 16)
                    }
                    Text(rollup.text).font(.caption)
                    Image(systemName: model.expandedRollups.contains(rollup.id) ? "chevron.up" : "chevron.down")
                        .font(.caption2)
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 12).padding(.vertical, 7)
                .background(PhoneTheme.agentBubble, in: Capsule())
            }
            .buttonStyle(.plain)
            .frame(maxWidth: .infinity)
            .padding(.vertical, 6)
        case .entry(let entry):
            switch entry {
            case .said(let row):
                if row.message.role == .system {
                    caption(SystemCaption.make(row.message))
                } else if let decision = row.message.decision {
                    VStack(alignment: .leading, spacing: 6) {
                        if !row.message.text.isEmpty, row.message.text != decision.prompt {
                            Bubble(message: row.message, openQuote: { model.jump(to: $0) })
                        }
                        DecisionCard(decision: decision) { value in
                            Task { await model.answer(decision, value: value) }
                        }
                    }
                    .padding(.vertical, 4)
                    .swipeToReply(row.message, reply: canReply ? { model.beginReply($0) } : nil)
                } else {
                    Bubble(message: row.message, openQuote: { model.jump(to: $0) })
                        .swipeToReply(row.message, reply: canReply ? { model.beginReply($0) } : nil)
                }
            case .toolCall:
                EmptyView()
            }
        case .turnFace(let face):
            HStack {
                AvatarView(look: look(face.desk), attention: .quiet, isDimmed: false, localAvatarURL: nil, size: 22)
                Spacer()
            }
            .padding(.leading, 4)
            .padding(.top, 2)
            .padding(.bottom, 8)
        case .caption(let c):
            caption(c)
        case .newDivider:
            HStack(spacing: 8) {
                Rectangle().fill(PhoneTheme.waiting.opacity(0.6)).frame(height: 1)
                Text("NEW").font(.caption2.weight(.bold)).foregroundStyle(PhoneTheme.waiting)
                Rectangle().fill(PhoneTheme.waiting.opacity(0.6)).frame(height: 1)
            }
            .padding(.vertical, 8)
        }
    }

    private func look(_ desk: String) -> AvatarLook {
        agents[desk]?.look ?? AvatarLook.forName(desk)
    }

    private var canReply: Bool { model.canSend }

    private func caption(_ c: SystemCaption) -> some View {
        Text(c.text)
            .font(.caption)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.center)
            .lineLimit(3)
            .frame(maxWidth: .infinity)
            .padding(.horizontal, 24)
            .padding(.vertical, 8)
    }
}

struct Bubble: View {
    let message: Message
    /// A tap on the quote this message carries, when it is a reply.
    var openQuote: ((MessageQuote) -> Void)?
    @Environment(\.phoneVoice) private var voice

    var body: some View {
        HStack {
            if message.isFromUser { Spacer(minLength: 56) }
            VStack(alignment: .leading, spacing: 8) {
                if let quote = message.replyTo {
                    PhoneQuoteBlock(quote: quote, open: openQuote)
                }
                if let note = message.voiceNote, let voice {
                    VoiceNotePlayRow(player: voice.player, note: note)
                }
                let images = AttachmentLines.sentImages(message)
                if !images.isEmpty { SentImagesView(images: images) }
                // A video, audio clip, PDF or other file the deck holds: a
                // player or a chip that opens it (`HeldAttachmentViews`,
                // shared with the Mac).
                ForEach(AttachmentLines.held(message), id: \.value) { HeldAttachmentView(attachment: $0) }
                let words = AttachmentLines.visibleText(message)
                if !words.isEmpty {
                    Text(Self.markdown(words))
                        .font(.body)
                        .textSelection(.enabled)
                }
                if let voice, ListenPlayer.canListen(message) {
                    ListenButton(player: voice.listen, message: message, compact: true)
                }
            }
                .contextMenu {
                    if let voice { ListenMenuItem(player: voice.listen, message: message) }
                }
                .padding(.horizontal, 14)
                .padding(.vertical, 10)
                .background(message.isFromUser ? PhoneTheme.ownerBubble : PhoneTheme.agentBubble,
                            in: RoundedRectangle(cornerRadius: PhoneTheme.bubbleRadius, style: .continuous))
                .overlay(alignment: .bottomTrailing) {
                    if message.channel == .voice {
                        Image(systemName: "waveform").font(.caption2).foregroundStyle(.secondary).padding(6)
                    }
                }
            if !message.isFromUser { Spacer(minLength: 56) }
        }
        .padding(.vertical, 2)
        .accessibilityLabel("\(message.displayName): \(message.text)")
    }

    static func markdown(_ text: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        let parsed = (try? AttributedString(markdown: text, options: options)) ?? AttributedString(text)
        // Pasted URLs, emails and bare domains become tappable links.
        return MarkdownBody.linked(parsed)
    }
}

/// **A question the desk asked him, with the answers as buttons.**
struct DecisionCard: View {
    let decision: Decision
    let answer: (String) -> Void
    @State private var custom = ""

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 6) {
                Image(systemName: "questionmark.bubble.fill")
                    .foregroundStyle(decision.state == .open ? PhoneTheme.waiting : .secondary)
                Text(decision.state == .open ? "DECISION" : "DECIDED")
                    .font(.caption2.weight(.bold)).tracking(0.6)
                    .foregroundStyle(.secondary)
            }
            Text(Bubble.markdown(decision.prompt)).font(.body.weight(.semibold))
            if let help = decision.help, !help.isEmpty {
                Text(help).font(.subheadline).foregroundStyle(.secondary)
            }
            if decision.offersChoices {
                VStack(spacing: 8) {
                    ForEach(decision.options, id: \.value) { option in
                        Button { Haptics.tap(); answer(option.value) } label: {
                            Text(option.label)
                                .font(.body.weight(.semibold))
                                .frame(maxWidth: .infinity)
                                .padding(.vertical, 12)
                                .foregroundStyle(foreground(option.style))
                                .background(background(option.style),
                                            in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                                .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous)
                                    .strokeBorder(option.style == .primary ? .clear : PhoneTheme.cardStroke, lineWidth: 1))
                        }
                        .buttonStyle(.plain)
                    }
                    if decision.allowCustom {
                        HStack {
                            TextField("Something else…", text: $custom)
                                .padding(.horizontal, 12).padding(.vertical, 10)
                                .background(PhoneTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                            Button("Send") { answer(custom) }
                                .disabled(custom.trimmingCharacters(in: .whitespaces).isEmpty)
                        }
                    }
                }
            }
            if let status = decision.statusLine {
                Text(status).font(.footnote.weight(.medium)).foregroundStyle(.secondary)
            }
        }
        .padding(16)
        .card(radius: 20)
        .padding(.trailing, 32)
    }

    private func foreground(_ style: DecisionOption.Style) -> Color {
        switch style {
        case .primary: return PhoneTheme.canvas
        case .danger: return .red
        case .default: return .primary
        }
    }

    private func background(_ style: DecisionOption.Style) -> Color {
        style == .primary ? .primary : PhoneTheme.canvas.opacity(0.001)
    }
}

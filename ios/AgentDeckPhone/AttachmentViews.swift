import SwiftUI
import PhotosUI
import UIKit
import UniformTypeIdentifiers
import DeckKit

// Owner, 2026-09-30: "On the phone I can't add or paste screenshots or files."
//
// The rules are DeckKit's (`OutgoingAttachments.swift`): what is downscaled,
// what a paste is, how the tray uploads and cancels, what text the desk reads.
// This file is only the phone's hands: the [+] menu, the pickers, the paste,
// the tray above the composer, and his pictures in his bubble.

// MARK: - fetching his pictures back

private struct AttachmentFetchKey: EnvironmentKey {
    static let defaultValue: (@Sendable (String) async throws -> Data)? = nil
}

extension EnvironmentValues {
    /// `GET /v1/attachments/...` with his token. Nil in renders.
    var attachmentFetch: (@Sendable (String) async throws -> Data)? {
        get { self[AttachmentFetchKey.self] }
        set { self[AttachmentFetchKey.self] = newValue }
    }
}

/// One process-wide cache, so scrolling back up does not refetch.
@MainActor
enum SentImageCache {
    static var images: [String: UIImage] = [:]
}

// MARK: - the [+] menu

/// Where an attachment comes from.
enum AttachSource: Identifiable {
    case photos, camera
    var id: Int { self == .photos ? 0 : 1 }
}

struct AttachMenu: View {
    let pick: (AttachSource) -> Void
    let files: () -> Void

    var body: some View {
        Menu {
            Button { pick(.photos) } label: { Label("Photos", systemImage: "photo.on.rectangle") }
            if UIImagePickerController.isSourceTypeAvailable(.camera) {
                Button { pick(.camera) } label: { Label("Camera", systemImage: "camera") }
            }
            Button { files() } label: { Label("Files", systemImage: "folder") }
        } label: {
            Image(systemName: "plus")
                .font(.system(size: 17, weight: .semibold))
                .foregroundStyle(.primary)
                .frame(width: 38, height: 38)
                .background(PhoneTheme.field, in: Circle())
        }
        .accessibilityLabel("Attach a photo or file")
    }
}

// MARK: - Photos

/// PHPicker: any number of images, screenshots included, no library permission.
struct PhotoPicker: UIViewControllerRepresentable {
    let onPicked: ([PreparedAttachment]) -> Void

    func makeUIViewController(context: Context) -> PHPickerViewController {
        var config = PHPickerConfiguration()
        config.filter = .images
        config.selectionLimit = 0
        config.preferredAssetRepresentationMode = .current
        let picker = PHPickerViewController(configuration: config)
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: PHPickerViewController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(onPicked: onPicked) }

    final class Coordinator: NSObject, PHPickerViewControllerDelegate {
        let onPicked: ([PreparedAttachment]) -> Void
        init(onPicked: @escaping ([PreparedAttachment]) -> Void) { self.onPicked = onPicked }

        func picker(_ picker: PHPickerViewController, didFinishPicking results: [PHPickerResult]) {
            picker.dismiss(animated: true)
            let providers = results.map(\.itemProvider)
            Task {
                var ready: [PreparedAttachment] = []
                for (index, provider) in providers.enumerated() {
                    if let item = await AttachmentLoading.load(provider, origin: .photo, fallbackName: "photo-\(index + 1)") {
                        ready.append(item)
                    }
                }
                await MainActor.run { onPicked(ready) }
            }
        }
    }
}

// MARK: - Camera

struct CameraPicker: UIViewControllerRepresentable {
    let onPicked: ([PreparedAttachment]) -> Void
    @Environment(\.dismiss) private var dismiss

    func makeUIViewController(context: Context) -> UIImagePickerController {
        let picker = UIImagePickerController()
        picker.sourceType = .camera
        picker.delegate = context.coordinator
        return picker
    }

    func updateUIViewController(_ controller: UIImagePickerController, context: Context) {}
    func makeCoordinator() -> Coordinator { Coordinator(self) }

    final class Coordinator: NSObject, UIImagePickerControllerDelegate, UINavigationControllerDelegate {
        let parent: CameraPicker
        init(_ parent: CameraPicker) { self.parent = parent }

        func imagePickerController(_ picker: UIImagePickerController,
                                   didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let image = info[.originalImage] as? UIImage, let data = image.jpegData(compressionQuality: 0.95) {
                let stamp = Int(Date().timeIntervalSince1970)
                parent.onPicked([AttachmentPrep.prepare(data: data, filename: "camera-\(stamp).jpg",
                                                        typeIdentifier: UTType.jpeg.identifier, origin: .photo)])
            }
            parent.dismiss()
        }

        func imagePickerControllerDidCancel(_ picker: UIImagePickerController) { parent.dismiss() }
    }
}

// MARK: - the text field that pastes pictures

/// The composer's text box. A UITextView because SwiftUI's TextField only
/// ever pastes words: here a long-press → Paste with a picture or a file on
/// the clipboard attaches it (`PasteClassifier`), and words paste as words.
struct ComposerTextView: UIViewRepresentable {
    @Binding var text: String
    let placeholder: String
    let maxLines: Int
    let onPaste: ([PreparedAttachment]) -> Void
    /// A new value raises the keyboard: starting a quote-reply puts him in
    /// the box, as WhatsApp does.
    var focusToken: String?

    func makeUIView(context: Context) -> PastingTextView {
        let view = PastingTextView()
        view.delegate = context.coordinator
        view.font = .preferredFont(forTextStyle: .body)
        view.adjustsFontForContentSizeCategory = true
        view.backgroundColor = .clear
        view.textContainerInset = UIEdgeInsets(top: 10, left: 12, bottom: 10, right: 12)
        view.textContainer.lineFragmentPadding = 0
        view.isScrollEnabled = false
        view.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        view.placeholder.text = placeholder
        view.accessibilityLabel = placeholder
        view.onPasteAttachments = onPaste
        return view
    }

    func updateUIView(_ view: PastingTextView, context: Context) {
        if view.text != text { view.text = text }
        view.placeholder.text = placeholder
        view.placeholder.isHidden = !text.isEmpty
        view.onPasteAttachments = onPaste
        if focusToken != context.coordinator.focusToken {
            context.coordinator.focusToken = focusToken
            if focusToken != nil { DispatchQueue.main.async { view.becomeFirstResponder() } }
        }
    }

    func sizeThatFits(_ proposal: ProposedViewSize, uiView: PastingTextView, context: Context) -> CGSize? {
        let width = proposal.width ?? 280
        let fitting = uiView.sizeThatFits(CGSize(width: width, height: .greatestFiniteMagnitude)).height
        let line = uiView.font?.lineHeight ?? 20
        let cap = line * CGFloat(maxLines) + uiView.textContainerInset.top + uiView.textContainerInset.bottom
        let scrolls = fitting > cap
        if uiView.isScrollEnabled != scrolls { uiView.isScrollEnabled = scrolls }
        return CGSize(width: width, height: min(fitting, cap))
    }

    func makeCoordinator() -> Coordinator { Coordinator(text: $text) }

    final class Coordinator: NSObject, UITextViewDelegate {
        var text: Binding<String>
        var focusToken: String?
        init(text: Binding<String>) { self.text = text }
        func textViewDidChange(_ view: UITextView) {
            text.wrappedValue = view.text
            (view as? PastingTextView)?.placeholder.isHidden = !view.text.isEmpty
            view.invalidateIntrinsicContentSize()
        }
    }
}

final class PastingTextView: UITextView {
    let placeholder = UILabel()
    var onPasteAttachments: (([PreparedAttachment]) -> Void)?

    override init(frame: CGRect, textContainer: NSTextContainer?) {
        super.init(frame: frame, textContainer: textContainer)
        placeholder.textColor = .placeholderText
        placeholder.font = .preferredFont(forTextStyle: .body)
        placeholder.adjustsFontForContentSizeCategory = true
        placeholder.isAccessibilityElement = false
        addSubview(placeholder)
    }

    required init?(coder: NSCoder) { fatalError("not used") }

    override func layoutSubviews() {
        super.layoutSubviews()
        placeholder.frame = CGRect(x: textContainerInset.left, y: textContainerInset.top,
                                   width: bounds.width - textContainerInset.left - textContainerInset.right,
                                   height: placeholder.font.lineHeight)
    }

    override func canPerformAction(_ action: Selector, withSender sender: Any?) -> Bool {
        if action == #selector(paste(_:)) {
            let board = UIPasteboard.general
            return board.hasStrings || board.hasImages || board.hasURLs
                || PasteClassifier.classify(board.types) != .nothing
        }
        return super.canPerformAction(action, withSender: sender)
    }

    override func paste(_ sender: Any?) {
        let board = UIPasteboard.general
        switch PasteClassifier.classify(board.types) {
        case .image, .file:
            let providers = board.itemProviders
            let handler = onPasteAttachments
            Task {
                var ready: [PreparedAttachment] = []
                for (index, provider) in providers.enumerated() {
                    if let item = await AttachmentLoading.load(provider, origin: .pasted, fallbackName: "pasted-\(index + 1)") {
                        ready.append(item)
                    }
                }
                await MainActor.run { handler?(ready) }
            }
        case .text, .nothing:
            super.paste(sender)
        }
    }
}

// MARK: - the tray above the composer

struct AttachmentTrayView: View {
    @ObservedObject var tray: AttachmentTray

    var body: some View {
        if !tray.items.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(tray.items) { item in
                        TrayItemView(item: item) { Haptics.tap(); tray.remove(item.id) }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)
            }
        }
    }
}

private struct TrayItemView: View {
    let item: OutgoingAttachment
    let remove: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Group {
                if item.isImage, let image = UIImage(data: item.prepared.data) {
                    Image(uiImage: image).resizable().scaledToFill()
                        .frame(width: 64, height: 64)
                        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                } else {
                    VStack(spacing: 4) {
                        Image(systemName: "doc.fill").font(.title3)
                        Text(item.filename).font(.caption2).lineLimit(2).multilineTextAlignment(.center)
                    }
                    .padding(6)
                    .frame(width: 96, height: 64)
                    .background(PhoneTheme.field, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
                }
            }
            .overlay { status }
            Button(action: remove) {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 20))
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, .black.opacity(0.7))
            }
            .offset(x: 6, y: -6)
            .accessibilityLabel("Remove \(item.filename)")
        }
        .padding(.top, 6).padding(.trailing, 6)
    }

    @ViewBuilder
    private var status: some View {
        switch item.state {
        case .uploading(let fraction):
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.black.opacity(0.35))
                ProgressView(value: max(fraction, 0.02))
                    .progressViewStyle(.circular)
                    .tint(.white)
            }
            .accessibilityLabel("Uploading")
        case .failed(let why):
            ZStack {
                RoundedRectangle(cornerRadius: 12, style: .continuous).fill(.red.opacity(0.55))
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.white)
            }
            .accessibilityLabel("Not sent: \(why)")
        case .uploaded:
            EmptyView()
        }
    }
}

// MARK: - his pictures in his bubble

struct SentImagesView: View {
    let images: [Attachment]
    @State private var open: OpenImage?

    var body: some View {
        VStack(alignment: .trailing, spacing: 6) {
            ForEach(images, id: \.value) { attachment in
                if let url = attachment.url {
                    SentImage(url: url)
                        .onTapGesture { open = OpenImage(url: url) }
                        .accessibilityAddTraits(.isButton)
                        .accessibilityLabel("Image you sent. Double-tap to open.")
                }
            }
        }
        .fullScreenCover(item: $open) { FullScreenImage(url: $0.url) }
    }
}

struct OpenImage: Identifiable { let url: String; var id: String { url } }

private struct SentImage: View {
    let url: String
    @Environment(\.attachmentFetch) private var fetch
    @State private var image: UIImage?
    @State private var failed = false

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFit()
                    .frame(maxWidth: 220, maxHeight: 280)
                    .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
            } else {
                RoundedRectangle(cornerRadius: 12, style: .continuous)
                    .fill(PhoneTheme.field)
                    .frame(width: 160, height: 120)
                    .overlay { failed ? AnyView(Image(systemName: "photo").foregroundStyle(.secondary)) : AnyView(ProgressView()) }
            }
        }
        .task(id: url) { await load() }
    }

    private func load() async {
        if let cached = SentImageCache.images[url] { image = cached; return }
        guard let fetch else { failed = true; return }
        do {
            let data = try await fetch(url)
            if let decoded = UIImage(data: data) {
                SentImageCache.images[url] = decoded
                image = decoded
            } else { failed = true }
        } catch { failed = true }
    }
}

struct FullScreenImage: View {
    let url: String
    @Environment(\.dismiss) private var dismiss
    @State private var scale: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.black.ignoresSafeArea()
            if let image = SentImageCache.images[url] {
                Image(uiImage: image).resizable().scaledToFit()
                    .scaleEffect(scale * pinch)
                    .gesture(MagnificationGesture()
                        .updating($pinch) { value, state, _ in state = value }
                        .onEnded { scale = min(max(scale * $0, 1), 5) })
                    .onTapGesture(count: 2) { withAnimation { scale = scale > 1 ? 1 : 2.5 } }
                    .accessibilityLabel("Image you sent")
            } else {
                ProgressView().tint(.white).frame(maxWidth: .infinity, maxHeight: .infinity)
            }
            if let image = SentImageCache.images[url] {
                // Save Image and Save to Files are in the share sheet.
                ShareLink(item: Image(uiImage: image), preview: SharePreview("Image", image: Image(uiImage: image))) {
                    Image(systemName: "square.and.arrow.up")
                        .font(.system(size: 17, weight: .bold))
                        .foregroundStyle(.white)
                        .frame(width: 44, height: 44)
                        .background(.white.opacity(0.15), in: Circle())
                }
                .padding()
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                .accessibilityLabel("Share or save")
            }
            Button { dismiss() } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 17, weight: .bold))
                    .foregroundStyle(.white)
                    .frame(width: 44, height: 44)
                    .background(.white.opacity(0.15), in: Circle())
            }
            .padding()
            .accessibilityLabel("Close")
        }
    }
}

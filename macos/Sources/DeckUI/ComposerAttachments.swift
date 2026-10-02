import AppKit
import SwiftUI
import UniformTypeIdentifiers
import DeckKit

// Owner, 2026-09-30: "also on Mac I can't paste images or files."
//
// The rules are DeckKit's (`OutgoingAttachments.swift`), shared with the
// phone. This file is the Mac's hands only: what ⌘V finds on the clipboard,
// the chips above the composer, and his pictures in his bubble.

/// **What ⌘V would attach**, or nil when the clipboard holds words and the
/// text field should paste them as usual.
public enum MacPasteboardAttachments {
    public static func read(_ board: NSPasteboard = .general) -> [PreparedAttachment]? {
        let types = (board.types ?? []).map(\.rawValue)
        switch PasteClassifier.classify(types) {
        case .file:
            let urls = board.readObjects(forClasses: [NSURL.self],
                                         options: [.urlReadingFileURLsOnly: true]) as? [URL] ?? []
            let files = urls.compactMap(AttachmentLoading.file(at:))
            return files.isEmpty ? nil : files
        case .image:
            var found: [PreparedAttachment] = []
            for (index, item) in (board.pasteboardItems ?? []).enumerated() {
                guard let picked = picture(in: item) else { continue }
                found.append(AttachmentLoading.named(picked.data, type: picked.type, suggested: nil,
                                                     fallbackName: "pasted-\(index + 1)", origin: .pasted))
            }
            return found.isEmpty ? nil : found
        case .text, .nothing:
            return nil
        }
    }

    /// PNG first (a screenshot carries one), then any other picture; a TIFF
    /// alone is turned into a PNG, since a desk reads PNG and not TIFF.
    private static func picture(in item: NSPasteboardItem) -> (data: Data, type: String)? {
        if let data = item.data(forType: .png) { return (data, UTType.png.identifier) }
        for raw in item.types.map(\.rawValue) where raw != NSPasteboard.PasteboardType.tiff.rawValue {
            if UTType(raw)?.conforms(to: .image) == true, let data = item.data(forType: .init(raw)) {
                return (data, raw)
            }
        }
        if let tiff = item.data(forType: .tiff), let rep = NSBitmapImageRep(data: tiff),
           let png = rep.representation(using: .png, properties: [:]) {
            return (png, UTType.png.identifier)
        }
        return nil
    }
}

/// ⌘V while the composer has focus: a picture or a file on the clipboard is
/// attached and the key is consumed; words fall through to the text field.
@MainActor
final class ComposerPasteMonitor {
    private var token: Any?

    func install(isComposing: @escaping @MainActor () -> Bool, attach: @escaping @MainActor ([PreparedAttachment]) -> Void) {
        guard token == nil else { return }
        token = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { event in
            guard event.modifierFlags.intersection(.deviceIndependentFlagsMask) == .command,
                  event.charactersIgnoringModifiers?.lowercased() == "v" else { return event }
            return MainActor.assumeIsolated {
                guard isComposing(), let found = MacPasteboardAttachments.read() else { return event }
                attach(found)
                return nil
            }
        }
    }

    func remove() {
        if let token { NSEvent.removeMonitor(token) }
        token = nil
    }
}

extension View {
    /// A picture or file dropped here is attached like a pasted one.
    func acceptsAttachmentDrops(_ attach: @escaping @MainActor ([PreparedAttachment]) -> Void) -> some View {
        onDrop(of: [.fileURL, .image], isTargeted: nil) { providers in
            Task {
                var found: [PreparedAttachment] = []
                for (index, provider) in providers.enumerated() {
                    if let item = await AttachmentLoading.load(provider, origin: .pasted,
                                                                fallbackName: "dropped-\(index + 1)") {
                        found.append(item)
                    }
                }
                await MainActor.run { attach(found) }
            }
            return true
        }
    }
}

// MARK: - the chips above the composer

struct ComposerTrayView: View {
    @ObservedObject var tray: AttachmentTray

    var body: some View {
        if !tray.items.isEmpty {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 8) {
                    ForEach(tray.items) { item in
                        ComposerTrayChip(item: item) { tray.remove(item.id) }
                    }
                }
                .padding(.horizontal, 12)
                .padding(.top, 8)
            }
        }
    }
}

private struct ComposerTrayChip: View {
    let item: OutgoingAttachment
    let remove: () -> Void

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Group {
                if item.isImage, let image = NSImage(data: item.prepared.data) {
                    Image(nsImage: image).resizable().scaledToFill()
                        .frame(width: 56, height: 56)
                        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
                } else {
                    VStack(spacing: 3) {
                        Image(systemName: "doc.fill")
                        Text(item.filename).font(.caption2).lineLimit(2).multilineTextAlignment(.center)
                    }
                    .padding(5)
                    .frame(width: 90, height: 56)
                    .background(.quaternary, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                }
            }
            .overlay { status }
            .help(item.filename)
            Button(action: remove) {
                Image(systemName: "xmark.circle.fill")
                    .font(.system(size: 16))
                    .symbolRenderingMode(.palette)
                    .foregroundStyle(.white, .black.opacity(0.7))
            }
            .buttonStyle(.plain)
            .offset(x: 5, y: -5)
            .accessibilityLabel("Remove \(item.filename)")
        }
        .padding(.top, 5).padding(.trailing, 5)
    }

    @ViewBuilder
    private var status: some View {
        switch item.state {
        case .uploading(let fraction):
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.black.opacity(0.35))
                ProgressView(value: max(fraction, 0.02)).progressViewStyle(.circular).controlSize(.small)
            }
            .accessibilityLabel("Uploading")
        case .failed(let why):
            ZStack {
                RoundedRectangle(cornerRadius: 10, style: .continuous).fill(.red.opacity(0.55))
                Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.white)
            }
            .help(why)
            .accessibilityLabel("Not sent: \(why)")
        case .uploaded:
            EmptyView()
        }
    }
}

// MARK: - his pictures in his bubble

private struct AttachmentFetchKey: EnvironmentKey {
    static let defaultValue: (@Sendable (String) async throws -> Data)? = nil
}

extension EnvironmentValues {
    /// `GET /v1/attachments/...` with his token. Nil in previews and tests.
    var attachmentFetch: (@Sendable (String) async throws -> Data)? {
        get { self[AttachmentFetchKey.self] }
        set { self[AttachmentFetchKey.self] = newValue }
    }
}

@MainActor
private enum SentImageCache {
    static var images: [String: NSImage] = [:]
}

/// A picture he sent, fetched from the deck. A click opens it full size.
struct SentImage: View {
    let url: String
    @Environment(\.attachmentFetch) private var fetch
    @State private var image: NSImage?
    @State private var failed = false
    @State private var open = false

    var body: some View {
        Group {
            if let image {
                Image(nsImage: image).resizable().scaledToFit()
                    .frame(maxWidth: 320, maxHeight: 320)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
                    .onTapGesture { open = true }
                    .sheet(isPresented: $open) { FullSizeImage(image: image) }
            } else {
                RoundedRectangle(cornerRadius: 8).fill(.quaternary)
                    .frame(width: 160, height: 120)
                    .overlay { if failed { Image(systemName: "photo").foregroundStyle(.secondary) } }
            }
        }
        .accessibilityLabel("Image you sent")
        .task(id: url) {
            if let cached = SentImageCache.images[url] { image = cached; return }
            guard let fetch, let data = try? await fetch(url), let decoded = NSImage(data: data) else {
                failed = true
                return
            }
            SentImageCache.images[url] = decoded
            image = decoded
        }
    }
}

/// Full size, with zoom (pinch, or the buttons / ⌘+ ⌘-) and Save / Share:
/// a picture a desk sent him is one he may want to keep.
private struct FullSizeImage: View {
    let image: NSImage
    @Environment(\.dismiss) private var dismiss
    @State private var zoom: CGFloat = 1
    @GestureState private var pinch: CGFloat = 1

    private var scale: CGFloat { min(max(zoom * pinch, 0.25), 8) }

    var body: some View {
        VStack(spacing: 0) {
            ScrollView([.horizontal, .vertical]) {
                Image(nsImage: image)
                    .resizable()
                    .frame(width: image.size.width * scale, height: image.size.height * scale)
            }
            .gesture(MagnifyGesture()
                .updating($pinch) { value, state, _ in state = value.magnification }
                .onEnded { zoom = min(max(zoom * $0.magnification, 0.25), 8) })
            .frame(minWidth: 480, idealWidth: min(image.size.width, 1200),
                   minHeight: 360, idealHeight: min(image.size.height, 800))
            HStack {
                Button { zoom = max(zoom / 1.5, 0.25) } label: { Image(systemName: "minus.magnifyingglass") }
                    .keyboardShortcut("-", modifiers: .command).accessibilityLabel("Zoom out")
                Button { zoom = min(zoom * 1.5, 8) } label: { Image(systemName: "plus.magnifyingglass") }
                    .keyboardShortcut("=", modifiers: .command).accessibilityLabel("Zoom in")
                Button("Actual Size") { zoom = 1 }
                Spacer()
                ShareLink(item: Image(nsImage: image), preview: SharePreview("Image", image: Image(nsImage: image)))
                Button("Save…") { save() }
                Button("Close") { dismiss() }.keyboardShortcut(.cancelAction)
            }
            .padding(10)
        }
    }

    private func save() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "image.png"
        guard panel.runModal() == .OK, let target = panel.url,
              let tiff = image.tiffRepresentation, let rep = NSBitmapImageRep(data: tiff),
              let png = rep.representation(using: .png, properties: [:]) else { return }
        try? png.write(to: target)
    }
}

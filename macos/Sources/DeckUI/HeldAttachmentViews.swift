import SwiftUI
import AVKit
import QuickLook
import DeckKit
#if os(macOS)
import AppKit
#else
import UIKit
#endif

// Owner, 2026-10-01: "can the chat send me videos or image files and I will
// see it there?"
//
// A file the deck holds -- one a desk sent with `mcp__deck__send_file`, or one
// he sent himself -- drawn so he can SEE it: a video is a poster with a play
// button that plays in place (and full screen from the player's controls), an
// audio clip is a player row, a PDF or any other file is a chip that opens
// QuickLook and can be saved or shared. Pictures keep their own view
// (`SentImage`). Compiled into the Mac app AND the phone (ios/project.yml), so
// the two cannot draw a file differently. Nothing plays until he taps.

private struct MediaLoaderKey: EnvironmentKey {
    static let defaultValue: DeckMediaLoader? = nil
}

extension EnvironmentValues {
    /// Streams a held video or audio clip from the deck with his token. Nil
    /// in renders and tests: the tile still draws, it just cannot play.
    public var mediaLoader: DeckMediaLoader? {
        get { self[MediaLoaderKey.self] }
        set { self[MediaLoaderKey.self] = newValue }
    }
}

#if os(macOS)
typealias HeldPlatformImage = NSImage
private func heldImage(_ image: NSImage) -> Image { Image(nsImage: image) }
#else
typealias HeldPlatformImage = UIImage
private func heldImage(_ image: UIImage) -> Image { Image(uiImage: image) }
#endif

@MainActor
private enum PosterCache {
    static var images: [String: HeldPlatformImage] = [:]
}

struct HeldAttachmentView: View {
    enum Style: Equatable { case video, audio, document }

    let attachment: Attachment

    static func style(of attachment: Attachment) -> Style {
        switch attachment.media {
        case .video: return .video
        case .audio: return .audio
        default: return .document
        }
    }

    var body: some View {
        switch Self.style(of: attachment) {
        case .video: HeldVideoTile(attachment: attachment)
        case .audio: HeldAudioRow(attachment: attachment)
        case .document: HeldFileChip(attachment: attachment)
        }
    }
}

// MARK: - video

private struct HeldVideoTile: View {
    let attachment: Attachment
    @Environment(\.mediaLoader) private var loader
    @Environment(\.attachmentFetch) private var fetch
    @State private var player: AVPlayer?
    @State private var poster: HeldPlatformImage?

    private let width: CGFloat = 300

    private var height: CGFloat {
        guard let poster, poster.size.width > 0 else { return width * 9 / 16 }
        return min(width * 1.4, max(width * 9 / 16, width * poster.size.height / poster.size.width))
    }

    var body: some View {
        HeldVideoStage(player: player, poster: poster, label: attachment.displayName,
                       caption: [attachment.displayName, attachment.sizeLabel].compactMap { $0 }.joined(separator: " · "),
                       canPlay: loader != nil, play: play)
        .frame(width: width, height: height)
        .clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
        .task(id: attachment.previewURL) { await loadPoster() }
    }

    private func play() {
        guard let loader, let url = attachment.url else { return }
        let made = AVPlayer(playerItem: AVPlayerItem(asset: loader.asset(for: url)))
        player = made
        made.play()  // he tapped play: never before
    }

    private func loadPoster() async {
        guard let url = attachment.previewURL else { return }
        if let cached = PosterCache.images[url] { poster = cached; return }
        guard let fetch, let data = try? await fetch(url), let image = HeldPlatformImage(data: data) else { return }
        PosterCache.images[url] = image
        poster = image
    }
}

/// The tile's two faces: the poster with its play button, and -- once he taps
/// -- the player. Split out so a test can drive the exact swap that crashed.
///
/// MEASURED 2026-10-01: SwiftUI's `VideoPlayer` aborted the Mac app the moment
/// he tapped play ("failed to demangle superclass of VideoPlayerView from
/// mangled name 'So12AVPlayerViewC'"). The SwiftPM binary linked
/// `_AVKit_SwiftUI` but not AVKit itself, so `AVPlayerView` -- the superclass
/// of SwiftUI's player -- did not exist at runtime. The player is now AVKit's
/// own view, named directly, so the framework is always linked.
struct HeldVideoStage: View {
    let player: AVPlayer?
    let poster: HeldPlatformImage?
    let label: String
    let caption: String
    let canPlay: Bool
    let play: () -> Void

    var body: some View {
        if let player {
            PlatformVideoPlayer(player: player)
                .onDisappear { player.pause() }
                .accessibilityLabel("Video \(label)")
        } else {
            Button(action: play) {
                ZStack {
                    if let poster {
                        heldImage(poster).resizable().scaledToFill()
                    } else {
                        Rectangle().fill(Color.black.opacity(0.85))
                    }
                    Image(systemName: "play.circle.fill")
                        .font(.system(size: 52))
                        .symbolRenderingMode(.palette)
                        .foregroundStyle(.white, .black.opacity(0.45))
                }
                .overlay(alignment: .bottomLeading) {
                    Text(caption)
                        .font(.caption2)
                        .lineLimit(1)
                        .foregroundStyle(.white)
                        .padding(.horizontal, 8).padding(.vertical, 4)
                        .background(.black.opacity(0.5), in: Capsule())
                        .padding(8)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(!canPlay)
            .accessibilityLabel("Video \(label). Play.")
        }
    }
}

#if os(macOS)
/// AVKit's `AVPlayerView`, not SwiftUI's `VideoPlayer` (see `HeldVideoStage`).
struct PlatformVideoPlayer: NSViewRepresentable {
    let player: AVPlayer

    func makeNSView(context: Context) -> AVPlayerView {
        let view = AVPlayerView()
        view.controlsStyle = .inline
        view.showsFullScreenToggleButton = true
        view.videoGravity = .resizeAspect
        view.player = player
        return view
    }

    func updateNSView(_ view: AVPlayerView, context: Context) {
        if view.player !== player { view.player = player }
    }

    static func dismantleNSView(_ view: AVPlayerView, coordinator: ()) {
        view.player?.pause()
        view.player = nil
    }
}
#else
/// AVKit's `AVPlayerViewController`, not SwiftUI's `VideoPlayer` (see `HeldVideoStage`).
struct PlatformVideoPlayer: UIViewControllerRepresentable {
    let player: AVPlayer

    func makeUIViewController(context: Context) -> AVPlayerViewController {
        let controller = AVPlayerViewController()
        controller.showsPlaybackControls = true
        controller.videoGravity = .resizeAspect
        controller.player = player
        return controller
    }

    func updateUIViewController(_ controller: AVPlayerViewController, context: Context) {
        if controller.player !== player { controller.player = player }
    }

    static func dismantleUIViewController(_ controller: AVPlayerViewController, coordinator: ()) {
        controller.player?.pause()
        controller.player = nil
    }
}
#endif

// MARK: - audio

@MainActor
private final class AudioClip: ObservableObject {
    @Published var playing = false
    @Published var progress: Double = 0
    private var player: AVPlayer?
    private var observer: Any?

    func toggle(_ attachment: Attachment, loader: DeckMediaLoader?) {
        if player == nil {
            guard let loader, let url = attachment.url else { return }
            let made = AVPlayer(playerItem: AVPlayerItem(asset: loader.asset(for: url)))
            observer = made.addPeriodicTimeObserver(forInterval: CMTime(seconds: 0.25, preferredTimescale: 600),
                                                    queue: .main) { [weak self, weak made] time in
                guard let made, let duration = made.currentItem?.duration.seconds,
                      duration.isFinite, duration > 0 else { return }
                MainActor.assumeIsolated {
                    self?.progress = time.seconds / duration
                    if time.seconds >= duration - 0.05 { self?.playing = false }
                }
            }
            player = made
        }
        guard let player else { return }
        if playing {
            player.pause()
        } else {
            if progress >= 0.99 { player.seek(to: .zero); progress = 0 }
            player.play()
        }
        playing.toggle()
    }

    func stop() { player?.pause(); playing = false }
}

private struct HeldAudioRow: View {
    let attachment: Attachment
    @Environment(\.mediaLoader) private var loader
    @StateObject private var clip = AudioClip()

    var body: some View {
        HStack(spacing: 10) {
            Button { clip.toggle(attachment, loader: loader) } label: {
                Image(systemName: clip.playing ? "pause.circle.fill" : "play.circle.fill")
                    .font(.system(size: 30))
            }
            .buttonStyle(.plain)
            .disabled(loader == nil)
            .accessibilityLabel(clip.playing ? "Pause \(attachment.displayName)" : "Play \(attachment.displayName)")
            VStack(alignment: .leading, spacing: 4) {
                Text(attachment.displayName).font(.callout).lineLimit(1)
                ProgressView(value: clip.progress).frame(width: 160)
            }
            if let size = attachment.sizeLabel {
                Text(size).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .onDisappear { clip.stop() }
    }
}

// MARK: - a PDF or any other file

private struct HeldFileChip: View {
    let attachment: Attachment
    @Environment(\.attachmentFetch) private var fetch
    @State private var local: URL?
    @State private var preview: URL?
    @State private var loading = false
    @State private var failed = false

    var body: some View {
        HStack(spacing: 8) {
            Button(action: open) {
                HStack(spacing: 8) {
                    Image(systemName: attachment.media == .pdf ? "doc.richtext.fill" : "doc.fill")
                        .font(.title3)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(attachment.displayName).font(.callout).lineLimit(1)
                        Text(failed ? "Could not open" : (attachment.sizeLabel ?? "Open"))
                            .font(.caption).foregroundStyle(failed ? Color.red : Color.secondary)
                    }
                    if loading { ProgressView().controlSize(.small) }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .disabled(fetch == nil || loading)
            .accessibilityLabel("File \(attachment.displayName). Open.")
            if let local {
                ShareLink(item: local) { Image(systemName: "square.and.arrow.up") }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Share \(attachment.displayName)")
                #if os(macOS)
                Button { save(local) } label: { Image(systemName: "square.and.arrow.down") }
                    .buttonStyle(.plain)
                    .accessibilityLabel("Save \(attachment.displayName)")
                #endif
            }
        }
        .padding(.horizontal, 10).padding(.vertical, 8)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .quickLookPreview($preview)
    }

    private func open() {
        if let local { preview = local; return }
        guard let fetch else { return }
        loading = true
        failed = false
        Task {
            defer { loading = false }
            do {
                let file = try await AttachmentFiles().local(attachment, fetch: fetch)
                local = file
                preview = file
            } catch {
                failed = true
            }
        }
    }

    #if os(macOS)
    private func save(_ file: URL) {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = attachment.displayName
        guard panel.runModal() == .OK, let target = panel.url else { return }
        try? FileManager.default.removeItem(at: target)
        try? FileManager.default.copyItem(at: file, to: target)
    }
    #endif
}

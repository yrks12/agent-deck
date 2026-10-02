import Foundation

// Owner, 2026-10-01: "can the chat send me videos or image files and I will
// see it there?"
//
// A desk sends him a file with `mcp__deck__send_file`; the deck holds the
// bytes under `/v1/attachments/{id}/{name}` and answers byte ranges
// (`server/uploads.py`). This file is the shared half both apps draw from:
// what a held attachment is, how a video streams through the deck with his
// token, and where a file lands for QuickLook, Save and Share.

extension Attachment {
    /// What to draw: the deck's word, else judged by the name.
    public var media: Media {
        if let mediaName, let known = Media(rawValue: mediaName) { return known }
        if kind == .image { return .image }
        switch (displayName as NSString).pathExtension.lowercased() {
        case "png", "jpg", "jpeg", "gif", "webp", "heic": return .image
        case "mp4", "m4v", "mov", "webm": return .video
        case "mp3", "m4a", "aac", "wav", "ogg", "flac": return .audio
        case "pdf": return .pdf
        default: return .file
        }
    }

    /// The file's own name.
    public var displayName: String {
        if let name, !name.isEmpty { return name }
        return (value as NSString).lastPathComponent
    }

    /// "12 MB", "1.5 MB", "2 KB": what he sees before he taps.
    public var sizeLabel: String? {
        guard let bytes else { return nil }
        if bytes < 1024 { return "\(bytes) bytes" }
        let units = ["KB", "MB", "GB"]
        var value = Double(bytes) / 1024
        var unit = 0
        while value >= 1024, unit < units.count - 1 { value /= 1024; unit += 1 }
        let rounded = (value * 10).rounded() / 10
        let text = rounded == rounded.rounded() ? String(Int(rounded)) : String(format: "%.1f", rounded)
        return "\(text) \(units[unit])"
    }
}

extension AttachmentLines {
    /// Held files drawn as players and chips (not pictures), in order.
    public static func held(_ message: Message) -> [Attachment] {
        message.attachments.filter { $0.url != nil && $0.kind != .link && $0.media != .image }
    }
}

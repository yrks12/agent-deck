import SwiftUI
import DeckKit

/// A desk's face, drawn the way the sidebar draws it, for the Mac views: the
/// same character for the same name on every screen, so "Atlas" is one thing.
struct MacDeskFace: View {
    let desk: String
    var size: CGFloat = 32

    var body: some View {
        AvatarView(look: AvatarLook.forName(desk), attention: .quiet, isDimmed: false,
                   localAvatarURL: nil, size: size)
            .frame(width: size, height: size)
            .accessibilityHidden(true)
    }
}

/// **"Atlas wants to use this Mac."** The card a desk's first request raises
/// in `Ask me` mode. It is the same question the notification banner asks, in
/// the same words (`MacGrantCopy`), and it states what a yes means before it is
/// pressed: it runs commands as him and changes files in the named folders.
///
/// Dumb on purpose: it draws one `MacGrantRequest` and reports one
/// `MacGrantDecision`. Who applies it (`MacPolicy.apply`) is the bridge's job.
public struct MacGrantCardView: View {
    let request: MacGrantRequest
    var onDecide: (MacGrantDecision) -> Void
    /// "Change…" / "Choose folder…". The bridge owns the folder list.
    var onChooseFolder: () -> Void

    public init(request: MacGrantRequest, onDecide: @escaping (MacGrantDecision) -> Void,
                onChooseFolder: @escaping () -> Void = {}) {
        self.request = request
        self.onDecide = onDecide
        self.onChooseFolder = onChooseFolder
    }

    public var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(alignment: .center, spacing: 12) {
                MacDeskFace(desk: request.desk, size: 40)
                VStack(alignment: .leading, spacing: 2) {
                    Text(MacGrantCopy.title(desk: request.desk))
                        .font(.headline)
                    Text("It is asking for the first time.")
                        .font(.subheadline)
                        .foregroundStyle(.secondary)
                }
                Spacer(minLength: 0)
            }

            VStack(alignment: .leading, spacing: 4) {
                Text("First request")
                    .font(.caption.weight(.medium))
                    .foregroundStyle(.secondary)
                Text(MacGrantCopy.firstRequest(request.summary))
                    .font(.system(.callout, design: .monospaced))
                    .lineLimit(4)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(10)
                    .background(RoundedRectangle(cornerRadius: 8, style: .continuous)
                        .fill(Color.primary.opacity(0.06)))
            }

            VStack(alignment: .leading, spacing: 6) {
                Text(MacGrantCopy.scope(folders: request.folders))
                    .font(.callout)
                    .fixedSize(horizontal: false, vertical: true)
                Button(request.folders.isEmpty ? "Choose folder…" : "Change…", action: onChooseFolder)
                    .buttonStyle(.link)
                if request.folders.isEmpty {
                    Text("Suggested: \(MacGrantCopy.suggestedFolder)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            if request.unconfined {
                FlatLabel(MacGrantCopy.unconfinedWarning, systemImage: "exclamationmark.triangle.fill")
                    .font(.callout)
                    .foregroundStyle(AttentionPalette.waitingText.swiftUIColor)
                    .fixedSize(horizontal: false, vertical: true)
            }

            ViewThatFits(in: .horizontal) {
                HStack(spacing: 8) { buttons }
                VStack(spacing: 8) { buttons }
            }

            Text("Deny holds for 24 hours, then it can ask again.")
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(18)
        .frame(maxWidth: 460, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .fill(.regularMaterial))
        .overlay(RoundedRectangle(cornerRadius: 16, style: .continuous)
            .strokeBorder(Color.primary.opacity(0.10), lineWidth: 1))
        .accessibilityElement(children: .contain)
    }

    @ViewBuilder private var buttons: some View {
        Button(MacGrantCopy.allowHour) { onDecide(.hour) }
            .buttonStyle(.borderedProminent)
            .keyboardShortcut(.defaultAction)
        Button(MacGrantCopy.alwaysAllow) { onDecide(.always) }
            .buttonStyle(.bordered)
        Button(MacGrantCopy.deny) { onDecide(.deny) }
            .buttonStyle(.bordered)
            .tint(.red)
    }
}

extension NSColor {
    var swiftUIColor: Color { Color(nsColor: self) }
}

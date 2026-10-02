import SwiftUI
import DeckKit

/// **The Money screen** (`GET /v1/money`): what the companies earn and spend.
///
/// One file for both apps (compiled into DeckUI for the Mac and into the iPhone
/// target via `ios/project.yml`). Every decision — wording, order, what is
/// flagged, which source needs connecting — is `MoneyPresentation`'s; this only
/// draws it.
@MainActor
public final class MoneyModel: ObservableObject {
    public enum Phase: Equatable { case loading, unavailable, failed(String), loaded(MoneyReport) }

    let source: MoneySource
    @Published public private(set) var phase: Phase = .loading
    @Published public private(set) var isRefreshing = false

    public init(source: MoneySource) { self.source = source }

    /// `refresh` is the Refresh button: the deck re-reads Stripe and the cloud
    /// bill now. A failed refresh keeps what is on screen and says why.
    public func load(refresh: Bool = false) async {
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            if let report = try await source.money(refresh: refresh) { phase = .loaded(report) } else { phase = .unavailable }
        } catch {
            if case .loaded = phase { return }
            phase = .failed((error as? DeckError)?.userFacingText ?? "Could not read the money numbers.")
        }
    }
}

public struct MoneyView: View {
    @StateObject private var model: MoneyModel
    let onClose: (() -> Void)?

    public init(source: MoneySource, onClose: (() -> Void)? = nil) {
        _model = StateObject(wrappedValue: MoneyModel(source: source))
        self.onClose = onClose
    }

    public var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            ScrollView {
                VStack(alignment: .leading, spacing: 14) { content }
                    .padding(16)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .background(DeckPalette.canvas)
        .task { await model.load() }
    }

    private var header: some View {
        HStack(spacing: 10) {
            Text("Money").font(.title3.weight(.semibold)).accessibilityAddTraits(.isHeader)
            Spacer(minLength: 0)
            if model.isRefreshing { ProgressView().controlSize(.small) }
            Button("Refresh") { Task { await model.load(refresh: true) } }
                .disabled(model.isRefreshing)
            if let onClose { Button("Done", action: onClose).keyboardShortcut(.cancelAction) }
        }
        .padding(.horizontal, 16).padding(.vertical, 12)
    }

    @ViewBuilder private var content: some View {
        switch model.phase {
        case .loading:
            ProgressView().frame(maxWidth: .infinity).padding(.top, 40)
        case .unavailable:
            note(MoneyPresentation.unavailable)
        case .failed(let message):
            note(message)
        case .loaded(let report):
            if report.isWarming {
                VStack(alignment: .leading, spacing: 4) {
                    Text(MoneyPresentation.warmingTitle).font(.headline)
                    Text(MoneyPresentation.warmingDetail).font(.subheadline).foregroundStyle(.secondary)
                }
                .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
            } else {
                report_(report)
            }
        }
    }

    private func note(_ text: String) -> some View {
        Text(text).font(.subheadline).foregroundStyle(.secondary)
            .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
    }

    @ViewBuilder private func report_(_ r: MoneyReport) -> some View {
        if let t = r.totals {
            HStack(spacing: 10) {
                total("Money in", t.revenue, r.currency)
                total("Money out", t.costs, r.currency)
                total("Net", t.net, r.currency, signed: true)
            }
            Text("Since the start").font(.caption).foregroundStyle(.secondary)
        }
        ForEach(MoneyPresentation.connectCards(r)) { connectCard($0) }
        ForEach(MoneyPresentation.companies(r)) { companyCard($0, r.currency) }
        let flagged = MoneyPresentation.flagged(r)
        if !flagged.isEmpty {
            VStack(alignment: .leading, spacing: 8) {
                Text(MoneyPresentation.flaggedHeading(r)).font(.headline)
                ForEach(flagged) { e in
                    HStack {
                        Text(e.desk).font(.callout.weight(.medium))
                        if let c = e.company { Text(c).font(.callout).foregroundStyle(.secondary) }
                        Spacer(minLength: 0)
                        Text("\(e.days) days").font(.callout).monospacedDigit().foregroundStyle(.secondary)
                    }
                    .accessibilityElement(children: .combine)
                }
            }
            .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
        }
        VStack(alignment: .leading, spacing: 2) {
            ForEach(MoneyPresentation.footnotes(r), id: \.self) { Text($0) }
        }
        .font(.caption).foregroundStyle(.secondary)
    }

    private func total(_ title: String, _ value: Double, _ currency: String, signed: Bool = false) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(title).font(.caption).foregroundStyle(.secondary)
            Text(MoneyPresentation.amount(value, currency: currency))
                .font(.title3.weight(.semibold)).monospacedDigit().minimumScaleFactor(0.6).lineLimit(1)
                .foregroundStyle(signed ? tint(MoneyPresentation.roiTone(value)) : Color.primary)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12).card()
        .accessibilityElement(children: .combine)
    }

    private func companyCard(_ c: MoneyReport.Company, _ currency: String) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text(c.name).font(.headline)
                Spacer(minLength: 0)
                Text("ROI " + MoneyPresentation.roi(c.roi)).font(.caption.weight(.semibold))
                    .padding(.horizontal, 8).padding(.vertical, 2)
                    .background(tint(MoneyPresentation.roiTone(c.roi)).opacity(0.18), in: Capsule())
                    .foregroundStyle(tint(MoneyPresentation.roiTone(c.roi)))
            }
            HStack {
                figure("In", c.revenue, currency)
                figure("Out", c.costTotal, currency)
                figure("Net", c.net, currency)
            }
            if let work = MoneyPresentation.claudeWork(c, currency: currency) {
                Text(work).font(.caption).foregroundStyle(.secondary)
            }
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
        .opacity(c.overhead ? 0.6 : 1)
        .accessibilityElement(children: .combine)
    }

    private func figure(_ title: String, _ value: Double, _ currency: String) -> some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title).font(.caption2).foregroundStyle(.secondary)
            Text(MoneyPresentation.amount(value, currency: currency)).font(.callout.weight(.medium)).monospacedDigit()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func connectCard(_ c: MoneyReport.ConnectCard) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            FlatLabel(c.title, systemImage: "link.badge.plus").font(.headline)
            Text(c.detail).font(.subheadline).foregroundStyle(.secondary)
        }
        .padding(14).frame(maxWidth: .infinity, alignment: .leading).card()
        .accessibilityElement(children: .combine)
    }

    private func tint(_ tone: MoneyPresentation.Tone) -> Color {
        switch tone {
        case .good: return .green
        case .bad: return .red
        case .none: return .secondary
        }
    }
}

import Foundation

/// **`GET /v1/money` — what the companies earn and spend.**
///
/// Like the usage meter the route may be absent on an older deck: the client
/// answers `nil` on a 404 and the Money screen says so. Every key but `state`
/// is optional on the wire (a warming body carries almost nothing), so each is
/// decoded leniently; a missing key is a state, not a decode failure.
public protocol MoneySource: Sendable {
    /// `refresh` asks the deck to re-read Stripe and the cloud bill now
    /// (`?refresh=1`) instead of answering from its cache.
    func money(refresh: Bool) async throws -> MoneyReport?
}

public struct MoneyReport: Equatable, Sendable, Decodable {
    public enum State: String, Equatable, Sendable { case ready, warming }

    public struct Totals: Equatable, Sendable, Decodable {
        public var revenue: Double
        public var costs: Double
        public var net: Double
        public var mrr: Double

        public init(revenue: Double = 0, costs: Double = 0, net: Double = 0, mrr: Double = 0) {
            self.revenue = revenue; self.costs = costs; self.net = net; self.mrr = mrr
        }

        enum CodingKeys: String, CodingKey { case revenue, costs, net, mrr }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            revenue = (try? c.decodeIfPresent(Double.self, forKey: .revenue)) ?? 0
            costs = (try? c.decodeIfPresent(Double.self, forKey: .costs)) ?? 0
            net = (try? c.decodeIfPresent(Double.self, forKey: .net)) ?? 0
            mrr = (try? c.decodeIfPresent(Double.self, forKey: .mrr)) ?? 0
        }
    }

    public struct ClaudeWork: Equatable, Sendable, Decodable {
        public var tokens: Int
        public var apiUsd: Double

        public init(tokens: Int = 0, apiUsd: Double = 0) { self.tokens = tokens; self.apiUsd = apiUsd }

        enum CodingKeys: String, CodingKey { case tokens; case apiUsd = "api_usd" }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            tokens = (try? c.decodeIfPresent(Int.self, forKey: .tokens)) ?? 0
            apiUsd = (try? c.decodeIfPresent(Double.self, forKey: .apiUsd)) ?? 0
        }
    }

    public struct Company: Equatable, Sendable, Decodable, Identifiable {
        public var name: String
        public var revenue: Double
        public var mrr: Double
        public var desks: [String]
        public var costTotal: Double
        public var net: Double
        public var roi: Double?
        public var claude: ClaudeWork?
        /// Claude work in the report's currency (`costs.claude`); `claude.apiUsd` is the same work in dollars.
        public var claudeCost: Double
        public var overhead: Bool

        public var id: String { name }

        public init(name: String, revenue: Double = 0, mrr: Double = 0, desks: [String] = [], costTotal: Double = 0,
                    net: Double = 0, roi: Double? = nil, claude: ClaudeWork? = nil, claudeCost: Double = 0,
                    overhead: Bool = false) {
            self.claudeCost = claudeCost
            self.name = name; self.revenue = revenue; self.mrr = mrr; self.desks = desks
            self.costTotal = costTotal; self.net = net; self.roi = roi; self.claude = claude; self.overhead = overhead
        }

        enum CodingKeys: String, CodingKey {
            case name, revenue, mrr, desks, net, roi, claude, overhead, costs
            case costTotal = "cost_total"
        }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            name = (try? c.decodeIfPresent(String.self, forKey: .name)) ?? "—"
            revenue = (try? c.decodeIfPresent(Double.self, forKey: .revenue)) ?? 0
            mrr = (try? c.decodeIfPresent(Double.self, forKey: .mrr)) ?? 0
            desks = (try? c.decodeIfPresent([String].self, forKey: .desks)) ?? []
            costTotal = (try? c.decodeIfPresent(Double.self, forKey: .costTotal)) ?? 0
            net = (try? c.decodeIfPresent(Double.self, forKey: .net)) ?? 0
            roi = try? c.decodeIfPresent(Double.self, forKey: .roi)
            claude = try? c.decodeIfPresent(ClaudeWork.self, forKey: .claude)
            overhead = (try? c.decodeIfPresent(Bool.self, forKey: .overhead)) ?? false
            claudeCost = ((try? c.decodeIfPresent(Costs.self, forKey: .costs)) ?? nil)?.claude ?? 0
        }

        private struct Costs: Decodable { var claude: Double? }
    }

    public struct Experiment: Equatable, Sendable, Decodable, Identifiable {
        public var desk: String
        public var company: String?
        public var days: Int
        public var revenue: Double
        public var cost: Double
        public var roi: Double?
        public var signal: Bool
        public var flag: String?

        public var id: String { desk }

        public init(desk: String, company: String? = nil, days: Int = 0, revenue: Double = 0, cost: Double = 0,
                    roi: Double? = nil, signal: Bool = false, flag: String? = nil) {
            self.desk = desk; self.company = company; self.days = days; self.revenue = revenue
            self.cost = cost; self.roi = roi; self.signal = signal; self.flag = flag
        }

        enum CodingKeys: String, CodingKey { case desk, company, days, revenue, cost, roi, signal, flag }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            desk = (try? c.decodeIfPresent(String.self, forKey: .desk)) ?? "—"
            company = try? c.decodeIfPresent(String.self, forKey: .company)
            days = (try? c.decodeIfPresent(Int.self, forKey: .days)) ?? 0
            revenue = (try? c.decodeIfPresent(Double.self, forKey: .revenue)) ?? 0
            cost = (try? c.decodeIfPresent(Double.self, forKey: .cost)) ?? 0
            roi = try? c.decodeIfPresent(Double.self, forKey: .roi)
            signal = (try? c.decodeIfPresent(Bool.self, forKey: .signal)) ?? false
            flag = try? c.decodeIfPresent(String.self, forKey: .flag)
        }
    }

    /// A source the deck cannot read yet (or cannot any more): what to do about it.
    public struct ConnectCard: Equatable, Sendable, Decodable, Identifiable {
        public var source: String
        public var state: String
        public var title: String
        public var detail: String

        public var id: String { source }

        public init(source: String, state: String, title: String, detail: String = "") {
            self.source = source; self.state = state; self.title = title; self.detail = detail
        }

        enum CodingKeys: String, CodingKey { case source, state, title, detail }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            source = (try? c.decodeIfPresent(String.self, forKey: .source)) ?? "?"
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "missing"
            title = (try? c.decodeIfPresent(String.self, forKey: .title)) ?? "Connect \(source)"
            detail = (try? c.decodeIfPresent(String.self, forKey: .detail)) ?? ""
        }
    }

    public struct StripeAccount: Equatable, Sendable, Decodable {
        public struct Balance: Equatable, Sendable, Decodable {
            public var available: Double
            public var pending: Double
            enum CodingKeys: String, CodingKey { case available, pending }
            public init(available: Double = 0, pending: Double = 0) { self.available = available; self.pending = pending }
            public init(from decoder: Decoder) throws {
                let c = try decoder.container(keyedBy: CodingKeys.self)
                available = (try? c.decodeIfPresent(Double.self, forKey: .available)) ?? 0
                pending = (try? c.decodeIfPresent(Double.self, forKey: .pending)) ?? 0
            }
        }
        public var label: String
        public var state: String
        public var payouts30d: Double?
        public var balance: Balance?

        enum CodingKeys: String, CodingKey { case label, state, balance; case payouts30d = "payouts_30d" }
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            label = (try? c.decodeIfPresent(String.self, forKey: .label)) ?? "Stripe"
            state = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? "missing"
            payouts30d = try? c.decodeIfPresent(Double.self, forKey: .payouts30d)
            balance = try? c.decodeIfPresent(Balance.self, forKey: .balance)
        }
    }

    public var state: State
    public var currency: String
    public var generatedAt: Date?
    public var fxAssumed: Bool
    public var flagDays: Int
    public var totals: Totals?
    public var companies: [Company]
    public var experiments: [Experiment]
    public var unattributedRevenue: Double
    public var connect: [ConnectCard]
    public var stripe: [StripeAccount]

    public var isWarming: Bool { state != .ready }

    enum CodingKeys: String, CodingKey {
        case state, currency, totals, companies, experiments, unattributed, connect, stripe
        case generatedAt = "generated_at"
        case fxAssumed = "fx_assumed"
        case flagDays = "flag_days"
    }

    private struct Unattributed: Decodable { var revenue: Double? }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = (try? c.decodeIfPresent(String.self, forKey: .state)) ?? nil
        // Anything that is not "ready" is treated as still warming: no numbers drawn.
        state = raw.flatMap(State.init(rawValue:)) ?? .warming
        currency = ((try? c.decodeIfPresent(String.self, forKey: .currency)) ?? nil) ?? "GBP"
        generatedAt = WireDate.read(c, .generatedAt)
        fxAssumed = (try? c.decodeIfPresent(Bool.self, forKey: .fxAssumed)) ?? false
        flagDays = (try? c.decodeIfPresent(Int.self, forKey: .flagDays)) ?? 14
        totals = try? c.decodeIfPresent(Totals.self, forKey: .totals)
        companies = (try? c.decodeIfPresent(Lossy<Company>.self, forKey: .companies))?.elements ?? []
        experiments = (try? c.decodeIfPresent(Lossy<Experiment>.self, forKey: .experiments))?.elements ?? []
        unattributedRevenue = ((try? c.decodeIfPresent(Unattributed.self, forKey: .unattributed)) ?? nil)?.revenue ?? 0
        connect = (try? c.decodeIfPresent(Lossy<ConnectCard>.self, forKey: .connect))?.elements ?? []
        stripe = (try? c.decodeIfPresent(Lossy<StripeAccount>.self, forKey: .stripe))?.elements ?? []
    }

    public init(state: State, currency: String = "GBP", generatedAt: Date? = nil, fxAssumed: Bool = false,
                flagDays: Int = 14, totals: Totals? = nil, companies: [Company] = [],
                experiments: [Experiment] = [], unattributedRevenue: Double = 0,
                connect: [ConnectCard] = [], stripe: [StripeAccount] = []) {
        self.state = state; self.currency = currency; self.generatedAt = generatedAt; self.fxAssumed = fxAssumed
        self.flagDays = flagDays; self.totals = totals; self.companies = companies
        self.experiments = experiments; self.unattributedRevenue = unattributedRevenue
        self.connect = connect; self.stripe = stripe
    }
}

/// What the Money screen shows and in what words. DeckUI only draws this.
public enum MoneyPresentation {
    public enum Tone: Equatable, Sendable { case good, bad, none }

    public static let claudeFootnote = "Costs include Claude work at API prices"
    public static let fxFootnote = "Exchange rates approximate"

    /// "£10.00", "-£802.30", "£1,234.50".
    public static func amount(_ value: Double, currency: String) -> String {
        let symbols = ["GBP": "£", "USD": "$", "EUR": "€"]
        let symbol = symbols[currency.uppercased()] ?? "\(currency.uppercased()) "
        let magnitude = abs(value)
        let formatter = NumberFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.numberStyle = .decimal
        formatter.usesGroupingSeparator = true
        formatter.groupingSeparator = ","
        formatter.minimumFractionDigits = 2
        formatter.maximumFractionDigits = 2
        let digits = formatter.string(from: NSNumber(value: magnitude)) ?? String(format: "%.2f", magnitude)
        let negative = value < 0 && digits != "0.00"
        return (negative ? "-" : "") + symbol + digits
    }

    /// "-98%", "+400%", "0%", or "—" when the deck has no ratio yet.
    public static func roi(_ value: Double?) -> String {
        guard let value else { return "—" }
        let percent = Int((value * 100).rounded())
        return percent > 0 ? "+\(percent)%" : "\(percent)%"
    }

    public static func roiTone(_ value: Double?) -> Tone {
        guard let value else { return .none }
        return value < 0 ? .bad : (value > 0 ? .good : .none)
    }

    /// Real companies first, in the deck's order; overhead (the shared desks) last.
    public static func companies(_ report: MoneyReport) -> [MoneyReport.Company] {
        report.companies.filter { !$0.overhead } + report.companies.filter(\.overhead)
    }

    public static func flagged(_ report: MoneyReport) -> [MoneyReport.Experiment] {
        report.experiments.filter { $0.flag != nil }
    }

    public static func flaggedHeading(_ report: MoneyReport) -> String {
        "No sales after \(report.flagDays) days"
    }

    public static func connectCards(_ report: MoneyReport) -> [MoneyReport.ConnectCard] {
        report.connect.filter { $0.state != "connected" }
    }

    public static func footnotes(_ report: MoneyReport) -> [String] {
        [claudeFootnote] + (report.fxAssumed ? [fxFootnote] : [])
    }

    /// "Claude work ≈ £512.10" (the cost in the report's currency), or `nil` when there is none.
    public static func claudeWork(_ company: MoneyReport.Company, currency: String) -> String? {
        guard company.claudeCost > 0 else { return nil }
        return "Claude work ≈ " + amount(company.claudeCost, currency: currency)
    }

    public static let warmingTitle = "Warming up"
    public static let warmingDetail = "Shaliach is reading Stripe and the cloud bill. Check back in a minute."
    public static let unavailable = "This deck does not report money yet."
}

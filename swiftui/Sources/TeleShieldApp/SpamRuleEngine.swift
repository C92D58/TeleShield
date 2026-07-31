import Foundation

struct SpamRuleEngine: Sendable {
    private static let builtInPatterns = [
        "加[\\s\\-]*[LlvVXx]|[LlvVXx][\\s\\-]*信",
        "tg[\\s\\-]*@?[a-zA-Z0-9_]{3,}",
        "https?://t\\.me/",
        "@\\w{4,}",
        "兼職|刷單|日入|月入|躺賺|被動收入|在家工作|輕鬆賺",
        "投資|理財|帶單|跟單|量化|穩賺|穩健|高回報|高收益",
        "色情|A片|av|成人|裸聊|約炮|援交|包養",
        "賭|博|彩|casino|betting",
        "註冊送|免費領|紅包|禮金|優惠碼|推廣碼",
        "點贊|關注|刷粉|刷讚|漲粉",
        "售|賣|出|供應|批發|代購|代發",
        "promote|promotion|advertisement|sponsor",
        "click\\s*(here|this\\s*link|the\\s*link)",
        "earn\\s*money|work\\s*from\\s*home|passive\\s*income",
        "free\\s*crypto|free\\s*bitcoin|airdrop|giveaway",
        "limited\\s*offer|discount\\s*\\d{2,}%|buy\\s*now"
    ]

    private static let traditionalCharacterMap: [Character: Character] = [
        "赚": "賺", "钱": "錢", "资": "資", "产": "產", "优": "優", "惠": "惠",
        "注": "註", "册": "冊", "领": "領", "红": "紅", "礼": "禮", "码": "碼", "广": "廣",
        "告": "告", "轻": "輕", "松": "鬆", "职": "職", "业": "業", "赌": "賭", "博": "博",
        "联": "聯", "系": "係", "网": "網", "络": "絡", "发": "發", "现": "現",
        "关": "關", "赞": "讚", "点": "點", "击": "擊", "这": "這", "个": "個",
        "让": "讓", "你": "你", "获": "獲", "取": "取", "数": "數", "据": "據", "后": "後",
        "台": "臺", "无": "無", "吗": "嗎", "来": "來", "为": "為", "什": "什",
        "么": "麼", "还": "還", "是": "是", "将": "將", "转": "轉", "账": "帳", "号": "號"
    ]

    private let whitelist: Set<Int64>
    private let blacklist: Set<Int64>
    private let keywords: [String]
    private let regularExpressions: [NSRegularExpression]
    private let builtInExpressions: [NSRegularExpression]

    init(configuration: StoredConfiguration) {
        whitelist = Set(configuration.whitelist.keys.compactMap(Int64.init))
        blacklist = Set(configuration.blacklist.keys.compactMap(Int64.init))
        keywords = configuration.learnedPatterns.keywords
            .map { Self.normalize($0.trimmingCharacters(in: .whitespacesAndNewlines)).lowercased() }
            .filter { !$0.isEmpty }
        regularExpressions = configuration.learnedPatterns.patterns.compactMap {
            try? NSRegularExpression(pattern: $0, options: [.caseInsensitive])
        }
        builtInExpressions = Self.builtInPatterns.compactMap {
            try? NSRegularExpression(pattern: $0, options: [.caseInsensitive])
        }
    }

    func evaluate(user: NativeUser, message: NativeMessage) -> String? {
        guard !whitelist.contains(user.id) else { return nil }
        if blacklist.contains(user.id) { return "黑名單使用者" }
        let text = Self.normalize(message.text).lowercased()
        let range = NSRange(text.startIndex..<text.endIndex, in: text)
        if builtInExpressions.contains(where: { $0.firstMatch(in: text, range: range) != nil }) {
            return "命中內建廣告規則"
        }
        if let keyword = keywords.first(where: { text.contains($0) }) {
            return "命中學習關鍵字：\(keyword)"
        }
        if regularExpressions.contains(where: { $0.firstMatch(in: text, range: range) != nil }) {
            return "命中學習規則"
        }
        return nil
    }

    static func normalize(_ value: String) -> String {
        String(value.map { traditionalCharacterMap[$0] ?? $0 })
    }

    static func learn(from value: String, existing: LearnedPatterns) -> LearnedPatterns {
        let normalized = normalize(value.trimmingCharacters(in: .whitespacesAndNewlines))
        guard !normalized.isEmpty else { return existing }
        var result = existing
        let stopWords: Set<String> = [
            "我們", "他們", "可以", "沒有", "這個", "那個", "什麼", "因為", "所以", "但是",
            "如果", "雖然", "然後", "而且", "或者", "不過", "還是", "就是", "不是", "一個"
        ]
        let tokenPattern = try? NSRegularExpression(pattern: "[\\u{4e00}-\\u{9fff}]{2,6}")
        let fullRange = NSRange(normalized.startIndex..<normalized.endIndex, in: normalized)
        for match in tokenPattern?.matches(in: normalized, range: fullRange) ?? [] {
            guard let range = Range(match.range, in: normalized) else { continue }
            let token = String(normalized[range])
            if !stopWords.contains(token), !result.keywords.contains(token) { result.keywords.append(token) }
        }

        if let contactRegex = try? NSRegularExpression(pattern: "(?i)(加微信|加\\s*(?:V|v)|加|薇|威|wechat|line|whatsapp)[-:\\s]*([a-zA-Z0-9_]{4,})"),
           let match = contactRegex.firstMatch(in: normalized, range: fullRange),
           let range = Range(match.range(at: 2), in: normalized) {
            let pattern = NSRegularExpression.escapedPattern(for: String(normalized[range]))
            if !result.patterns.contains(pattern) { result.patterns.append(pattern) }
        }
        if let urlRegex = try? NSRegularExpression(pattern: "https?://[^\\s]{4,}") {
            for match in urlRegex.matches(in: normalized, range: fullRange) {
                guard let range = Range(match.range, in: normalized) else { continue }
                let prefix = String(normalized[range].prefix(20))
                let pattern = NSRegularExpression.escapedPattern(for: prefix)
                if !result.patterns.contains(pattern) { result.patterns.append(pattern) }
            }
        }
        if result.keywords == existing.keywords && result.patterns == existing.patterns {
            let pattern = NSRegularExpression.escapedPattern(for: String(normalized.prefix(30)))
            if !result.patterns.contains(pattern) { result.patterns.append(pattern) }
        }
        return result
    }
}

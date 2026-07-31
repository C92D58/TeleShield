import Foundation

enum AuthenticationPresentation {
    static func shouldDismissLoginSheet(
        event: String,
        accountID: String?,
        targetAccountID: String?
    ) -> Bool {
        event == "auth_succeeded" && accountID != nil && accountID == targetAccountID
    }
}

struct AccountSummary: Codable, Identifiable {
    let accountID: String?
    let userID: Int64?
    let username: String
    let displayName: String
    let phoneMasked: String
    let configured: Bool
    let blockedCount: Int
    let kickedCount: Int
    let recentBlockCount: Int
    let whitelistCount: Int
    let blacklistCount: Int
    let learnedKeywordCount: Int
    let lastScan: String?
    let running: Bool
    let ready: Bool
    let state: String
    let error: String?

    var id: String { accountID ?? "__default__" }
    var label: String { displayName.isEmpty ? (username.isEmpty ? id : "@\(username)") : displayName }

    enum CodingKeys: String, CodingKey {
        case accountID = "id"
        case userID = "user_id"
        case username
        case displayName = "display_name"
        case phoneMasked = "phone_masked"
        case configured
        case blockedCount = "blocked_count"
        case kickedCount = "kicked_count"
        case recentBlockCount = "recent_block_count"
        case whitelistCount = "whitelist_count"
        case blacklistCount = "blacklist_count"
        case learnedKeywordCount = "learned_keyword_count"
        case lastScan = "last_scan"
        case running
        case ready
        case state
        case error
    }
}

struct OCRStatus: Codable {
    let available: Bool
    let bundled: Bool
    let languages: [String]
}

struct CoreStatus: Codable {
    let activeAccountID: String?
    let selectedAccount: AccountSummary
    let accounts: [AccountSummary]
    let ocr: OCRStatus

    enum CodingKeys: String, CodingKey {
        case activeAccountID = "active_account_id"
        case selectedAccount = "selected_account"
        case accounts
        case ocr
    }
}

struct ManagedGroup: Codable, Identifiable {
    let groupID: String
    let title: String
    let username: String
    let permission: String
    let enabled: Bool
    /// Channel/supergroup references are retained separately from the UI
    /// label.  A channel `access_hash` is required by
    /// `updates.getChannelDifference`; looking it up from a truncated dialog
    /// page each polling cycle is both expensive and unreliable.
    let accessHash: Int64?
    let isChannel: Bool
    let isBroadcast: Bool

    var id: String { groupID }

    enum CodingKeys: String, CodingKey {
        case groupID = "id"
        case title
        case username
        case permission
        case enabled
        case accessHash = "access_hash"
        case isChannel = "is_channel"
        case isBroadcast = "is_broadcast"
    }

    init(
        groupID: String,
        title: String,
        username: String,
        permission: String,
        enabled: Bool,
        accessHash: Int64? = nil,
        isChannel: Bool = false,
        isBroadcast: Bool = false
    ) {
        self.groupID = groupID
        self.title = title
        self.username = username
        self.permission = permission
        self.enabled = enabled
        self.accessHash = accessHash
        self.isChannel = isChannel
        self.isBroadcast = isBroadcast
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let stringID = try? container.decode(String.self, forKey: .groupID) {
            groupID = stringID
        } else if let numericID = try? container.decode(Int64.self, forKey: .groupID) {
            groupID = String(numericID)
        } else {
            groupID = ""
        }
        title = (try? container.decode(String.self, forKey: .title)) ?? groupID
        username = (try? container.decode(String.self, forKey: .username)) ?? ""
        permission = (try? container.decode(String.self, forKey: .permission)) ?? ""
        enabled = (try? container.decode(Bool.self, forKey: .enabled)) ?? true
        accessHash = try? container.decode(Int64.self, forKey: .accessHash)
        isChannel = (try? container.decode(Bool.self, forKey: .isChannel)) ?? false
        isBroadcast = (try? container.decode(Bool.self, forKey: .isBroadcast)) ?? false
    }
}

struct ScanSettings: Codable {
    var privateDialogLimit: Int
    var privateMessageLimit: Int
    var privateDays: Int
    var groupDialogLimit: Int
    var groupMessageLimit: Int
    var groupDays: Int

    enum CodingKeys: String, CodingKey {
        case privateDialogLimit = "private_dialog_limit"
        case privateMessageLimit = "private_message_limit"
        case privateDays = "private_days"
        case groupDialogLimit = "group_dialog_limit"
        case groupMessageLimit = "group_message_limit"
        case groupDays = "group_days"
    }

    static let defaults = ScanSettings(
        privateDialogLimit: 30,
        privateMessageLimit: 5,
        privateDays: 14,
        groupDialogLimit: 50,
        groupMessageLimit: 20,
        groupDays: 3
    )

    var normalized: ScanSettings {
        ScanSettings(
            privateDialogLimit: min(max(privateDialogLimit, 1), 100),
            privateMessageLimit: min(max(privateMessageLimit, 1), 100),
            privateDays: min(max(privateDays, 1), 365),
            groupDialogLimit: min(max(groupDialogLimit, 1), 100),
            groupMessageLimit: min(max(groupMessageLimit, 1), 100),
            groupDays: min(max(groupDays, 1), 365)
        )
    }
}

struct LearnedPatterns: Codable {
    var keywords: [String]
    var patterns: [String]

    static let empty = LearnedPatterns(keywords: [], patterns: [])
}

struct AccountDetails: Codable {
    let accountID: String?
    let loggedIn: Bool
    let hasAPICredentials: Bool
    let managedGroups: [ManagedGroup]
    let scanSettings: ScanSettings
    let learnedPatterns: LearnedPatterns
    let autoStart: Bool
    let autoStartAccountID: String?
    let autoStartAccountIDs: [String]

    enum CodingKeys: String, CodingKey {
        case accountID = "account_id"
        case loggedIn = "logged_in"
        case hasAPICredentials = "has_api_credentials"
        case managedGroups = "managed_groups"
        case scanSettings = "scan_settings"
        case learnedPatterns = "learned_patterns"
        case autoStart = "auto_start"
        case autoStartAccountID = "auto_start_account_id"
        case autoStartAccountIDs = "auto_start_account_ids"
    }
}

struct ListEntry: Codable, Identifiable {
    let userID: String
    let username: String
    let added: String
    let reason: String

    var id: String { userID }

    init(userID: String, username: String, added: String, reason: String) {
        self.userID = userID
        self.username = username
        self.added = added
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case username
        case added
        case reason
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let value = try? container.decode(String.self, forKey: .userID) {
            userID = value
        } else if let value = try? container.decode(Int64.self, forKey: .userID) {
            userID = String(value)
        } else {
            userID = ""
        }
        username = (try? container.decode(String.self, forKey: .username)) ?? ""
        added = (try? container.decode(String.self, forKey: .added)) ?? ""
        reason = (try? container.decode(String.self, forKey: .reason)) ?? ""
    }
}

struct BlockRecord: Codable, Identifiable {
    let time: String
    let source: String
    let userID: String
    let name: String
    let reason: String

    var id: String { "\(time)-\(source)-\(userID)-\(name)-\(reason)" }

    init(time: String, source: String, userID: String, name: String, reason: String) {
        self.time = time
        self.source = source
        self.userID = userID
        self.name = name
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case time
        case source
        case userID = "user_id"
        case name
        case reason
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        time = (try? container.decode(String.self, forKey: .time)) ?? ""
        source = (try? container.decode(String.self, forKey: .source)) ?? ""
        if let value = try? container.decode(String.self, forKey: .userID) {
            userID = value
        } else if let value = try? container.decode(Int64.self, forKey: .userID) {
            userID = String(value)
        } else {
            userID = ""
        }
        name = (try? container.decode(String.self, forKey: .name)) ?? ""
        reason = (try? container.decode(String.self, forKey: .reason)) ?? ""
    }
}

struct ScanFinding: Codable, Identifiable {
    let userID: String
    let name: String
    let group: String?
    let reason: String

    var id: String { "\(userID)-\(group ?? "")-\(reason)" }

    init(userID: String, name: String, group: String?, reason: String) {
        self.userID = userID
        self.name = name
        self.group = group
        self.reason = reason
    }

    enum CodingKeys: String, CodingKey {
        case userID = "user_id"
        case name
        case group
        case reason
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        if let value = try? container.decode(String.self, forKey: .userID) {
            userID = value
        } else if let value = try? container.decode(Int64.self, forKey: .userID) {
            userID = String(value)
        } else {
            userID = ""
        }
        name = (try? container.decode(String.self, forKey: .name)) ?? userID
        group = try? container.decode(String.self, forKey: .group)
        reason = (try? container.decode(String.self, forKey: .reason)) ?? ""
    }
}

struct ScanResult: Codable {
    let scope: String
    let dryRun: Bool
    let dialogsSeen: Int
    let dialogsScanned: Int
    let groupsFound: Int
    let messagesScanned: Int
    let matched: Int
    let acted: Int
    let errors: [String]
    let findings: [ScanFinding]
    let cancelled: Bool

    enum CodingKeys: String, CodingKey {
        case scope
        case dryRun = "dry_run"
        case dialogsSeen = "dialogs_seen"
        case dialogsScanned = "dialogs_scanned"
        case groupsFound = "groups_found"
        case messagesScanned = "messages_scanned"
        case matched
        case acted
        case errors
        case findings
        case cancelled
    }
}

struct Report: Codable {
    let period: String
    let label: String
    let total: Int
    let bySource: [String: Int]
    let byReason: [String: Int]
    let trend: [String: Int]
    let records: [BlockRecord]

    enum CodingKeys: String, CodingKey {
        case period
        case label
        case total
        case bySource = "by_source"
        case byReason = "by_reason"
        case trend
        case records
    }
}

struct CoreClientError: LocalizedError {
    let message: String

    var errorDescription: String? { message }
}

struct EventLogEntry: Identifiable {
    let id = UUID()
    let time = Date()
    let level: String
    let message: String
}

import Foundation

struct StoredAccount: Codable, Identifiable, Sendable {
    let id: String
    var userID: Int64?
    var username: String
    var displayName: String
    var phoneMasked: String
    let createdAt: Date
    var lastUsedAt: Date
    var autoStartProtection: Bool

    var isConfigured: Bool { userID != nil }

    var label: String {
        if !displayName.isEmpty { return displayName }
        if !username.isEmpty { return "@\(username)" }
        return id
    }

    enum CodingKeys: String, CodingKey {
        case id
        case userID = "user_id"
        case username
        case displayName = "display_name"
        case phoneMasked = "phone_masked"
        case createdAt = "created_at"
        case lastUsedAt = "last_used_at"
        case autoStartProtection = "auto_start_protection"
    }
}

struct StoredConfiguration: Codable, Sendable {
    var apiID: Int?
    var apiHash: String
    var phone: String
    var userID: Int64?
    var username: String
    var displayName: String
    var blockedCount: Int
    var kickedCount: Int
    var lastScan: Date?
    var lastPreview: Date?
    var whitelist: [String: ListEntry]
    var blacklist: [String: ListEntry]
    var managedGroups: [ManagedGroup]
    var learnedPatterns: LearnedPatterns
    var scanSettings: ScanSettings
    var listenScanGroups: Bool

    static let empty = StoredConfiguration(
        apiID: nil,
        apiHash: "",
        phone: "",
        userID: nil,
        username: "",
        displayName: "",
        blockedCount: 0,
        kickedCount: 0,
        lastScan: nil,
        lastPreview: nil,
        whitelist: [:],
        blacklist: [:],
        managedGroups: [],
        learnedPatterns: .empty,
        scanSettings: .defaults,
        listenScanGroups: true
    )

    enum CodingKeys: String, CodingKey {
        case apiID = "api_id"
        case apiHash = "api_hash"
        case phone
        case userID = "user_id"
        case username
        case displayName = "display_name"
        case blockedCount = "blocked_count"
        case kickedCount = "kicked_count"
        case lastScan = "last_scan"
        case lastPreview = "last_preview"
        case whitelist
        case blacklist
        case managedGroups = "managed_groups"
        case learnedPatterns = "learned_patterns"
        case scanSettings = "scan_settings"
        case listenScanGroups = "listen_scan_groups"
    }

    init(
        apiID: Int?,
        apiHash: String,
        phone: String,
        userID: Int64?,
        username: String,
        displayName: String,
        blockedCount: Int,
        kickedCount: Int,
        lastScan: Date?,
        lastPreview: Date?,
        whitelist: [String: ListEntry],
        blacklist: [String: ListEntry],
        managedGroups: [ManagedGroup],
        learnedPatterns: LearnedPatterns,
        scanSettings: ScanSettings,
        listenScanGroups: Bool
    ) {
        self.apiID = apiID
        self.apiHash = apiHash
        self.phone = phone
        self.userID = userID
        self.username = username
        self.displayName = displayName
        self.blockedCount = blockedCount
        self.kickedCount = kickedCount
        self.lastScan = lastScan
        self.lastPreview = lastPreview
        self.whitelist = whitelist
        self.blacklist = blacklist
        self.managedGroups = managedGroups
        self.learnedPatterns = learnedPatterns
        self.scanSettings = scanSettings
        self.listenScanGroups = listenScanGroups
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        apiID = try container.decodeIfPresent(Int.self, forKey: .apiID)
        apiHash = (try? container.decode(String.self, forKey: .apiHash)) ?? ""
        phone = (try? container.decode(String.self, forKey: .phone)) ?? ""
        userID = try container.decodeIfPresent(Int64.self, forKey: .userID)
        username = (try? container.decode(String.self, forKey: .username)) ?? ""
        displayName = (try? container.decode(String.self, forKey: .displayName)) ?? ""
        blockedCount = (try? container.decode(Int.self, forKey: .blockedCount)) ?? 0
        kickedCount = (try? container.decode(Int.self, forKey: .kickedCount)) ?? 0
        lastScan = try container.decodeIfPresent(Date.self, forKey: .lastScan)
        lastPreview = try container.decodeIfPresent(Date.self, forKey: .lastPreview)
        whitelist = (try? container.decode([String: ListEntry].self, forKey: .whitelist)) ?? [:]
        blacklist = (try? container.decode([String: ListEntry].self, forKey: .blacklist)) ?? [:]
        managedGroups = (try? container.decode([ManagedGroup].self, forKey: .managedGroups)) ?? []
        learnedPatterns = (try? container.decode(LearnedPatterns.self, forKey: .learnedPatterns)) ?? .empty
        scanSettings = (try? container.decode(ScanSettings.self, forKey: .scanSettings)) ?? .defaults
        listenScanGroups = (try? container.decode(Bool.self, forKey: .listenScanGroups)) ?? true
    }
}

struct StoredBlockLog: Codable, Sendable {
    var blocks: [BlockRecord]

    static let empty = StoredBlockLog(blocks: [])
}

struct NativeSession: Codable, Sendable {
    let dcID: Int
    let authKey: Data
    var userID: Int64
    var serverSalt: Int64
    var date: Date

    enum CodingKeys: String, CodingKey {
        case dcID = "dc_id"
        case authKey = "auth_key"
        case userID = "user_id"
        case serverSalt = "server_salt"
        case date
    }

    init(dcID: Int, authKey: Data, userID: Int64, serverSalt: Int64 = 0, date: Date) {
        self.dcID = dcID
        self.authKey = authKey
        self.userID = userID
        self.serverSalt = serverSalt
        self.date = date
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        dcID = try container.decode(Int.self, forKey: .dcID)
        authKey = try container.decode(Data.self, forKey: .authKey)
        userID = try container.decode(Int64.self, forKey: .userID)
        serverSalt = (try? container.decode(Int64.self, forKey: .serverSalt)) ?? 0
        date = try container.decode(Date.self, forKey: .date)
    }
}

/// Persistent common Telegram update state for one authorized account.
///
/// Telegram's `updates.getDifference` contract is stateful. Keeping this
/// separately from user-editable configuration lets the background worker
/// resume after a process restart without mixing protocol cursors into UI
/// settings or credentials.
struct NativeUpdateState: Codable, Sendable, Equatable {
    var pts: Int32
    var qts: Int32
    var date: Int32
    var seq: Int32
    var unreadCount: Int32

    init(pts: Int32, qts: Int32, date: Int32, seq: Int32, unreadCount: Int32) {
        self.pts = pts
        self.qts = qts
        self.date = date
        self.seq = seq
        self.unreadCount = unreadCount
    }

    enum CodingKeys: String, CodingKey {
        case pts
        case qts
        case date
        case seq
        case unreadCount = "unread_count"
    }
}

/// Persistent PTS for one channel/supergroup message box.
///
/// Telegram has a separate update sequence for every channel/supergroup;
/// the common `updates.getDifference` cursor cannot be used to recover those
/// messages.  Keeping this value in its own record also lets a failed action
/// leave only that channel eligible for retry without rewinding the common
/// account cursor.
struct NativeChannelUpdateState: Codable, Sendable, Equatable {
    let channelID: Int64
    var pts: Int32

    enum CodingKeys: String, CodingKey {
        case channelID = "channel_id"
        case pts
    }

    init(channelID: Int64, pts: Int32) {
        self.channelID = channelID
        self.pts = pts
    }
}

struct NativeUser: Sendable, Equatable {
    let id: Int64
    let accessHash: Int64?
    let firstName: String
    let lastName: String
    let username: String
    let phone: String?
    let isSelf: Bool
    let isBot: Bool

    var displayName: String {
        let value = "\(firstName) \(lastName)".trimmingCharacters(in: .whitespacesAndNewlines)
        return value.isEmpty ? (username.isEmpty ? String(id) : "@\(username)") : value
    }
}

struct NativeChat: Sendable, Equatable {
    let id: Int64
    let accessHash: Int64?
    let title: String
    let username: String
    let isChannel: Bool
    let isBroadcast: Bool
    let isMegagroup: Bool
    let adminRights: Bool
}

struct NativeMessage: Sendable, Equatable {
    let id: Int32
    let peerID: Int64
    let senderID: Int64?
    /// Telegram user/chat/channel namespaces are distinct even when their
    /// numeric IDs happen to overlap.  Keep the decoded peer kinds alongside
    /// the legacy numeric projections so moderation never has to infer a
    /// channel from an Int64 alone.
    let peerIdentity: NativePeerIdentity?
    let senderIdentity: NativePeerIdentity?
    let date: Date
    let text: String
    let hasPhoto: Bool
    let photo: NativePhotoReference?

    init(
        id: Int32,
        peerID: Int64,
        senderID: Int64?,
        date: Date,
        text: String,
        hasPhoto: Bool,
        photo: NativePhotoReference? = nil,
        peerIdentity: NativePeerIdentity? = nil,
        senderIdentity: NativePeerIdentity? = nil
    ) {
        self.id = id
        self.peerID = peerID
        self.senderID = senderID
        self.peerIdentity = peerIdentity
        self.senderIdentity = senderIdentity
        self.date = date
        self.text = text
        self.hasPhoto = hasPhoto
        self.photo = photo
    }
}

struct NativePhotoReference: Sendable, Equatable {
    let id: Int64
    let accessHash: Int64
    let fileReference: Data
    let dcID: Int32
    let thumbSize: String
}

struct NativeDialog: Sendable, Equatable {
    let peer: NativePeer
    let title: String
    let isPrivate: Bool
    let isGroup: Bool
    let isBroadcast: Bool
    /// Initial channel PTS carried by `messages.dialog` when available.
    /// This is only a bootstrap value; the durable cursor lives in
    /// `NativeChannelUpdateState`.
    let channelPTS: Int32?
}

enum NativePeer: Sendable, Equatable, Hashable {
    case user(id: Int64, accessHash: Int64?)
    case chat(id: Int64)
    case channel(id: Int64, accessHash: Int64?)

    var stableID: NativePeerIdentity {
        switch self {
        case .user(let id, _): return .user(id)
        case .chat(let id): return .chat(id)
        case .channel(let id, _): return .channel(id)
        }
    }
}

enum NativePeerIdentity: Sendable, Equatable, Hashable {
    case user(Int64)
    case chat(Int64)
    case channel(Int64)
}

struct NativeScanResult: Sendable {
    let matched: Int
    let acted: Int
    let findings: [ScanFinding]
    let dialogsSeen: Int
    let dialogsScanned: Int
    let groupsFound: Int
    let messagesScanned: Int
    let errors: [String]
    let cancelled: Bool
    let dryRun: Bool
}

enum NativeAuthState: Sendable, Equatable {
    case waitingForCode(flowID: UUID, phoneCodeHash: String)
    case waitingForPassword(flowID: UUID, hint: String)
    case authorized(user: NativeUser)
}

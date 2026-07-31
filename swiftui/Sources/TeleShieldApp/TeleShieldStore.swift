import Foundation
import Security
#if canImport(Darwin)
import Darwin
#endif

enum NativeStoreError: LocalizedError {
    case invalidAccountID
    case missingAccount
    case corruptedFile(URL)
    case keychain(OSStatus)
    case invalidSession
    case fileProtection(URL, Int32)

    var errorDescription: String? {
        switch self {
        case .invalidAccountID: return "帳號 ID 格式無效"
        case .missingAccount: return "找不到指定 Telegram 帳號"
        case .corruptedFile(let url): return "資料檔案無法讀取：\(url.lastPathComponent)"
        case .keychain(let status): return "Keychain 操作失敗（\(status)）"
        case .invalidSession: return "Telegram session 的 auth key 無效"
        case .fileProtection(let url, let status): return "無法保護資料檔案權限：\(url.lastPathComponent)（\(status)）"
        }
    }
}

private struct StoredRegistry: Codable, Sendable {
    var version = 1
    var activeAccountID: String?
    var autoStartAccountIDs: [String] = []
    var autoStartConfigured = false
    var accounts: [StoredAccount] = []

    enum CodingKeys: String, CodingKey {
        case version
        case activeAccountID = "active_account_id"
        case autoStartAccountIDs = "auto_start_account_ids"
        case autoStartConfigured = "auto_start_accounts_configured"
        case accounts
    }

    enum LegacyCodingKeys: String, CodingKey {
        case autoStartAccountID = "auto_start_account_id"
    }

    init() {}

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        version = (try? container.decode(Int.self, forKey: .version)) ?? 1
        activeAccountID = try container.decodeIfPresent(String.self, forKey: .activeAccountID)
        if let ids = try container.decodeIfPresent([String].self, forKey: .autoStartAccountIDs) {
            autoStartAccountIDs = ids
        } else if let legacyValue = try? decoder.container(keyedBy: LegacyCodingKeys.self).decode(String.self, forKey: .autoStartAccountID), !legacyValue.isEmpty {
            autoStartAccountIDs = [legacyValue]
        }
        autoStartConfigured = try container.decodeIfPresent(Bool.self, forKey: .autoStartConfigured) ?? false
        accounts = try container.decodeIfPresent([StoredAccount].self, forKey: .accounts) ?? []
    }
}

private struct StoredSessionMetadata: Codable, Sendable {
    let dcID: Int
    let userID: Int64
    let serverSalt: Int64
    let date: Date

    enum CodingKeys: String, CodingKey {
        case dcID = "dc_id"
        case userID = "user_id"
        case serverSalt = "server_salt"
        case date
    }

    init(dcID: Int, userID: Int64, serverSalt: Int64, date: Date) {
        self.dcID = dcID
        self.userID = userID
        self.serverSalt = serverSalt
        self.date = date
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        dcID = try container.decode(Int.self, forKey: .dcID)
        userID = try container.decode(Int64.self, forKey: .userID)
        serverSalt = (try? container.decode(Int64.self, forKey: .serverSalt)) ?? 0
        date = try container.decode(Date.self, forKey: .date)
    }
}

private enum NativeDateCoding {
    static let encoder: JSONEncoder = {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        encoder.dateEncodingStrategy = .iso8601
        return encoder
    }()

    static let decoder: JSONDecoder = {
        let decoder = JSONDecoder()
        // The legacy Python store writes ISO-8601 timestamps with fractional
        // seconds (for example, `2026-07-30T14:30:38.074423+00:00`), while
        // JSONDecoder's built-in `.iso8601` strategy only accepts the
        // non-fractional form on macOS.  Accept both formats so an app
        // upgrade never makes the account registry look corrupted.
        decoder.dateDecodingStrategy = .custom { decoder in
            let container = try decoder.singleValueContainer()
            let value = try container.decode(String.self)
            if let date = parse(value) {
                return date
            }
            throw DecodingError.dataCorruptedError(
                in: container,
                debugDescription: "Unsupported ISO-8601 date: \(value)"
            )
        }
        return decoder
    }()

    private static func parse(_ value: String) -> Date? {
        let fractional = ISO8601DateFormatter()
        fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = fractional.date(from: value) {
            return date
        }

        let standard = ISO8601DateFormatter()
        standard.formatOptions = [.withInternetDateTime]
        return standard.date(from: value)
    }
}

private struct KeychainVault {
    let service = "com.caryyu0306.TeleShield"

    func read(account: String, field: String) throws -> Data? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "\(account).\(field)",
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne
        ]
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw NativeStoreError.keychain(status) }
        return result as? Data
    }

    func write(_ data: Data, account: String, field: String) throws {
        let key = "\(account).\(field)"
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: key
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock
        ]
        let updateStatus = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if updateStatus == errSecItemNotFound {
            var item = query
            attributes.forEach { item[$0.key] = $0.value }
            let addStatus = SecItemAdd(item as CFDictionary, nil)
            guard addStatus == errSecSuccess else { throw NativeStoreError.keychain(addStatus) }
        } else if updateStatus != errSecSuccess {
            throw NativeStoreError.keychain(updateStatus)
        }
    }

    func delete(account: String, field: String) throws {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: "\(account).\(field)"
        ]
        let status = SecItemDelete(query as CFDictionary)
        guard status == errSecSuccess || status == errSecItemNotFound else {
            throw NativeStoreError.keychain(status)
        }
    }
}

actor TeleShieldStore {
    private let fileManager = FileManager.default
    private let root: URL
    private let keychain = KeychainVault()

    init(root: URL? = nil) {
        let manager = FileManager.default
        if let root {
            self.root = root
        } else if let override = ProcessInfo.processInfo.environment["TELESHIELD_DATA_DIR"] {
            self.root = URL(fileURLWithPath: override).standardizedFileURL
        } else {
            let support = manager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
                ?? manager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support", isDirectory: true)
            self.root = support.appendingPathComponent("TeleShield", isDirectory: true)
        }
    }

    func ensureRoot() throws {
        try fileManager.createDirectory(at: root, withIntermediateDirectories: true)
        try protect(root, mode: 0o700)
        try migrateLegacyIfNeeded()
    }

    func listAccounts() throws -> [StoredAccount] {
        try ensureRoot()
        return try loadRegistry().accounts
    }

    func activeAccountID() throws -> String? {
        try ensureRoot()
        return try loadRegistry().activeAccountID
    }

    func autoStartAccountIDs() throws -> [String] {
        try ensureRoot()
        let registry = try loadRegistry()
        if registry.autoStartConfigured {
            let valid = Set(registry.accounts.map(\.id))
            return registry.autoStartAccountIDs.filter { valid.contains($0) }
        }
        return registry.accounts.filter(\.autoStartProtection).map(\.id)
    }

    func createAccount(id: String? = nil) throws -> StoredAccount {
        try ensureRoot()
        var registry = try loadRegistry()
        let accountID = try validateAccountID(id ?? "account-\(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased())")
        if let existing = registry.accounts.first(where: { $0.id == accountID }) {
            try ensureAccountDirectory(accountID)
            return existing
        }
        let now = Date()
        let account = StoredAccount(
            id: accountID,
            userID: nil,
            username: "",
            displayName: "",
            phoneMasked: "",
            createdAt: now,
            lastUsedAt: now,
            autoStartProtection: false
        )
        registry.accounts.append(account)
        if registry.activeAccountID == nil { registry.activeAccountID = accountID }
        try ensureAccountDirectory(accountID)
        try saveRegistry(registry)
        try saveConfiguration(.empty, accountID: accountID)
        try save(StoredBlockLog.empty, to: blockLogURL(accountID))
        try saveLearnedPatterns(.empty, accountID: accountID)
        try save([NativeChannelUpdateState](), to: channelUpdateStatesURL(accountID))
        return account
    }

    func selectAccount(_ accountID: String) throws -> StoredAccount {
        try ensureRoot()
        var registry = try loadRegistry()
        guard let index = registry.accounts.firstIndex(where: { $0.id == accountID }) else {
            throw NativeStoreError.missingAccount
        }
        registry.activeAccountID = accountID
        registry.accounts[index].lastUsedAt = Date()
        try saveRegistry(registry)
        return registry.accounts[index]
    }

    func removeAccount(_ accountID: String, deleteFiles: Bool = true) throws {
        try ensureRoot()
        var registry = try loadRegistry()
        guard registry.accounts.contains(where: { $0.id == accountID }) else {
            throw NativeStoreError.missingAccount
        }
        registry.accounts.removeAll { $0.id == accountID }
        registry.autoStartAccountIDs.removeAll { $0 == accountID }
        if registry.activeAccountID == accountID { registry.activeAccountID = registry.accounts.first?.id }
        try saveRegistry(registry)
        try keychain.delete(account: accountID, field: "apiHash")
        try keychain.delete(account: accountID, field: "phone")
        try keychain.delete(account: accountID, field: "authKey")
        if deleteFiles {
            try removeIfExists(accountDirectory(accountID))
        }
    }

    func setAutoStartAccountIDs(_ accountIDs: [String]) throws {
        try ensureRoot()
        var registry = try loadRegistry()
        let valid = Set(registry.accounts.map(\.id))
        registry.autoStartAccountIDs = accountIDs.filter { valid.contains($0) }
        registry.autoStartConfigured = true
        try saveRegistry(registry)
    }

    func configuration(accountID: String) throws -> StoredConfiguration {
        try ensureExistingAccountDirectory(accountID)
        var configuration = try load(StoredConfiguration.self, from: configurationURL(accountID)) ?? .empty
        if let apiHash = try keychain.read(account: accountID, field: "apiHash"), let value = String(data: apiHash, encoding: .utf8) {
            configuration.apiHash = value
        }
        if let phone = try keychain.read(account: accountID, field: "phone"), let value = String(data: phone, encoding: .utf8) {
            configuration.phone = value
        }
        configuration.scanSettings = configuration.scanSettings.normalized
        return configuration
    }

    func saveConfiguration(_ configuration: StoredConfiguration, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        if !configuration.apiHash.isEmpty, let apiHash = configuration.apiHash.data(using: .utf8) {
            try keychain.write(apiHash, account: accountID, field: "apiHash")
        } else {
            try keychain.delete(account: accountID, field: "apiHash")
        }
        if !configuration.phone.isEmpty, let phone = configuration.phone.data(using: .utf8) {
            try keychain.write(phone, account: accountID, field: "phone")
        } else {
            try keychain.delete(account: accountID, field: "phone")
        }
        var redacted = configuration
        redacted.apiHash = ""
        redacted.phone = ""
        try save(redacted, to: configurationURL(accountID))
    }

    /// Performs a configuration read/modify/write entirely inside the store
    /// actor. Callers should prefer this over loading a configuration, waiting
    /// across other actor calls, and saving a stale copy later.
    func updateConfiguration(
        accountID: String,
        _ mutation: @Sendable (inout StoredConfiguration) throws -> Void
    ) throws {
        var configuration = try self.configuration(accountID: accountID)
        try mutation(&configuration)
        try saveConfiguration(configuration, accountID: accountID)
    }

    /// Records a completed moderation action while the store actor owns both
    /// the append and counter update. This keeps the normal success path
    /// consistent when UI refreshes and background protection run together.
    func recordAction(_ record: BlockRecord, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        var log = try load(StoredBlockLog.self, from: blockLogURL(accountID)) ?? .empty
        log.blocks.append(record)
        if log.blocks.count > 500 { log.blocks = Array(log.blocks.suffix(500)) }

        var configuration = try self.configuration(accountID: accountID)
        if record.source == "group" { configuration.kickedCount += 1 }
        else { configuration.blockedCount += 1 }

        try save(log, to: blockLogURL(accountID))
        try saveConfiguration(configuration, accountID: accountID)
    }

    func updateAccount(_ accountID: String, user: NativeUser, phone: String) throws {
        try ensureRoot()
        var registry = try loadRegistry()
        guard let index = registry.accounts.firstIndex(where: { $0.id == accountID }) else { throw NativeStoreError.missingAccount }
        registry.accounts[index].userID = user.id
        registry.accounts[index].username = user.username
        registry.accounts[index].displayName = user.displayName
        registry.accounts[index].phoneMasked = maskPhone(phone)
        registry.accounts[index].lastUsedAt = Date()
        try saveRegistry(registry)
        var configuration = try configuration(accountID: accountID)
        configuration.userID = user.id
        configuration.username = user.username
        configuration.displayName = user.displayName
        if !phone.isEmpty { configuration.phone = phone }
        try saveConfiguration(configuration, accountID: accountID)
    }

    func blockRecords(accountID: String, query: String = "", source: String = "all", limit: Int = 500) throws -> [BlockRecord] {
        try ensureExistingAccountDirectory(accountID)
        let log = try load(StoredBlockLog.self, from: blockLogURL(accountID)) ?? .empty
        let normalizedQuery = query.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return Array(log.blocks.reversed().filter { record in
            let sourceMatch = source == "all" || (source == "private" && (record.source == "private" || record.source == "scan")) || record.source == source
            let text = "\(record.userID) \(record.name) \(record.reason) \(record.source)".lowercased()
            return sourceMatch && (normalizedQuery.isEmpty || text.contains(normalizedQuery))
        }.prefix(max(1, limit)))
    }

    func appendBlock(_ record: BlockRecord, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        var log = try load(StoredBlockLog.self, from: blockLogURL(accountID)) ?? .empty
        log.blocks.append(record)
        if log.blocks.count > 500 { log.blocks = Array(log.blocks.suffix(500)) }
        try save(log, to: blockLogURL(accountID))
    }

    func incrementActionCounter(source: String, accountID: String) throws {
        var configuration = try configuration(accountID: accountID)
        if source == "group" { configuration.kickedCount += 1 }
        else { configuration.blockedCount += 1 }
        try saveConfiguration(configuration, accountID: accountID)
    }

    func loadLearnedPatterns(accountID: String) throws -> LearnedPatterns {
        try ensureExistingAccountDirectory(accountID)
        return try load(LearnedPatterns.self, from: learnedPatternsURL(accountID)) ?? .empty
    }

    func saveLearnedPatterns(_ patterns: LearnedPatterns, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        try save(patterns, to: learnedPatternsURL(accountID))
    }

    /// Updates both the dedicated learned-pattern file and the denormalized
    /// configuration snapshot in one actor-isolated operation.
    @discardableResult
    func updateLearnedPatterns(
        accountID: String,
        _ mutation: @Sendable (inout LearnedPatterns) throws -> Void
    ) throws -> LearnedPatterns {
        try ensureExistingAccountDirectory(accountID)
        var patterns = try load(LearnedPatterns.self, from: learnedPatternsURL(accountID)) ?? .empty
        try mutation(&patterns)
        try save(patterns, to: learnedPatternsURL(accountID))
        var configuration = try self.configuration(accountID: accountID)
        configuration.learnedPatterns = patterns
        try saveConfiguration(configuration, accountID: accountID)
        return patterns
    }

    func loadSession(accountID: String) throws -> NativeSession? {
        try ensureExistingAccountDirectory(accountID)
        guard let metadata = try load(StoredSessionMetadata.self, from: sessionMetadataURL(accountID)),
              let authKey = try keychain.read(account: accountID, field: "authKey") else { return nil }
        guard authKey.count == 256, (1...5).contains(metadata.dcID) else { throw NativeStoreError.invalidSession }
        return NativeSession(
            dcID: metadata.dcID,
            authKey: authKey,
            userID: metadata.userID,
            serverSalt: metadata.serverSalt,
            date: metadata.date
        )
    }

    func loadUpdateState(accountID: String) throws -> NativeUpdateState? {
        try ensureExistingAccountDirectory(accountID)
        return try load(NativeUpdateState.self, from: updateStateURL(accountID))
    }

    func saveUpdateState(_ state: NativeUpdateState, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        try save(state, to: updateStateURL(accountID))
    }

    func clearUpdateState(accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        try removeIfExists(updateStateURL(accountID))
    }

    /// Loads all per-channel PTS values for an account.  The on-disk shape is
    /// an array instead of a JSON dictionary so signed Telegram IDs remain
    /// unambiguous and the file remains forward-compatible with additional
    /// cursor metadata.
    func loadChannelUpdateStates(accountID: String) throws -> [Int64: NativeChannelUpdateState] {
        try ensureExistingAccountDirectory(accountID)
        let states = try load([NativeChannelUpdateState].self, from: channelUpdateStatesURL(accountID)) ?? []
        return Dictionary(states.map { ($0.channelID, $0) }, uniquingKeysWith: { _, latest in latest })
    }

    /// Atomically updates one channel cursor while preserving all other
    /// channel cursors for the same account.
    func saveChannelUpdateState(_ state: NativeChannelUpdateState, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        var states = try load([NativeChannelUpdateState].self, from: channelUpdateStatesURL(accountID)) ?? []
        states.removeAll { $0.channelID == state.channelID }
        states.append(state)
        states.sort { $0.channelID < $1.channelID }
        try save(states, to: channelUpdateStatesURL(accountID))
    }

    func clearChannelUpdateState(channelID: Int64, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        var states = try load([NativeChannelUpdateState].self, from: channelUpdateStatesURL(accountID)) ?? []
        states.removeAll { $0.channelID == channelID }
        try save(states, to: channelUpdateStatesURL(accountID))
    }

    func saveSession(_ session: NativeSession, accountID: String) throws {
        try ensureExistingAccountDirectory(accountID)
        guard session.authKey.count == 256, (1...5).contains(session.dcID) else { throw NativeStoreError.invalidSession }
        try keychain.write(session.authKey, account: accountID, field: "authKey")
        try save(
            StoredSessionMetadata(
                dcID: session.dcID,
                userID: session.userID,
                serverSalt: session.serverSalt,
                date: session.date
            ),
            to: sessionMetadataURL(accountID)
        )
    }

    func clearSession(accountID: String, removeCredentials: Bool) throws {
        try ensureExistingAccountDirectory(accountID)
        try removeIfExists(sessionMetadataURL(accountID))
        try removeIfExists(updateStateURL(accountID))
        try removeIfExists(channelUpdateStatesURL(accountID))
        try keychain.delete(account: accountID, field: "authKey")
        var configuration = try configuration(accountID: accountID)
        configuration.userID = nil
        configuration.username = ""
        configuration.displayName = ""
        configuration.lastScan = nil
        if removeCredentials {
            configuration.apiID = nil
            configuration.apiHash = ""
            configuration.phone = ""
            try keychain.delete(account: accountID, field: "apiHash")
            try keychain.delete(account: accountID, field: "phone")
        }
        try saveConfiguration(configuration, accountID: accountID)
        try updateRegistryForLogout(accountID)
    }

    private func updateRegistryForLogout(_ accountID: String) throws {
        var registry = try loadRegistry()
        guard let index = registry.accounts.firstIndex(where: { $0.id == accountID }) else { return }
        registry.accounts[index].userID = nil
        registry.accounts[index].username = ""
        registry.accounts[index].displayName = ""
        registry.accounts[index].phoneMasked = ""
        try saveRegistry(registry)
    }

    private func loadRegistry() throws -> StoredRegistry {
        try load(StoredRegistry.self, from: root.appendingPathComponent("accounts.json")) ?? StoredRegistry()
    }

    private func saveRegistry(_ registry: StoredRegistry) throws {
        try save(registry, to: root.appendingPathComponent("accounts.json"))
    }

    private func accountDirectory(_ accountID: String) -> URL {
        root.appendingPathComponent("accounts", isDirectory: true).appendingPathComponent(accountID, isDirectory: true)
    }

    private func configurationURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("config.json") }
    private func blockLogURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("block_log.json") }
    private func learnedPatternsURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("learned_patterns.json") }
    private func sessionMetadataURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("session.json") }
    private func updateStateURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("updates.json") }
    private func channelUpdateStatesURL(_ accountID: String) -> URL { accountDirectory(accountID).appendingPathComponent("channel_updates.json") }

    private func ensureAccountDirectory(_ accountID: String) throws {
        _ = try validateAccountID(accountID)
        try ensureRoot()
        let directory = accountDirectory(accountID)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try protect(directory, mode: 0o700)
    }

    private func ensureExistingAccountDirectory(_ accountID: String) throws {
        _ = try validateAccountID(accountID)
        try ensureRoot()
        let registry = try loadRegistry()
        guard registry.accounts.contains(where: { $0.id == accountID }) else {
            throw NativeStoreError.missingAccount
        }
        let directory = accountDirectory(accountID)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try protect(directory, mode: 0o700)
    }

    private func load<Value: Decodable>(_ type: Value.Type, from url: URL) throws -> Value? {
        guard fileManager.fileExists(atPath: url.path) else { return nil }
        do { return try NativeDateCoding.decoder.decode(Value.self, from: Data(contentsOf: url)) }
        catch { throw NativeStoreError.corruptedFile(url) }
    }

    private func save<Value: Encodable>(_ value: Value, to url: URL) throws {
        try fileManager.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try NativeDateCoding.encoder.encode(value)
        try data.write(to: url, options: .atomic)
        try protect(url, mode: 0o600)
    }

    private func removeIfExists(_ url: URL) throws {
        guard fileManager.fileExists(atPath: url.path) else { return }
        try fileManager.removeItem(at: url)
    }

    private func protect(_ url: URL, mode: Int16) throws {
        #if canImport(Darwin)
        guard chmod(url.path, mode_t(mode)) == 0 else {
            throw NativeStoreError.fileProtection(url, errno)
        }
        #endif
    }

    private func validateAccountID(_ value: String) throws -> String {
        guard !value.isEmpty, value != ".", value != "..", value.allSatisfy({ $0.isLetter || $0.isNumber || $0 == "-" || $0 == "_" }) else {
            throw NativeStoreError.invalidAccountID
        }
        return value
    }

    private func maskPhone(_ phone: String) -> String {
        guard phone.count > 4 else { return phone.isEmpty ? "" : String(repeating: "*", count: phone.count) }
        let prefix = phone.prefix(3)
        let suffix = phone.suffix(2)
        return "\(prefix)\(String(repeating: "*", count: max(1, phone.count - 5)))\(suffix)"
    }

    private func migrateLegacyIfNeeded() throws {
        // Existing files are read conservatively; an old binary session must
        // be re-established because the native client owns its auth key.
        let legacyConfig = root.appendingPathComponent("config.json")
        let legacySession = root.appendingPathComponent("user.session")
        guard fileManager.fileExists(atPath: legacyConfig.path) || fileManager.fileExists(atPath: legacySession.path) else { return }
        var registry = try loadRegistry()
        guard registry.accounts.isEmpty else { return }
        let accountID = "legacy-account"
        let now = Date()
        let account = StoredAccount(
            id: accountID,
            userID: nil,
            username: "",
            displayName: "",
            phoneMasked: "",
            createdAt: now,
            lastUsedAt: now,
            autoStartProtection: false
        )
        registry.accounts.append(account)
        try ensureAccountDirectoryWithoutMigration(accountID)
        try save(StoredConfiguration.empty, to: configurationURL(accountID))
        try save(StoredBlockLog.empty, to: blockLogURL(accountID))
        try save(LearnedPatterns.empty, to: learnedPatternsURL(accountID))
        if fileManager.fileExists(atPath: legacyConfig.path) {
            let legacy = try? Data(contentsOf: legacyConfig)
            var config = (legacy.flatMap { try? NativeDateCoding.decoder.decode(StoredConfiguration.self, from: $0) }) ?? .empty
            if let legacy, let object = try? JSONSerialization.jsonObject(with: legacy) as? [String: Any] {
                config.apiID = config.apiID ?? object["api_id"] as? Int
                config.apiHash = config.apiHash.isEmpty ? (object["api_hash"] as? String ?? "") : config.apiHash
                config.phone = config.phone.isEmpty ? (object["phone"] as? String ?? "") : config.phone
                config.userID = config.userID ?? (object["user_id"] as? NSNumber)?.int64Value
                config.username = config.username.isEmpty ? (object["username"] as? String ?? "") : config.username
                config.displayName = config.displayName.isEmpty ? (object["display_name"] as? String ?? "") : config.displayName
                if let apiHash = config.apiHash.data(using: .utf8), !config.apiHash.isEmpty {
                    try keychain.write(apiHash, account: accountID, field: "apiHash")
                }
                if let phone = config.phone.data(using: .utf8), !config.phone.isEmpty {
                    try keychain.write(phone, account: accountID, field: "phone")
                }
                config.apiHash = ""
                config.phone = ""
                try save(config, to: configurationURL(accountID))
                registry.accounts[0].userID = config.userID
                registry.accounts[0].username = config.username
                registry.accounts[0].displayName = config.displayName
                registry.accounts[0].phoneMasked = maskPhone(object["phone"] as? String ?? "")
            }
        }
        let legacyBlockLog = root.appendingPathComponent("block_log.json")
        if let data = try? Data(contentsOf: legacyBlockLog),
           let blocks = try? NativeDateCoding.decoder.decode(StoredBlockLog.self, from: data) {
            try save(blocks, to: blockLogURL(accountID))
        }
        let legacyPatterns = root.appendingPathComponent("learned_patterns.json")
        if let data = try? Data(contentsOf: legacyPatterns),
           let patterns = try? NativeDateCoding.decoder.decode(LearnedPatterns.self, from: data) {
            try save(patterns, to: learnedPatternsURL(accountID))
        }
        registry.activeAccountID = account.id
        try saveRegistry(registry)

        // The legacy root files may contain an API hash, phone number or a
        // serialized binary session. Once the new account-scoped copy is
        // durable, retire those plaintext artifacts so the migration does
        // not leave a second credential store behind.
        for legacyURL in [
            legacyConfig,
            legacySession,
            root.appendingPathComponent("block_log.json"),
            root.appendingPathComponent("learned_patterns.json")
        ] {
            try removeIfExists(legacyURL)
        }
    }

    private func ensureAccountDirectoryWithoutMigration(_ accountID: String) throws {
        _ = try validateAccountID(accountID)
        let directory = accountDirectory(accountID)
        try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        try protect(directory, mode: 0o700)
    }
}

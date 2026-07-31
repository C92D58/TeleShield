import Combine
import Foundation
@preconcurrency import ServiceManagement

@MainActor
final class CoreClient: ObservableObject {
    @Published private(set) var status: CoreStatus?
    @Published private(set) var details: AccountDetails?
    @Published private(set) var startupEnabled = false
    @Published private(set) var connectionMessage = "尚未啟動"
    @Published private(set) var lastLog = ""
    @Published private(set) var errorMessage: String?
    @Published private(set) var eventLog: [EventLogEntry] = []
    @Published private(set) var isBusy = false
    @Published private(set) var busyOperation: String?
    @Published private(set) var authFlowID: String?
    @Published private(set) var authChallengeKind: String?
    @Published private(set) var authDeliveryMessage = ""
    @Published private(set) var authInProgress = false
    @Published private(set) var authenticatedAccountID: String?
    @Published private(set) var whitelist: [ListEntry] = []
    @Published private(set) var blacklist: [ListEntry] = []
    @Published private(set) var learnedPatterns = LearnedPatterns.empty
    @Published private(set) var groups: [ManagedGroup] = []
    @Published private(set) var blockRecords: [BlockRecord] = []
    @Published private(set) var report: Report?
    @Published private(set) var scanSettings = ScanSettings.defaults
    @Published private(set) var scanJobID: String?
    @Published private(set) var scanProgress: [String] = []
    @Published private(set) var scanResult: ScanResult?
    @Published private(set) var operationJobID: String?

    private struct AuthContext {
        let flowID: String
        let accountID: String
        let phone: String
        let phoneCodeHash: String
        let api: TelegramAPI
        var challenge: TelegramPasswordChallenge?
    }

    private let store: TeleShieldStore
    private var apis: [String: TelegramAPI] = [:]
    private var coordinators: [String: ProtectionCoordinator] = [:]
    private var runningAccountIDs = Set<String>()
    private var startingAccountIDs = Set<String>()
    private var authContext: AuthContext?
    private var authRequestID: UUID?
    private var pendingAuthenticationAPI: TelegramAPI?
    private var serviceStarted = false
    private var automaticProtectionRetryAt: [String: Date] = [:]
    private var scanStartInFlight = false
    private var scanTask: Task<Void, Never>?
    private var refreshInFlight = false
    private var refreshRequested = false
    private var lifecycleGeneration = 0

    init(store: TeleShieldStore = TeleShieldStore()) {
        self.store = store
    }

    var selectedAccount: AccountSummary? { status?.selectedAccount }
    var selectedAccountID: String? { status?.activeAccountID ?? status?.selectedAccount.accountID }
    var helperIsRunning: Bool { serviceStarted }
    var hasActiveScan: Bool { scanTask != nil || scanJobID != nil || scanStartInFlight }
    var canModifySelectedAccount: Bool {
        guard let selectedAccount else { return false }
        return !selectedAccount.running && !authInProgress && operationJobID == nil
    }

    func launch() {
        guard !serviceStarted else { return }
        serviceStarted = true
        lifecycleGeneration &+= 1
        connectionMessage = "原生 Swift 服務已啟動"
        errorMessage = nil
        Task { [weak self] in await self?.refresh() }
    }

    func refresh() async {
        guard serviceStarted else { return }
        let generation = lifecycleGeneration
        guard !refreshInFlight else {
            refreshRequested = true
            return
        }
        refreshInFlight = true
        defer {
            refreshInFlight = false
            if refreshRequested && serviceStarted {
                refreshRequested = false
                Task { @MainActor [weak self] in await self?.refresh() }
            } else {
                refreshRequested = false
            }
        }
        do {
            try await store.ensureRoot()
            guard serviceStarted, lifecycleGeneration == generation else { return }
            var accounts = try await store.listAccounts()
            if accounts.isEmpty {
                _ = try await store.createAccount()
                accounts = try await store.listAccounts()
            }
            guard serviceStarted, lifecycleGeneration == generation else { return }
            let storedActiveID = try await store.activeAccountID()
            let activeID = storedActiveID ?? accounts.first?.id
            if let activeID, storedActiveID == nil {
                _ = try await store.selectAccount(activeID)
            }
            guard serviceStarted, lifecycleGeneration == generation else { return }
            try await rebuildStatus(accounts: accounts, activeAccountID: activeID, expectedGeneration: generation)
            guard serviceStarted, lifecycleGeneration == generation else { return }
            connectionMessage = "已連線"
            errorMessage = nil
            await refreshAccountData(expectedGeneration: generation)
            guard serviceStarted, lifecycleGeneration == generation else { return }
            await startAutomaticProtectionIfNeeded(expectedGeneration: generation)
        } catch {
            guard serviceStarted, lifecycleGeneration == generation else { return }
            present(error: error)
        }
    }

    func refreshAccountData(expectedGeneration: Int? = nil) async {
        guard serviceStarted, let accountID = selectedAccountID else { return }
        let generation = expectedGeneration ?? lifecycleGeneration
        do {
            let config = try await store.configuration(accountID: accountID)
            let autoIDs = try await store.autoStartAccountIDs()
            let records = try await store.blockRecords(accountID: accountID)
            guard serviceStarted,
                  lifecycleGeneration == generation,
                  selectedAccountID == accountID else { return }
            let nextDetails = AccountDetails(
                accountID: accountID,
                loggedIn: config.userID != nil,
                hasAPICredentials: config.apiID != nil && !config.apiHash.isEmpty,
                managedGroups: config.managedGroups,
                scanSettings: config.scanSettings,
                learnedPatterns: config.learnedPatterns,
                autoStart: autoIDs.contains(accountID),
                autoStartAccountID: autoIDs.first,
                autoStartAccountIDs: autoIDs
            )
            details = nextDetails
            scanSettings = config.scanSettings
            learnedPatterns = config.learnedPatterns
            groups = config.managedGroups
            whitelist = config.whitelist.values.sorted { $0.userID < $1.userID }
            blacklist = config.blacklist.values.sorted { $0.userID < $1.userID }
            blockRecords = records
        } catch {
            guard serviceStarted, lifecycleGeneration == generation, selectedAccountID == accountID else { return }
            present(error: error)
        }
    }

    func selectAccount(_ accountID: String) async {
        await runBusy("切換帳號") {
            _ = try await self.store.selectAccount(accountID)
            self.report = nil
            self.blockRecords = []
            self.eventLog.removeAll()
            await self.refresh()
        }
    }

    func createAccount() async -> String? {
        do {
            let account = try await store.createAccount()
            await refresh()
            await selectAccount(account.id)
            return account.id
        } catch {
            present(error: error)
            return nil
        }
    }

    func removeAccount(_ accountID: String, deleteFiles: Bool = true) async -> Bool {
        guard !isBusy else { return false }
        isBusy = true
        busyOperation = "移除帳號"
        defer { isBusy = false; busyOperation = nil }
        do {
            await stopProtection(for: accountID)
            if let api = apis.removeValue(forKey: accountID) { await api.disconnect() }
            try await store.removeAccount(accountID, deleteFiles: deleteFiles)
            await refresh()
            return true
        } catch {
            present(error: error)
            return false
        }
    }

    func startAll() async {
        await runBusy("啟動全部帳號") {
            for account in try await self.store.listAccounts() where account.isConfigured {
                if !self.runningAccountIDs.contains(account.id) {
                    do {
                        try await self.startProtection(accountID: account.id)
                    } catch {
                        self.appendLog("啟動 \(account.label) 失敗：\(error.localizedDescription)", level: "error")
                    }
                }
            }
            await self.refresh()
        }
    }

    func stopAll() async {
        await runBusy("停止全部帳號") {
            for accountID in Array(self.runningAccountIDs) {
                await self.stopProtection(for: accountID)
            }
            await self.refresh()
        }
    }

    func setAutoStartAccounts(accountIDs: [String]) async {
        do {
            try await store.setAutoStartAccountIDs(accountIDs)
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func setAutoStart(accountID: String?) async {
        await setAutoStartAccounts(accountIDs: accountID.map { [$0] } ?? [])
    }

    func setStartup(_ enabled: Bool) async {
        do {
            if enabled { try SMAppService.mainApp.register() }
            else { try await SMAppService.mainApp.unregister() }
            UserDefaults.standard.set(enabled, forKey: "teleShield.startupEnabled")
            startupEnabled = enabled
            appendLog(enabled ? "已啟用登入時啟動" : "已停用登入時啟動", level: "info")
        } catch {
            present(error: error)
        }
    }

    func startProtection() async {
        guard let accountID = selectedAccountID else { return }
        await runBusy("啟動防護") {
            try await self.startProtection(accountID: accountID)
            await self.refresh()
        }
    }

    func stopProtection() async {
        guard let accountID = selectedAccountID else { return }
        await runBusy("停止防護") {
            await self.stopProtection(for: accountID)
            await self.refresh()
        }
    }

    func startAuthentication(apiID: String, apiHash: String, phone: String, accountID: String? = nil) async {
        guard let numericAPIID = Int(apiID.trimmingCharacters(in: .whitespacesAndNewlines)),
              numericAPIID > 0,
              numericAPIID <= Int(Int32.max) else {
            errorMessage = "API ID 必須是 Int32 範圍內的正整數"
            return
        }
        let normalizedHash = apiHash.trimmingCharacters(in: .whitespacesAndNewlines)
        let normalizedPhone = phone.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !normalizedHash.isEmpty, !normalizedPhone.isEmpty else {
            errorMessage = "API Hash 與手機號碼不可為空"
            return
        }
        if authContext != nil || pendingAuthenticationAPI != nil {
            await cancelAuthentication()
        }
        let requestID = UUID()
        authRequestID = requestID
        isBusy = true
        authInProgress = true
        errorMessage = nil
        defer {
            if authRequestID == requestID {
                pendingAuthenticationAPI = nil
                isBusy = false
            }
        }
        do {
            let targetID: String
            if let accountID { targetID = accountID }
            else { targetID = try await store.createAccount().id }
            guard authRequestID == requestID, authInProgress else { throw CancellationError() }
            try await store.updateConfiguration(accountID: targetID) { config in
                config.apiID = numericAPIID
                config.apiHash = normalizedHash
                config.phone = normalizedPhone
            }
            guard authRequestID == requestID, authInProgress else { throw CancellationError() }
            let api = TelegramAPI(apiID: numericAPIID, apiHash: normalizedHash)
            pendingAuthenticationAPI = api
            let sentCode = try await api.sendCode(phone: normalizedPhone)
            try Task.checkCancellation()
            guard authRequestID == requestID, authInProgress else { throw CancellationError() }
            let flowID = UUID().uuidString
            authContext = AuthContext(flowID: flowID, accountID: targetID, phone: normalizedPhone, phoneCodeHash: sentCode.phoneCodeHash, api: api, challenge: nil)
            pendingAuthenticationAPI = nil
            authFlowID = flowID
            authChallengeKind = "code"
            authDeliveryMessage = "驗證碼已透過 \(sentCode.deliveryDescription) 發送"
            connectionMessage = "等待 Telegram 驗證碼"
            appendLog(authDeliveryMessage, level: "info")
        } catch is CancellationError {
            if authRequestID == requestID {
                let pendingAPI = pendingAuthenticationAPI
                pendingAuthenticationAPI = nil
                authRequestID = nil
                if let pendingAPI { await pendingAPI.disconnect() }
                authContext = nil
                authFlowID = nil
                authChallengeKind = nil
                authInProgress = false
                isBusy = false
            }
        } catch {
            guard authRequestID == requestID, authInProgress else {
                return
            }
            let pendingAPI = pendingAuthenticationAPI
            pendingAuthenticationAPI = nil
            authRequestID = nil
            if let pendingAPI { await pendingAPI.disconnect() }
            authInProgress = false
            isBusy = false
            present(error: error)
        }
    }

    func submitAuthCode(_ value: String) async {
        guard let context = authContext, context.challenge == nil else { return }
        do {
            let user = try await context.api.signIn(phone: context.phone, phoneCodeHash: phoneCodeHash(from: context), code: value)
            try await completeAuthentication(context: context, user: user)
        } catch let error as TelegramMTProtoError {
            if case let .rpc(_, message) = error, message.contains("SESSION_PASSWORD_NEEDED") {
                do {
                    var next = context
                    next.challenge = try await context.api.passwordChallenge()
                    guard authContextIsCurrent(context) else { return }
                    authContext = next
                    authChallengeKind = "password"
                    connectionMessage = "需要 Telegram 兩步驟驗證"
                    if let hint = next.challenge?.hint, !hint.isEmpty {
                        authDeliveryMessage = "提示：\(hint)"
                    } else {
                        authDeliveryMessage = "請輸入 Telegram 兩步驟密碼"
                    }
                } catch is CancellationError {
                    return
                } catch {
                    guard authContextIsCurrent(context) else { return }
                    present(error: error)
                }
            } else {
                guard authContextIsCurrent(context) else { return }
                present(error: error)
            }
        } catch is CancellationError {
            return
        } catch {
            guard authContextIsCurrent(context) else { return }
            present(error: error)
        }
    }

    func submitAuthPassword(_ value: String) async {
        guard let context = authContext, let challenge = context.challenge else { return }
        do {
            let user = try await context.api.checkPassword(challenge, password: value)
            try await completeAuthentication(context: context, user: user)
        } catch is CancellationError {
            return
        } catch {
            guard authContextIsCurrent(context) else { return }
            present(error: error)
        }
    }

    func cancelAuthentication() async {
        authRequestID = nil
        let pendingAPI = pendingAuthenticationAPI
        pendingAuthenticationAPI = nil
        if let pendingAPI { await pendingAPI.disconnect() }
        if let context = authContext { await context.api.disconnect() }
        authContext = nil
        authFlowID = nil
        authChallengeKind = nil
        authInProgress = false
        isBusy = false
        connectionMessage = "已取消登入"
    }

    func fetchList(_ listType: String, query: String = "") async {
        guard isSupportedListType(listType) else { return }
        guard let accountID = selectedAccountID else { return }
        do {
            let config = try await store.configuration(accountID: accountID)
            let source = listType == "whitelist" ? config.whitelist.values : config.blacklist.values
            let normalized = query.lowercased()
            let result = source.filter { query.isEmpty || "\($0.userID) \($0.username) \($0.reason)".lowercased().contains(normalized) }.sorted { $0.userID < $1.userID }
            if listType == "whitelist" { whitelist = result } else { blacklist = result }
        } catch { present(error: error) }
    }

    func upsertList(_ listType: String, userID: String, username: String, reason: String) async {
        guard isSupportedListType(listType) else { return }
        guard let accountID = selectedAccountID else { return }
        do {
            let normalizedUserID = try normalizedTelegramUserID(userID)
            try await store.updateConfiguration(accountID: accountID) { config in
                let existing = listType == "whitelist" ? config.whitelist[normalizedUserID] : config.blacklist[normalizedUserID]
                let normalizedUsername = username.trimmingCharacters(in: .whitespacesAndNewlines).drop(while: { $0 == "@" })
                let normalizedReason = reason.trimmingCharacters(in: .whitespacesAndNewlines)
                let entry = ListEntry(
                    userID: normalizedUserID,
                    username: String(normalizedUsername),
                    added: existing?.added.isEmpty == false ? existing!.added : ISO8601DateFormatter().string(from: Date()),
                    reason: normalizedReason.isEmpty ? (existing?.reason.isEmpty == false ? existing!.reason : "manual") : normalizedReason
                )
                if listType == "whitelist" { config.whitelist[normalizedUserID] = entry }
                else { config.blacklist[normalizedUserID] = entry }
            }
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func removeListEntry(_ listType: String, userID: String) async {
        guard isSupportedListType(listType) else { return }
        guard let accountID = selectedAccountID else { return }
        do {
            try await store.updateConfiguration(accountID: accountID) { config in
                if listType == "whitelist" { config.whitelist.removeValue(forKey: userID) }
                else { config.blacklist.removeValue(forKey: userID) }
            }
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func importList(_ listType: String, path: String, replace: Bool) async {
        guard isSupportedListType(listType) else { return }
        guard let accountID = selectedAccountID else { return }
        do {
            let data = try Data(contentsOf: URL(fileURLWithPath: path))
            let url = URL(fileURLWithPath: path)
            let entries: [ListEntry]
            if url.pathExtension.lowercased() == "csv" {
                entries = parseCSV(data: data).compactMap { row in
                    guard let userID = row["user_id"], !userID.isEmpty else { return nil }
                    return ListEntry(userID: userID, username: row["username"] ?? "", added: row["added"] ?? "", reason: row["reason"] ?? "import")
                }
            } else {
                if let array = try? JSONDecoder().decode([ListEntry].self, from: data) {
                    entries = array
                } else {
                    let dictionary = try JSONDecoder().decode([String: ListEntry].self, from: data)
                    entries = dictionary.map { key, value in
                        ListEntry(userID: value.userID.isEmpty ? key : value.userID, username: value.username, added: value.added, reason: value.reason)
                    }
                }
            }
            try await store.updateConfiguration(accountID: accountID) { config in
                var values = replace ? [:] : (listType == "whitelist" ? config.whitelist : config.blacklist)
                for entry in entries {
                    guard let normalizedID = try? normalizedTelegramUserID(entry.userID) else { continue }
                    let sanitized = ListEntry(
                        userID: normalizedID,
                        username: String(entry.username.trimmingCharacters(in: .whitespacesAndNewlines).drop(while: { $0 == "@" })),
                        added: entry.added,
                        reason: entry.reason.isEmpty ? "import" : entry.reason
                    )
                    values[normalizedID] = sanitized
                }
                if listType == "whitelist" { config.whitelist = values }
                else { config.blacklist = values }
            }
            await fetchList(listType)
            await refresh()
        } catch { present(error: error) }
    }

    func exportList(_ listType: String, path: String, format: String) async {
        guard isSupportedListType(listType) else { return }
        do {
            let values = listType == "whitelist" ? whitelist : blacklist
            let data: Data
            if format.lowercased() == "csv" || URL(fileURLWithPath: path).pathExtension.lowercased() == "csv" {
                let rows = values.map { [$0.userID, $0.username, $0.added, $0.reason] }
                data = Data(csv(header: ["user_id", "username", "added", "reason"], rows: rows).utf8)
            } else {
                data = try JSONEncoder.pretty.encode(values)
            }
            try data.write(to: URL(fileURLWithPath: path), options: .atomic)
            appendLog("已匯出 \(listType) 名單（\(format.uppercased())）", level: "info")
        } catch { present(error: error) }
    }

    func learn(_ text: String) async {
        guard let accountID = selectedAccountID else { return }
        do {
            let value = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty else {
                errorMessage = "請提供要學習的廣告文字"
                return
            }
            _ = try await store.updateLearnedPatterns(accountID: accountID) { patterns in
                patterns = SpamRuleEngine.learn(from: value, existing: patterns)
            }
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func removeLearnedPattern(kind: String, value: String) async {
        guard let accountID = selectedAccountID else { return }
        do {
            _ = try await store.updateLearnedPatterns(accountID: accountID) { patterns in
                if kind == "keyword" || kind == "keywords" { patterns.keywords.removeAll { $0 == value } }
                else { patterns.patterns.removeAll { $0 == value } }
            }
            await refreshAccountData()
        } catch { present(error: error) }
    }

    func buildReport(period: String) async {
        guard let accountID = selectedAccountID else { return }
        do {
            let normalizedPeriod = ["day", "week", "all"].contains(period) ? period : "day"
            let label: String
            let cutoff: Date?
            switch normalizedPeriod {
            case "week": label = "過去 7 天"; cutoff = Date().addingTimeInterval(-7 * 86_400)
            case "all": label = "全部記錄"; cutoff = nil
            default: label = "過去 24 小時"; cutoff = Date().addingTimeInterval(-86_400)
            }
            let allRecords = try await store.blockRecords(accountID: accountID, limit: 500)
            let records = allRecords.filter { record in
                guard let cutoff else { return true }
                guard let date = ISO8601DateFormatter().date(from: record.time) else { return false }
                return date >= cutoff
            }
            let bySource = Dictionary(grouping: records, by: { $0.source == "scan" ? "private" : $0.source }).mapValues(\.count)
            let byReason = Dictionary(grouping: records, by: { String($0.reason.prefix(20)).isEmpty ? "未分類" : String($0.reason.prefix(20)) }).mapValues(\.count)
            let trend = Dictionary(grouping: records, by: { String($0.time.prefix(10)) }).mapValues(\.count)
            let report = Report(period: normalizedPeriod, label: label, total: records.count, bySource: bySource, byReason: byReason, trend: trend, records: records)
            if selectedAccountID == accountID { self.report = report }
        } catch { present(error: error) }
    }

    func exportReport(path: String) async {
        guard let report else { return }
        do {
            try JSONEncoder.pretty.encode(report).write(to: URL(fileURLWithPath: path), options: .atomic)
            appendLog("已匯出報告", level: "info")
        } catch { present(error: error) }
    }

    func fetchBlockRecords(query: String = "", source: String = "all") async {
        guard let accountID = selectedAccountID else { blockRecords = []; return }
        do {
            blockRecords = try await store.blockRecords(accountID: accountID, query: query, source: source)
        } catch { present(error: error) }
    }

    func exportBlocks(path: String, query: String, source: String, format: String) async {
        guard let accountID = selectedAccountID else {
            errorMessage = "請先選擇 Telegram 帳號"
            return
        }
        do {
            let records = try await store.blockRecords(accountID: accountID, query: query, source: source)
            if format.lowercased() == "csv" || URL(fileURLWithPath: path).pathExtension.lowercased() == "csv" {
                let rows = records.map { [$0.time, $0.source, $0.userID, $0.name, $0.reason] }
                try Data(csv(header: ["time", "source", "user_id", "name", "reason"], rows: rows).utf8)
                    .write(to: URL(fileURLWithPath: path), options: .atomic)
            } else {
                try JSONEncoder.pretty.encode(records).write(to: URL(fileURLWithPath: path), options: .atomic)
            }
            appendLog("已匯出封鎖記錄（\(format.uppercased())）", level: "info")
        } catch { present(error: error) }
    }

    func discoverGroups() async {
        guard operationJobID == nil, let accountID = selectedAccountID else { return }
        operationJobID = UUID().uuidString
        defer { operationJobID = nil }
        do {
            let api = try await api(for: accountID)
            let page = try await api.getDialogs(limit: 100)
            let discovered = page.chats
                .filter { !$0.isBroadcast && $0.adminRights }
                .map {
                    ManagedGroup(
                        groupID: String($0.id),
                        title: $0.title,
                        username: $0.username,
                        permission: "admin",
                        enabled: true,
                        accessHash: $0.accessHash,
                        isChannel: $0.isChannel,
                        isBroadcast: $0.isBroadcast
                    )
                }
            try await store.updateConfiguration(accountID: accountID) { config in
                let existing = Dictionary(config.managedGroups.map { ($0.groupID, $0) }, uniquingKeysWith: { first, _ in first })
                var merged = discovered.map { group in
                    guard let old = existing[group.groupID] else { return group }
                    return ManagedGroup(
                        groupID: group.groupID,
                        title: group.title,
                        username: group.username,
                        permission: group.permission,
                        enabled: old.enabled,
                        accessHash: group.accessHash ?? old.accessHash,
                        isChannel: group.isChannel || old.isChannel,
                        isBroadcast: group.isBroadcast || old.isBroadcast
                    )
                }
                let discoveredIDs = Set(discovered.map(\.groupID))
                merged.append(contentsOf: config.managedGroups.filter { !discoveredIDs.contains($0.groupID) })
                config.managedGroups = merged
            }
            await refreshAccountData()
        } catch {
            present(error: error)
        }
    }

    func setGroupEnabled(_ groupID: String, enabled: Bool) async {
        guard operationJobID == nil, let accountID = selectedAccountID else { return }
        operationJobID = UUID().uuidString
        defer { operationJobID = nil }
        do {
            try await store.updateConfiguration(accountID: accountID) { config in
                config.managedGroups = config.managedGroups.map {
                    $0.groupID == groupID
                        ? ManagedGroup(
                            groupID: $0.groupID,
                            title: $0.title,
                            username: $0.username,
                            permission: $0.permission,
                            enabled: enabled,
                            accessHash: $0.accessHash,
                            isChannel: $0.isChannel,
                            isBroadcast: $0.isBroadcast
                        )
                        : $0
                }
            }
            groups = try await store.configuration(accountID: accountID).managedGroups
        } catch { present(error: error) }
    }

    func updateScanSettings(_ settings: ScanSettings) async {
        guard let accountID = selectedAccountID else { return }
        do {
            let normalized = settings.normalized
            try await store.updateConfiguration(accountID: accountID) { config in
                config.scanSettings = normalized
            }
            scanSettings = normalized
            appendLog("掃描設定已儲存", level: "info")
        } catch { present(error: error) }
    }

    func startScan(scope: String, dryRun: Bool) async {
        guard !hasActiveScan, let accountID = selectedAccountID else { return }
        guard selectedAccount?.running != true else {
            errorMessage = "請先停止即時防護，再掃描同一帳號的歷史訊息"
            return
        }
        scanStartInFlight = true
        scanProgress.removeAll()
        scanResult = nil
        scanTask = Task { @MainActor [weak self] in
            await self?.performScan(accountID: accountID, scope: scope, dryRun: dryRun)
        }
    }

    func cancelScan() {
        scanTask?.cancel()
        scanJobID = nil
        scanStartInFlight = false
        appendLog("已取消掃描", level: "info")
    }

    private func performScan(accountID: String, scope: String, dryRun: Bool) async {
        defer {
            scanTask = nil
            scanStartInFlight = false
        }
        do {
            let config = try await store.configuration(accountID: accountID)
            let api = try await api(for: accountID)
            do {
                try await api.connect()
            } catch {
                removeAPIIfIdentical(api, accountID: accountID)
                throw error
            }
            try Task.checkCancellation()
            let coordinator = ProtectionCoordinator(accountID: accountID, api: api, store: store, configuration: config)
            let jobID = UUID().uuidString
            scanJobID = jobID
            let result = try await coordinator.scan(scope: scope, dryRun: dryRun, settings: config.scanSettings) { [weak self] message in
                Task { @MainActor [weak self] in
                    self?.scanProgress.append(message)
                    self?.appendLog(message, level: "info")
                }
            }
            guard !Task.isCancelled, scanJobID == jobID else {
                scanJobID = nil
                return
            }
            let shouldPublishResult = selectedAccountID == accountID
            if shouldPublishResult {
                scanResult = ScanResult(scope: scope, dryRun: dryRun, dialogsSeen: result.dialogsSeen, dialogsScanned: result.dialogsScanned, groupsFound: result.groupsFound, messagesScanned: result.messagesScanned, matched: result.matched, acted: result.acted, errors: result.errors, findings: result.findings, cancelled: result.cancelled)
            }
            try await store.updateConfiguration(accountID: accountID) { config in
                if dryRun { config.lastPreview = Date() }
                else { config.lastScan = Date() }
            }
            scanJobID = nil
            await refresh()
        } catch is CancellationError {
            scanJobID = nil
        } catch {
            scanJobID = nil
            present(error: error)
        }
    }

    func addFindings(_ findings: [ScanFinding], to listType: String) async {
        guard isSupportedListType(listType) else { return }
        await runBusy("加入名單") {
            for finding in findings {
                await self.upsertList(listType, userID: finding.userID, username: finding.name, reason: "歷史掃描：\(finding.reason)")
            }
            await self.refresh()
        }
    }

    func logout(removeCredentials: Bool) async {
        guard let accountID = selectedAccountID else { return }
        await runBusy("登出帳號") {
            // Stop polling before revoking the Telegram session so no
            // background request can race with logOut.
            await self.stopCoordinator(for: accountID)
            if let api = self.apis[accountID] {
                try? await api.logOut()
                await api.disconnect()
            }
            self.apis.removeValue(forKey: accountID)
            try await self.store.clearSession(accountID: accountID, removeCredentials: removeCredentials)
            await self.refresh()
        }
    }

    func clearSession(removeCredentials: Bool) async {
        guard let accountID = selectedAccountID else { return }
        await runBusy("清除登入狀態") {
            await self.stopProtection(for: accountID)
            if let api = self.apis.removeValue(forKey: accountID) {
                await api.disconnect()
            }
            try await self.store.clearSession(accountID: accountID, removeCredentials: removeCredentials)
            await self.refresh()
        }
    }

    func refreshOCR() async {
        guard let status else { return }
        let ocr = OCRService.status
        self.status = CoreStatus(activeAccountID: status.activeAccountID, selectedAccount: status.selectedAccount, accounts: status.accounts, ocr: ocr)
    }

    func shutdownGracefully() async {
        lifecycleGeneration &+= 1
        refreshRequested = false
        let activeScan = scanTask
        activeScan?.cancel()
        await activeScan?.value
        scanTask = nil
        for accountID in Array(runningAccountIDs) { await stopProtection(for: accountID) }
        if authRequestID != nil || authContext != nil || pendingAuthenticationAPI != nil {
            await cancelAuthentication()
        }
        isBusy = false
        for api in apis.values { await api.disconnect() }
        apis.removeAll()
        startingAccountIDs.removeAll()
        automaticProtectionRetryAt.removeAll()
        serviceStarted = false
        connectionMessage = "已停止"
    }

    func shutdown() {
        Task { await shutdownGracefully() }
    }

    private func rebuildStatus(accounts: [StoredAccount], activeAccountID: String?, expectedGeneration: Int) async throws {
        var summaries: [AccountSummary] = []
        for account in accounts {
            try Task.checkCancellation()
            guard serviceStarted, lifecycleGeneration == expectedGeneration else { return }
            let config = try await store.configuration(accountID: account.id)
            let records = try await store.blockRecords(accountID: account.id, limit: 500)
            guard serviceStarted, lifecycleGeneration == expectedGeneration else { return }
            summaries.append(AccountSummary(
                accountID: account.id,
                userID: config.userID,
                username: config.username,
                displayName: config.displayName,
                phoneMasked: account.phoneMasked,
                configured: config.userID != nil,
                blockedCount: config.blockedCount,
                kickedCount: config.kickedCount,
                recentBlockCount: records.count,
                whitelistCount: config.whitelist.count,
                blacklistCount: config.blacklist.count,
                learnedKeywordCount: config.learnedPatterns.keywords.count,
                lastScan: config.lastScan.map { ISO8601DateFormatter().string(from: $0) },
                running: runningAccountIDs.contains(account.id),
                ready: config.userID != nil && config.apiID != nil && !config.apiHash.isEmpty,
                state: runningAccountIDs.contains(account.id) ? "running" : (config.userID != nil ? "ready" : "logged_out"),
                error: nil
            ))
        }
        guard serviceStarted, lifecycleGeneration == expectedGeneration else { return }
        let fallback = AccountSummary(accountID: nil, userID: nil, username: "", displayName: "", phoneMasked: "", configured: false, blockedCount: 0, kickedCount: 0, recentBlockCount: 0, whitelistCount: 0, blacklistCount: 0, learnedKeywordCount: 0, lastScan: nil, running: false, ready: false, state: "empty", error: nil)
        let selected = summaries.first(where: { $0.accountID == activeAccountID }) ?? summaries.first ?? fallback
        status = CoreStatus(activeAccountID: selected.accountID, selectedAccount: selected, accounts: summaries, ocr: OCRService.status)
        startupEnabled = UserDefaults.standard.bool(forKey: "teleShield.startupEnabled")
    }

    private func startProtection(accountID: String) async throws {
        guard !runningAccountIDs.contains(accountID), startingAccountIDs.insert(accountID).inserted else { return }
        defer { startingAccountIDs.remove(accountID) }

        let generation = lifecycleGeneration
        guard serviceStarted else { return }
        let config = try await store.configuration(accountID: accountID)
        guard serviceStarted, lifecycleGeneration == generation else { return }
        let api = try await api(for: accountID)
        guard serviceStarted, lifecycleGeneration == generation else {
            removeAPIIfIdentical(api, accountID: accountID)
            return
        }
        do {
            try await api.connect()
        } catch {
            removeAPIIfIdentical(api, accountID: accountID)
            throw error
        }
        guard serviceStarted, lifecycleGeneration == generation else {
            await api.disconnect()
            removeAPIIfIdentical(api, accountID: accountID)
            return
        }
        let coordinator = ProtectionCoordinator(accountID: accountID, api: api, store: store, configuration: config)
        coordinators[accountID] = coordinator
        runningAccountIDs.insert(accountID)
        await coordinator.start { [weak self] message in
            Task { @MainActor [weak self] in
                self?.appendLog(message, level: "info")
                await self?.refreshAccountData()
            }
        }
        guard serviceStarted, lifecycleGeneration == generation, coordinators[accountID] === coordinator else {
            await coordinator.stop()
            if coordinators[accountID] === coordinator {
                coordinators.removeValue(forKey: accountID)
                runningAccountIDs.remove(accountID)
            }
            if apis[accountID] === api {
                await api.disconnect()
                removeAPIIfIdentical(api, accountID: accountID)
            }
            return
        }
        appendLog("已啟動 \(config.displayName.isEmpty ? accountID : config.displayName) 的原生背景防護", level: "info")
    }

    private func stopProtection(for accountID: String) async {
        await stopCoordinator(for: accountID)
        if let api = apis[accountID] { await api.disconnect() }
    }

    private func stopCoordinator(for accountID: String) async {
        await coordinators.removeValue(forKey: accountID)?.stop()
        runningAccountIDs.remove(accountID)
    }

    private func removeAPIIfIdentical(_ api: TelegramAPI, accountID: String) {
        guard let current = apis[accountID], current === api else { return }
        apis.removeValue(forKey: accountID)
    }

    private func api(for accountID: String) async throws -> TelegramAPI {
        if let api = apis[accountID] { return api }
        let config = try await store.configuration(accountID: accountID)
        guard let apiID = config.apiID, !config.apiHash.isEmpty else {
            throw CoreClientError(message: "請先設定 Telegram API ID、API Hash 並完成登入")
        }
        let session = try await store.loadSession(accountID: accountID)
        guard session != nil else { throw CoreClientError(message: "此帳號尚未完成 Telegram 登入") }
        let store = self.store
        let api = TelegramAPI(
            apiID: apiID,
            apiHash: config.apiHash,
            session: session,
            sessionDidChange: { updatedSession in
                try? await store.saveSession(updatedSession, accountID: accountID)
            }
        )
        apis[accountID] = api
        return api
    }

    private func completeAuthentication(context: AuthContext, user: NativeUser) async throws {
        guard authContextIsCurrent(context) else { throw CancellationError() }
        guard let session = await context.api.session() else { throw CoreClientError(message: "登入成功但缺少 auth key") }
        let saved = NativeSession(
            dcID: session.dcID,
            authKey: session.authKey,
            userID: user.id,
            serverSalt: session.serverSalt,
            date: Date()
        )
        try await store.saveSession(saved, accountID: context.accountID)
        try await store.updateAccount(context.accountID, user: user, phone: context.phone)
        guard authContextIsCurrent(context) else { throw CancellationError() }
        apis[context.accountID] = context.api
        authRequestID = nil
        authContext = nil
        authFlowID = nil
        authChallengeKind = nil
        authInProgress = false
        authenticatedAccountID = context.accountID
        connectionMessage = "Telegram 登入成功"
        appendLog("Telegram 登入成功：\(user.displayName)", level: "info")
        await refresh()
    }

    private func phoneCodeHash(from context: AuthContext) -> String {
        context.phoneCodeHash
    }

    private func authContextIsCurrent(_ context: AuthContext) -> Bool {
        authContext?.flowID == context.flowID && authInProgress
    }

    private func startAutomaticProtectionIfNeeded(expectedGeneration: Int? = nil) async {
        let generation = expectedGeneration ?? lifecycleGeneration
        guard serviceStarted, lifecycleGeneration == generation,
              let autoIDs = details?.autoStartAccountIDs else { return }
        let autoIDSet = Set(autoIDs)
        automaticProtectionRetryAt = automaticProtectionRetryAt.filter { autoIDSet.contains($0.key) }
        for accountID in autoIDs where !runningAccountIDs.contains(accountID) {
            guard serviceStarted, lifecycleGeneration == generation else { return }
            if let retryAt = automaticProtectionRetryAt[accountID], retryAt > Date() { continue }
            do {
                try await startProtection(accountID: accountID)
                automaticProtectionRetryAt.removeValue(forKey: accountID)
            } catch {
                appendLog("自動啟動 \(accountID) 失敗：\(error.localizedDescription)", level: "error")
                automaticProtectionRetryAt[accountID] = Date().addingTimeInterval(30)
            }
        }
    }

    private func runBusy(_ operation: String, _ action: @escaping () async throws -> Void) async {
        guard !isBusy else { return }
        isBusy = true
        busyOperation = operation
        defer { isBusy = false; busyOperation = nil }
        do { try await action() } catch { present(error: error) }
    }

    private func appendLog(_ message: String, level: String) {
        guard !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        lastLog = message
        eventLog.append(EventLogEntry(level: level, message: message))
        if eventLog.count > 300 { eventLog.removeFirst(eventLog.count - 300) }
    }

    private func csv(header: [String], rows: [[String]]) -> String {
        ([header] + rows).map { row in row.map(csvCell).joined(separator: ",") }.joined(separator: "\n") + "\n"
    }

    private func csvCell(_ value: String) -> String {
        let escaped = value.replacingOccurrences(of: "\"", with: "\"\"")
        return "\"\(escaped)\""
    }

    private func parseCSV(data: Data) -> [[String: String]] {
        guard var text = String(data: data, encoding: .utf8) else { return [] }
        if text.first == "\u{feff}" { text.removeFirst() }

        var rows: [[String]] = []
        var row: [String] = []
        var field = ""
        var inQuotes = false
        let characters = Array(text)
        var index = 0

        func finishField() {
            row.append(field)
            field.removeAll(keepingCapacity: true)
        }

        func finishRow() {
            finishField()
            if row.contains(where: { !$0.isEmpty }) { rows.append(row) }
            row.removeAll(keepingCapacity: true)
        }

        while index < characters.count {
            let character = characters[index]
            if inQuotes {
                if character == "\"" {
                    if index + 1 < characters.count, characters[index + 1] == "\"" {
                        field.append("\"")
                        index += 2
                    } else {
                        inQuotes = false
                        index += 1
                    }
                } else {
                    field.append(character)
                    index += 1
                }
                continue
            }

            switch character {
            case "\"":
                inQuotes = true
                index += 1
            case ",":
                finishField()
                index += 1
            case "\r":
                finishRow()
                index += 1
                if index < characters.count, characters[index] == "\n" { index += 1 }
            case "\n":
                finishRow()
                index += 1
            default:
                field.append(character)
                index += 1
            }
        }

        guard !inQuotes else { return [] }
        if !field.isEmpty || !row.isEmpty { finishRow() }
        guard let firstRow = rows.first else { return [] }
        let header = firstRow.map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
        guard !header.isEmpty else { return [] }
        return rows.dropFirst().map { values in
            var dictionary: [String: String] = [:]
            for (column, key) in header.enumerated() where !key.isEmpty {
                dictionary[key] = column < values.count ? values[column] : ""
            }
            return dictionary
        }
    }

    private func present(error: Error) {
        errorMessage = error.localizedDescription
        connectionMessage = "原生 Swift 服務發生錯誤"
        appendLog(error.localizedDescription, level: "error")
    }

    private func isSupportedListType(_ listType: String) -> Bool {
        guard listType == "whitelist" || listType == "blacklist" else {
            errorMessage = "不支援的名單類型：\(listType)"
            return false
        }
        return true
    }

}

private func normalizedTelegramUserID(_ value: String) throws -> String {
    let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
    let digits = trimmed.hasPrefix("-") ? String(trimmed.dropFirst()) : trimmed
    guard !digits.isEmpty,
          digits.allSatisfy({ $0 >= "0" && $0 <= "9" }),
          Int64(trimmed) != nil else {
        throw CoreClientError(message: "使用者 ID 必須是 numeric Telegram user ID")
    }
    return trimmed
}

private extension JSONEncoder {
    static var pretty: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        return encoder
    }
}

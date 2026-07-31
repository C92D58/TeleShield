import Foundation

enum ProtectionCoordinatorError: LocalizedError {
    case invalidScope(String)

    var errorDescription: String? {
        switch self {
        case .invalidScope(let scope):
            return "不支援的掃描範圍：\(scope)"
        }
    }
}

actor ProtectionCoordinator {
    private struct ProcessedMessageKey: Hashable {
        let peer: NativePeerIdentity
        let messageID: Int32
    }

    private struct ActionKey: Hashable {
        let peer: NativePeerIdentity
        let userID: Int64
    }

    private struct ChannelCandidate {
        let chat: NativeChat
        let initialPTS: Int32?
    }

    private struct ChannelSyncResult {
        let states: [Int64: NativeChannelUpdateState]
        let handledActionKeys: Set<ActionKey>
    }

    private let accountID: String
    private let api: TelegramAPI
    private let store: TeleShieldStore
    private var configuration: StoredConfiguration
    private var monitoringTask: Task<Void, Never>?
    private var processedMessageIDs = Set<ProcessedMessageKey>()
    private var warnedChannelIDs = Set<Int64>()

    init(accountID: String, api: TelegramAPI, store: TeleShieldStore, configuration: StoredConfiguration) {
        self.accountID = accountID
        self.api = api
        self.store = store
        self.configuration = configuration
    }

    func start(progress: @escaping @Sendable (String) -> Void) {
        guard monitoringTask == nil else { return }
        monitoringTask = Task { [weak self] in
            await self?.monitor(progress: progress)
        }
    }

    func stop() async {
        let task = monitoringTask
        monitoringTask = nil
        task?.cancel()
        await task?.value
        processedMessageIDs.removeAll(keepingCapacity: false)
        warnedChannelIDs.removeAll(keepingCapacity: false)
    }

    func scan(scope: String, dryRun: Bool, settings: ScanSettings, progress: @escaping @Sendable (String) -> Void) async throws -> NativeScanResult {
        let normalizedScope = try normalizedScope(scope)
        let settings = settings.normalized
        // Settings and lists can be edited while a coordinator is retained by
        // the app.  Read the actor-owned snapshot at operation start instead
        // of silently scanning with the configuration captured at launch.
        configuration = try await store.configuration(accountID: accountID)
        var findings: [ScanFinding] = []
        var matched = 0
        var acted = 0
        var dialogsScanned = 0
        var messagesScanned = 0
        var errors: [String] = []
        var handledActionKeys = Set<ActionKey>()
        // Request the selected scope's limit so a mixed Telegram dialog page
        // cannot consume private slots with group dialogs, or vice versa.
        let requestedLimit = normalizedScope == "private" ? settings.privateDialogLimit : settings.groupDialogLimit
        let page = try await api.getDialogs(limit: requestedLimit)
        var users = Dictionary(page.users.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        var chats = Dictionary(page.chats.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let rules = SpamRuleEngine(configuration: configuration)
        let contactIDs = try await api.contactUserIDs()

        let enabledGroupIDs = Set(configuration.managedGroups.filter(\.enabled).compactMap { Int64($0.groupID) })
        let hasManagedGroups = !configuration.managedGroups.isEmpty
        let candidateDialogs = page.dialogs.filter {
            matchesScope($0, scope: normalizedScope, enabledGroupIDs: enabledGroupIDs, hasManagedGroups: hasManagedGroups)
                && (!$0.isGroup || chats[peerID($0.peer)]?.adminRights == true)
        }
        let groupsFound = candidateDialogs.filter { $0.isGroup && !$0.isBroadcast }.count
        for dialog in candidateDialogs {
            if Task.isCancelled {
                return NativeScanResult(matched: matched, acted: acted, findings: findings, dialogsSeen: page.dialogs.count, dialogsScanned: dialogsScanned, groupsFound: groupsFound, messagesScanned: messagesScanned, errors: errors, cancelled: true, dryRun: dryRun)
            }
            dialogsScanned += 1
            progress("掃描 \(dialog.title)")
            do {
                let limit = dialog.isPrivate ? settings.privateMessageLimit : settings.groupMessageLimit
                let history = try await api.getHistory(peer: dialog.peer, limit: limit)
                merge(history: history, users: &users, chats: &chats)
                messagesScanned += history.messages.count
                for message in history.messages {
                    try Task.checkCancellation()
                    let days = dialog.isPrivate ? settings.privateDays : settings.groupDays
                    guard message.date >= Date().addingTimeInterval(-TimeInterval(days * 86_400)) else { continue }
                    guard let senderID = userSenderID(for: message),
                          let user = users[senderID],
                          !user.isSelf,
                          !user.isBot,
                          !contactIDs.contains(user.id),
                          let reason = try await evaluate(user: user, message: message, rules: rules) else { continue }
                    matched += 1
                    let group = dialog.isPrivate ? nil : dialog.title
                    findings.append(ScanFinding(userID: String(user.id), name: user.displayName, group: group, reason: reason))
                    if !dryRun {
                        let actionKey = ActionKey(peer: dialog.peer.stableID, userID: user.id)
                        if !handledActionKeys.contains(actionKey) {
                            if try await performAction(user: user, dialog: dialog, chats: chats, reason: reason) {
                                acted += 1
                            }
                            handledActionKeys.insert(actionKey)
                        }
                    }
                    // A private account is blocked after the first matching
                    // message. Continuing would repeat the same destructive
                    // action and create duplicate records.
                    if dialog.isPrivate { break }
                }
            } catch is CancellationError {
                return NativeScanResult(matched: matched, acted: acted, findings: findings, dialogsSeen: page.dialogs.count, dialogsScanned: dialogsScanned, groupsFound: groupsFound, messagesScanned: messagesScanned, errors: errors, cancelled: true, dryRun: dryRun)
            } catch {
                errors.append("\(dialog.title)：\(error.localizedDescription)")
            }
        }
        return NativeScanResult(matched: matched, acted: acted, findings: findings, dialogsSeen: page.dialogs.count, dialogsScanned: dialogsScanned, groupsFound: groupsFound, messagesScanned: messagesScanned, errors: errors, cancelled: Task.isCancelled, dryRun: dryRun)
    }

    private func monitor(progress: @escaping @Sendable (String) -> Void) async {
        // The worker is deliberately driven by Telegram's common update
        // cursor rather than a bounded dialog page. The cursor is persisted
        // only after the current batch has been handled; a failed destructive
        // action therefore leaves the batch eligible for a retry.
        var updateState: NativeUpdateState?
        var channelStates: [Int64: NativeChannelUpdateState]?
        while !Task.isCancelled {
            do {
                var handledActionKeys = Set<ActionKey>()
                let currentConfiguration: StoredConfiguration
                do {
                    currentConfiguration = try await store.configuration(accountID: accountID)
                } catch {
                    // A stale configuration must never be used for a
                    // destructive background action. Pause this poll and
                    // retry after the normal interval instead.
                    progress("背景防護暫停本輪：無法讀取帳號設定")
                    try? await Task.sleep(for: .seconds(15))
                    continue
                }
                // A coordinator is retained across UI refreshes.  Keep its
                // in-memory snapshot aligned with the actor-owned store so a
                // newly edited whitelist or group selection takes effect on
                // the next poll.
                configuration = currentConfiguration

                if channelStates == nil {
                    channelStates = try await store.loadChannelUpdateStates(accountID: accountID)
                }

                if updateState == nil {
                    if let stored = try await store.loadUpdateState(accountID: accountID) {
                        updateState = stored
                    } else {
                        // The first launch establishes a baseline and does
                        // not retroactively moderate history. Explicit scan
                        // remains the path for historical cleanup.
                        let baseline = try await api.getUpdateState()
                        try await store.saveUpdateState(baseline, accountID: accountID)
                        updateState = baseline
                        progress("背景防護已建立 Telegram updates cursor")
                    }
                }
                guard let cursor = updateState else { continue }

                let rules = SpamRuleEngine(configuration: currentConfiguration)
                // A failed contact refresh must not turn every unknown user
                // into an actionable candidate.
                let contactIDs: Set<Int64>
                do {
                    contactIDs = try await api.contactUserIDs()
                } catch {
                    progress("背景防護暫停本輪：無法讀取聯絡人清單")
                    try? await Task.sleep(for: .seconds(15))
                    continue
                }

                let difference = try await api.getUpdateDifference(from: cursor)
                if difference.didResetBaseline {
                    try await store.saveUpdateState(difference.state, accountID: accountID)
                    updateState = difference.state
                    progress("背景防護偵測到過大的 Telegram 更新缺口，已安全重新建立 cursor")
                    try? await Task.sleep(for: .seconds(15))
                    continue
                }

                let users = Dictionary(difference.users.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                let chats = Dictionary(difference.chats.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                handledActionKeys = try await processMessages(
                    difference.messages,
                    users: users,
                    chats: chats,
                    currentConfiguration: currentConfiguration,
                    contactIDs: contactIDs,
                    rules: rules,
                    handledActionKeys: handledActionKeys,
                    progress: progress
                )

                try Task.checkCancellation()
                try await store.saveUpdateState(difference.state, accountID: accountID)
                updateState = difference.state
                if processedMessageIDs.count > 10_000 { processedMessageIDs.removeAll(keepingCapacity: true) }

                // Channel/supergroup PTS is an independent update stream.
                // A failure here is isolated to that channel: the common
                // account cursor above is already committed, while each
                // channel cursor is committed only after its own actions.
                if currentConfiguration.listenScanGroups {
                    let channelResult = await syncConfiguredChannels(
                        currentConfiguration: currentConfiguration,
                        contactIDs: contactIDs,
                        rules: rules,
                        currentStates: channelStates ?? [:],
                        handledActionKeys: handledActionKeys,
                        progress: progress
                    )
                    channelStates = channelResult.states
                    handledActionKeys = channelResult.handledActionKeys
                }
            } catch is CancellationError {
                break
            } catch {
                progress("背景防護暫時無法同步：\(error.localizedDescription)")
            }
            try? await Task.sleep(for: .seconds(15))
        }
    }

    private func processMessages(
        _ messages: [NativeMessage],
        users: [Int64: NativeUser],
        chats: [Int64: NativeChat],
        currentConfiguration: StoredConfiguration,
        contactIDs: Set<Int64>,
        rules: SpamRuleEngine,
        handledActionKeys: Set<ActionKey>,
        progress: @escaping @Sendable (String) -> Void
    ) async throws -> Set<ActionKey> {
        var handled = handledActionKeys
        let enabledGroupIDs = Set(currentConfiguration.managedGroups.filter(\.enabled).compactMap { Int64($0.groupID) })
        let hasManagedGroups = !currentConfiguration.managedGroups.isEmpty
        let sortedMessages = messages.sorted {
            if $0.date == $1.date { return $0.id < $1.id }
            return $0.date < $1.date
        }

        for message in sortedMessages {
            try Task.checkCancellation()
            guard let dialog = dialog(for: message, users: users, chats: chats) else { continue }
            if dialog.isGroup {
                guard currentConfiguration.listenScanGroups,
                      matchesScope(dialog, scope: "group", enabledGroupIDs: enabledGroupIDs, hasManagedGroups: hasManagedGroups),
                      chats[peerID(dialog.peer)]?.adminRights == true else { continue }
            }
            let peerIdentity = dialog.peer.stableID
            let key = ProcessedMessageKey(peer: peerIdentity, messageID: message.id)
            guard !processedMessageIDs.contains(key) else { continue }
            guard let senderID = userSenderID(for: message),
                  let user = users[senderID],
                  !user.isSelf,
                  !user.isBot,
                  !contactIDs.contains(user.id) else {
                processedMessageIDs.insert(key)
                continue
            }
            guard let reason = try await evaluate(user: user, message: message, rules: rules) else {
                processedMessageIDs.insert(key)
                continue
            }
            let actionKey = ActionKey(peer: peerIdentity, userID: user.id)
            if handled.contains(actionKey) {
                processedMessageIDs.insert(key)
                continue
            }
            let acted = try await performAction(user: user, dialog: dialog, chats: chats, reason: reason)
            handled.insert(actionKey)
            if acted {
                progress("已處理 \(user.displayName)：\(reason)")
            }
            processedMessageIDs.insert(key)
        }
        return handled
    }

    private func syncConfiguredChannels(
        currentConfiguration: StoredConfiguration,
        contactIDs: Set<Int64>,
        rules: SpamRuleEngine,
        currentStates: [Int64: NativeChannelUpdateState],
        handledActionKeys: Set<ActionKey>,
        progress: @escaping @Sendable (String) -> Void
    ) async -> ChannelSyncResult {
        var states = currentStates
        var handled = handledActionKeys

        let page: TelegramDialogPage
        do {
            // One bounded dialog page is enough for the normal UI-managed
            // set.  Persisted channel cursors continue to work even when a
            // configured group is temporarily absent from this page.
            page = try await api.getDialogs(limit: 100)
        } catch {
            progress("群組背景防護暫停本輪：無法重新整理頻道清單")
            return ChannelSyncResult(states: states, handledActionKeys: handled)
        }

        let pageChats = Dictionary(page.chats.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let enabledGroupIDs = Set(currentConfiguration.managedGroups.filter(\.enabled).compactMap { Int64($0.groupID) })
        let hasManagedGroups = !currentConfiguration.managedGroups.isEmpty
        var candidates: [Int64: ChannelCandidate] = [:]

        for dialog in page.dialogs {
            guard dialog.isGroup,
                  !dialog.isBroadcast,
                  matchesScope(dialog, scope: "group", enabledGroupIDs: enabledGroupIDs, hasManagedGroups: hasManagedGroups),
                  case .channel(let channelID, _) = dialog.peer,
                  let chat = pageChats[channelID],
                  chat.isChannel,
                  !chat.isBroadcast,
                  chat.adminRights,
                  chat.accessHash != nil else { continue }
            candidates[channelID] = ChannelCandidate(chat: chat, initialPTS: dialog.channelPTS)
        }

        // Keep working with a previously discovered channel when Telegram's
        // current dialog page does not include it.  The persisted permission
        // is only used to preserve the candidate; performAction still asks
        // Telegram to confirm the target user is not an administrator before
        // every destructive group action.
        for group in currentConfiguration.managedGroups where group.enabled && group.isChannel && !group.isBroadcast {
            guard let channelID = Int64(group.groupID), candidates[channelID] == nil,
                  let accessHash = group.accessHash,
                  persistedAdminPermission(group.permission) else { continue }
            candidates[channelID] = ChannelCandidate(
                chat: NativeChat(
                    id: channelID,
                    accessHash: accessHash,
                    title: group.title,
                    username: group.username,
                    isChannel: true,
                    isBroadcast: false,
                    isMegagroup: true,
                    adminRights: true
                ),
                initialPTS: nil
            )
        }

        for channelID in candidates.keys.sorted() {
            guard !Task.isCancelled, let candidate = candidates[channelID] else { break }
            var state = states[channelID]
            do {
                if state == nil {
                    guard let initialPTS = candidate.initialPTS, initialPTS >= 0 else {
                        warnMissingChannelBaseline(channelID: channelID, title: candidate.chat.title, progress: progress)
                        continue
                    }
                    state = NativeChannelUpdateState(channelID: channelID, pts: initialPTS)
                    try await store.saveChannelUpdateState(state!, accountID: accountID)
                    progress("已建立群組 \(candidate.chat.title) 的 Telegram channel cursor")
                }
                guard let state else { continue }

                let difference = try await api.getChannelDifference(channel: candidate.chat, from: state.pts)
                if difference.didResetBaseline {
                    try await store.saveChannelUpdateState(difference.state, accountID: accountID)
                    states[channelID] = difference.state
                    progress("群組 \(candidate.chat.title) 的更新缺口過大，已安全重建 channel cursor")
                    continue
                }

                var chats = pageChats
                for chat in difference.chats { chats[chat.id] = chat }
                let users = Dictionary(difference.users.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
                handled = try await processMessages(
                    difference.messages,
                    users: users,
                    chats: chats,
                    currentConfiguration: currentConfiguration,
                    contactIDs: contactIDs,
                    rules: rules,
                    handledActionKeys: handled,
                    progress: progress
                )
                try Task.checkCancellation()
                try await store.saveChannelUpdateState(difference.state, accountID: accountID)
                states[channelID] = difference.state
            } catch is CancellationError {
                break
            } catch {
                progress("群組 \(candidate.chat.title) 同步暫停：\(error.localizedDescription)")
            }
        }

        return ChannelSyncResult(states: states, handledActionKeys: handled)
    }

    private func persistedAdminPermission(_ permission: String) -> Bool {
        let normalized = permission.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return normalized == "admin" || normalized == "administrator" || normalized == "creator" || normalized == "owner"
    }

    private func warnMissingChannelBaseline(
        channelID: Int64,
        title: String,
        progress: @escaping @Sendable (String) -> Void
    ) {
        guard warnedChannelIDs.insert(channelID).inserted else { return }
        progress("群組 \(title) 尚未取得 channel PTS，暫不追溯歷史訊息")
    }

    private func dialog(for message: NativeMessage, users: [Int64: NativeUser], chats: [Int64: NativeChat]) -> NativeDialog? {
        if let identity = message.peerIdentity {
            switch identity {
            case .user(let id):
                guard let user = users[id] else { return nil }
                return NativeDialog(
                    peer: .user(id: user.id, accessHash: user.accessHash),
                    title: user.displayName,
                    isPrivate: true,
                    isGroup: false,
                    isBroadcast: false,
                    channelPTS: nil
                )
            case .chat(let id):
                guard let chat = chats[id] else { return nil }
                return NativeDialog(
                    peer: .chat(id: id),
                    title: chat.title,
                    isPrivate: false,
                    isGroup: true,
                    isBroadcast: false,
                    channelPTS: nil
                )
            case .channel(let id):
                guard let chat = chats[id] else { return nil }
                return NativeDialog(
                    peer: .channel(id: id, accessHash: chat.accessHash),
                    title: chat.title,
                    isPrivate: false,
                    isGroup: true,
                    isBroadcast: chat.isBroadcast,
                    channelPTS: nil
                )
            }
        }
        if let user = users[message.peerID] {
            return NativeDialog(
                peer: .user(id: user.id, accessHash: user.accessHash),
                title: user.displayName,
                isPrivate: true,
                isGroup: false,
                isBroadcast: false,
                channelPTS: nil
            )
        }
        guard let chat = chats[message.peerID] else { return nil }
        let peer: NativePeer = chat.isChannel
            ? .channel(id: chat.id, accessHash: chat.accessHash)
            : .chat(id: chat.id)
        return NativeDialog(
            peer: peer,
            title: chat.title,
            isPrivate: false,
            isGroup: true,
            isBroadcast: chat.isBroadcast,
            channelPTS: nil
        )
    }

    private func userSenderID(for message: NativeMessage) -> Int64? {
        if let senderIdentity = message.senderIdentity {
            guard case .user(let id) = senderIdentity else { return nil }
            return id
        }
        return message.senderID
    }

    private func matchesScope(_ dialog: NativeDialog, scope: String, enabledGroupIDs: Set<Int64>, hasManagedGroups: Bool) -> Bool {
        switch scope {
        case "private": return dialog.isPrivate
        case "group", "groups":
            guard dialog.isGroup && !dialog.isBroadcast else { return false }
            return !hasManagedGroups || enabledGroupIDs.contains(peerID(dialog.peer))
        default: return true
        }
    }

    private func normalizedScope(_ scope: String) throws -> String {
        switch scope.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() {
        case "private": return "private"
        case "group", "groups": return "group"
        default: throw ProtectionCoordinatorError.invalidScope(scope)
        }
    }

    private func evaluate(user: NativeUser, message: NativeMessage, rules: SpamRuleEngine) async throws -> String? {
        if let reason = rules.evaluate(user: user, message: message) { return reason }
        guard let photo = message.photo, OCRService.status.available else { return nil }
        guard let imageData = try? await api.downloadPhoto(photo),
              let recognizedText = try? await OCRService.recognizeText(in: imageData) else { return nil }
        let text = recognizedText
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return nil }
        let ocrMessage = NativeMessage(
            id: message.id,
            peerID: message.peerID,
            senderID: message.senderID,
            date: message.date,
            text: text,
            hasPhoto: true,
            photo: photo,
            peerIdentity: message.peerIdentity,
            senderIdentity: message.senderIdentity
        )
        guard rules.evaluate(user: user, message: ocrMessage) != nil else { return nil }
        return "[OCR] \(String(text.prefix(120)))"
    }

    private func performAction(user: NativeUser, dialog: NativeDialog, chats: [Int64: NativeChat], reason: String) async throws -> Bool {
        if dialog.isPrivate {
            try await api.block(user: user)
        } else if let chat = chats[peerID(dialog.peer)], chat.adminRights {
            // If Telegram cannot confirm the participant role, fail closed and
            // skip the destructive action instead of risking an admin kick.
            let isAdministrator = try await api.isAdministrator(user, in: chat)
            guard !isAdministrator else { return false }
            try await api.kick(user: user, from: chat)
        } else {
            return false
        }
        let record = BlockRecord(
            time: ISO8601DateFormatter().string(from: Date()),
            source: dialog.isPrivate ? "private" : "group",
            userID: String(user.id),
            name: user.displayName,
            reason: reason
        )
        try await store.recordAction(record, accountID: accountID)
        return true
    }

    private func peerID(_ peer: NativePeer) -> Int64 {
        switch peer {
        case .user(let id, _), .chat(let id), .channel(let id, _): return id
        }
    }

    private func merge(history: TelegramHistoryPage, users: inout [Int64: NativeUser], chats: inout [Int64: NativeChat]) {
        for user in history.users { users[user.id] = user }
        for chat in history.chats { chats[chat.id] = chat }
    }

}

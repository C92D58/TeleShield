import Foundation

/// The common update cursor persisted for one Telegram authorization.
struct TelegramUpdateDifference: Sendable {
    let messages: [NativeMessage]
    let chats: [NativeChat]
    let users: [NativeUser]
    let state: NativeUpdateState
    /// `true` means Telegram returned an over-large gap and the cursor was
    /// re-baselined. Historical cleanup remains an explicit user action.
    let didResetBaseline: Bool
}

/// One response page from `updates.getDifference`.
///
/// Keeping the page boundary explicit makes the production loop and offline
/// XCTest fixtures exercise exactly the same response decoder. A slice is not
/// committed by the worker until the final page has been consumed.
enum TelegramUpdatePage: Sendable {
    case empty(date: Int32, seq: Int32)
    case difference(TelegramUpdateDifference)
    case slice(TelegramUpdateDifference)
    case tooLong(serverPTS: Int32)
}

/// A page from one channel/supergroup update box.  Channel PTS is independent
/// from the account-wide `updates.State.pts` and must be committed separately.
struct TelegramChannelDifference: Sendable {
    let messages: [NativeMessage]
    let chats: [NativeChat]
    let users: [NativeUser]
    let state: NativeChannelUpdateState
    let isFinal: Bool
    let didResetBaseline: Bool
}

enum TelegramChannelDifferencePage: Sendable {
    case empty(state: NativeChannelUpdateState, isFinal: Bool)
    case difference(TelegramChannelDifference)
    case tooLong(state: NativeChannelUpdateState)
}

enum TelegramUpdateError: LocalizedError {
    case unsupportedUpdate(Int32)
    case differenceLoopLimit
    case channelDifferenceLoopLimit

    var errorDescription: String? {
        switch self {
        case .unsupportedUpdate(let constructor):
            return "Telegram updates 回傳未支援的 constructor：\(constructor)"
        case .differenceLoopLimit:
            return "Telegram updates 差異同步超過安全分頁上限"
        case .channelDifferenceLoopLimit:
            return "Telegram 頻道 updates 差異同步超過安全分頁上限"
        }
    }
}

extension TelegramAPI {
    /// Fetches the initial common update state. The caller should persist this
    /// state before starting a long-running worker; it is a cursor, not a
    /// history query.
    func getUpdateState() async throws -> NativeUpdateState {
        var request = TLWriter()
        request.writeInt32(Int32(bitPattern: 0xedd4882a)) // updates.getState
        let response = try await call(request.data)
        return try parseUpdateStateResponse(response)
    }

    /// Offline-testable response boundary for `updates.getState`.
    func parseUpdateStateResponse(_ data: Data) throws -> NativeUpdateState {
        var reader = TLReader(data)
        let state = try readUpdateState(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return state
    }

    /// Reads all common updates between a persisted cursor and the current
    /// server state. Telegram may return `differenceSlice`; in that case the
    /// intermediate cursor is used for the next page and is not exposed until
    /// the final page has been consumed.
    func getUpdateDifference(from initialState: NativeUpdateState) async throws -> TelegramUpdateDifference {
        var cursor = initialState
        var messages: [NativeMessage] = []
        var chats: [NativeChat] = []
        var users: [NativeUser] = []

        for _ in 0..<16 {
            var request = TLWriter()
            request.writeInt32(Int32(bitPattern: 0x19c2f763)) // updates.getDifference
            request.writeInt32(0) // flags; use server defaults for optional limits
            request.writeInt32(cursor.pts)
            request.writeInt32(cursor.date)
            request.writeInt32(cursor.qts)

            let response = try await call(request.data)
            switch try parseUpdateDifferencePage(response) {
            case .empty(let date, let seq):
                cursor.date = date
                cursor.seq = seq
                return TelegramUpdateDifference(
                    messages: messages,
                    chats: chats,
                    users: users,
                    state: cursor,
                    didResetBaseline: false
                )

            case .difference(let page):
                return TelegramUpdateDifference(
                    messages: messages + page.messages,
                    chats: chats + page.chats,
                    users: users + page.users,
                    state: page.state,
                    didResetBaseline: false
                )

            case .slice(let page):
                messages.append(contentsOf: page.messages)
                chats.append(contentsOf: page.chats)
                users.append(contentsOf: page.users)
                cursor = page.state

            case .tooLong:
                let freshState = try await getUpdateState()
                return TelegramUpdateDifference(
                    messages: [],
                    chats: [],
                    users: [],
                    state: freshState,
                    didResetBaseline: true
                )
            }
        }

        throw TelegramUpdateError.differenceLoopLimit
    }

    /// Offline-testable response boundary for one `updates.getDifference`
    /// page. The network loop above is intentionally a thin state machine over
    /// this decoder so fixtures cannot drift away from production parsing.
    func parseUpdateDifferenceResponse(_ data: Data) throws -> TelegramUpdatePage {
        try parseUpdateDifferencePage(data)
    }

    /// Fetches the independent update stream for a channel or supergroup.
    /// Telegram recommends a small per-request limit for ordinary user
    /// clients; the loop continues until the server marks the result final.
    func getChannelDifference(
        channel: NativeChat,
        from initialPTS: Int32,
        limit: Int32 = 100
    ) async throws -> TelegramChannelDifference {
        guard channel.isChannel,
              !channel.isBroadcast,
              let accessHash = channel.accessHash,
              initialPTS >= 0 else {
            throw TelegramAPIError.invalidResponse
        }

        var pts = initialPTS
        var messages: [NativeMessage] = []
        var chats: [NativeChat] = []
        var users: [NativeUser] = []

        for _ in 0..<128 {
            var request = TLWriter()
            request.writeInt32(Int32(bitPattern: 0x03173d78)) // updates.getChannelDifference
            request.writeInt32(0) // force = false
            request.writeInt32(Int32(bitPattern: 0xf35aec28)) // inputChannel
            request.writeInt64(channel.id)
            request.writeInt64(accessHash)
            request.writeInt32(Int32(bitPattern: 0x94d42ee7)) // channelMessagesFilterEmpty
            request.writeInt32(pts)
            request.writeInt32(max(10, min(limit, 100)))

            let response = try await call(request.data)
            switch try parseChannelDifferencePage(response, channelID: channel.id) {
            case .empty(let state, let isFinal):
                return TelegramChannelDifference(
                    messages: messages,
                    chats: chats,
                    users: users,
                    state: state,
                    isFinal: isFinal,
                    didResetBaseline: false
                )

            case .difference(let page):
                messages.append(contentsOf: page.messages)
                chats.append(contentsOf: page.chats)
                users.append(contentsOf: page.users)
                pts = page.state.pts
                if page.isFinal {
                    return TelegramChannelDifference(
                        messages: messages,
                        chats: chats,
                        users: users,
                        state: NativeChannelUpdateState(channelID: channel.id, pts: pts),
                        isFinal: true,
                        didResetBaseline: false
                    )
                }

            case .tooLong(let state):
                return TelegramChannelDifference(
                    messages: [],
                    chats: [],
                    users: [],
                    state: state,
                    isFinal: true,
                    didResetBaseline: true
                )
            }
        }

        throw TelegramUpdateError.channelDifferenceLoopLimit
    }

    /// Offline-testable response boundary for one
    /// `updates.getChannelDifference` result.
    func parseChannelDifferenceResponse(
        _ data: Data,
        channelID: Int64
    ) throws -> TelegramChannelDifferencePage {
        try parseChannelDifferencePage(data, channelID: channelID)
    }

    private func parseUpdateDifferencePage(_ data: Data) throws -> TelegramUpdatePage {
        var reader = TLReader(data)
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x5d75a138: // updates.differenceEmpty
            let date = try reader.readInt32()
            let seq = try reader.readInt32()
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .empty(date: date, seq: seq)

        case 0xf49ca0: // updates.difference
            let page = try readDifferencePayload(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .difference(page)

        case 0xa8fb1981: // updates.differenceSlice
            let page = try readDifferencePayload(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .slice(page)

        case 0x4afe8f6d: // updates.differenceTooLong
            let serverPTS = try reader.readInt32()
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .tooLong(serverPTS: serverPTS)

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func parseChannelDifferencePage(
        _ data: Data,
        channelID: Int64
    ) throws -> TelegramChannelDifferencePage {
        var reader = TLReader(data)
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x3e11affb: // updates.channelDifferenceEmpty
            let flags = try reader.readInt32()
            let pts = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() } // timeout
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .empty(
                state: NativeChannelUpdateState(channelID: channelID, pts: pts),
                isFinal: flags & 1 != 0
            )

        case 0x2064674e: // updates.channelDifference
            let flags = try reader.readInt32()
            let pts = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() } // timeout
            let messages = try readMessageVector(&reader)
            let otherUpdates = try readOtherUpdateVector(&reader)
            let chats = try readChatVector(&reader)
            let users = try readUserVector(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .difference(TelegramChannelDifference(
                messages: messages + otherUpdates,
                chats: chats,
                users: users,
                state: NativeChannelUpdateState(channelID: channelID, pts: pts),
                isFinal: flags & 1 != 0,
                didResetBaseline: false
            ))

        case 0xa4bcc6fe: // updates.channelDifferenceTooLong
            let flags = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() } // timeout
            let latestPTS = try readChannelDifferenceDialogPTS(&reader)
            _ = try readMessageVector(&reader)
            _ = try readChatVector(&reader)
            _ = try readUserVector(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .tooLong(state: NativeChannelUpdateState(channelID: channelID, pts: latestPTS))

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func readChannelDifferenceDialogPTS(_ reader: inout TLReader) throws -> Int32 {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xd58a08c6 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try readPeer(&reader)
        _ = try reader.readInt32() // top_message
        _ = try reader.readInt32() // read_inbox_max_id
        _ = try reader.readInt32() // read_outbox_max_id
        _ = try reader.readInt32() // unread_count
        _ = try reader.readInt32() // unread_mentions_count
        _ = try reader.readInt32() // unread_reactions_count
        try skipPeerNotifySettingsForUpdates(&reader)
        guard flags & 1 != 0 else { throw TelegramAPIError.invalidResponse }
        let pts = try reader.readInt32()
        if flags & 2 != 0 { try skipDraftForUpdates(&reader) }
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 32 != 0 { _ = try reader.readInt32() }
        return pts
    }

    private func readDifferencePayload(_ reader: inout TLReader) throws -> TelegramUpdateDifference {
        let messages = try readMessageVector(&reader)
        let encryptedMessages = try readEncryptedMessageVector(&reader)
        let otherUpdates = try readOtherUpdateVector(&reader)
        let chats = try readChatVector(&reader)
        let users = try readUserVector(&reader)
        let state = try readUpdateState(&reader)
        return TelegramUpdateDifference(
            messages: messages + encryptedMessages + otherUpdates,
            chats: chats,
            users: users,
            state: state,
            didResetBaseline: false
        )
    }

    private func readUpdateState(_ reader: inout TLReader) throws -> NativeUpdateState {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xa56c2a3e else {
            throw TelegramAPIError.invalidResponse
        }
        return NativeUpdateState(
            pts: try reader.readInt32(),
            qts: try reader.readInt32(),
            date: try reader.readInt32(),
            seq: try reader.readInt32(),
            unreadCount: try reader.readInt32()
        )
    }

    private func readEncryptedMessageVector(_ reader: inout TLReader) throws -> [NativeMessage] {
        try reader.readVector { reader in
            try skipEncryptedMessage(&reader)
            return Optional<NativeMessage>.none
        }.compactMap { $0 }
    }

    private func skipEncryptedMessage(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xed18c118: // encryptedMessage
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readBytes()
            try skipEncryptedFile(&reader)
        case 0x23734b06: // encryptedMessageService
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readBytes()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipEncryptedFile(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xc21f497e: // encryptedFileEmpty
            return
        case 0xa8008cd8: // encryptedFile
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func readOtherUpdateVector(_ reader: inout TLReader) throws -> [NativeMessage] {
        try reader.readVector { reader in try readOtherUpdate(&reader) }.compactMap { $0 }
    }

    /// Consume updates that are not represented by the `new_messages` vector.
    /// New-message constructors are returned so an unusual server response
    /// cannot silently drop a moderation candidate. Unsupported constructors
    /// fail closed and leave the persisted cursor unchanged.
    private func readOtherUpdate(_ reader: inout TLReader) throws -> NativeMessage? {
        let constructor = try reader.readInt32()
        switch UInt32(bitPattern: constructor) {
        case 0x1f2b0afd, 0x62ba04d9: // updateNewMessage / updateNewChannelMessage
            let message = try readMessage(&reader)
            _ = try reader.readInt32() // pts
            _ = try reader.readInt32() // pts_count
            return message

        case 0x39a51dfb: // updateNewScheduledMessage
            _ = try readMessage(&reader)
            return nil

        case 0x2a17bf5c: // updateUserTyping
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            try skipSendMessageAction(&reader)
            return nil

        case 0x83487af0: // updateChatUserTyping
            _ = try reader.readInt64()
            _ = try readPeer(&reader)
            try skipSendMessageAction(&reader)
            return nil

        case 0x8c88c923: // updateChannelUserTyping
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            _ = try readPeer(&reader)
            try skipSendMessageAction(&reader)
            return nil

        case 0x8951abef: // updateNewAuthorization
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 {
                _ = try reader.readInt32()
                _ = try reader.readString()
                _ = try reader.readString()
            }
            return nil

        case 0x12bcbd9a: // updateNewEncryptedMessage
            try skipEncryptedMessage(&reader)
            _ = try reader.readInt32()
            return nil

        case 0x1710f156: // updateEncryptedChatTyping
            _ = try reader.readInt32()
            return nil

        case 0x38fe25b7: // updateEncryptedMessagesRead
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xe40370a3, 0x1b3f4df7: // updateEditMessage / updateEditChannelMessage
            _ = try readMessage(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x4e90bfd6: // updateMessageID
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            return nil

        case 0xa20db0e5: // updateDeleteMessages
            _ = try reader.readVector { reader in try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xc32d5b12: // updateDeleteChannelMessages
            _ = try reader.readInt64()
            _ = try reader.readVector { reader in try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x9e84bc99: // updateReadHistoryInbox
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            _ = try readPeer(&reader)
            if flags & 2 != 0 { _ = try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x2f2f21bf: // updateReadHistoryOutbox
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x8e5e9873: // updateDcOptions
            _ = try reader.readVector { reader in
                try skipDCOptionForUpdates(&reader)
                return ()
            }
            return nil

        case 0xee3b272a: // updatePrivacy
            try skipPrivacyKeyForUpdates(&reader)
            try skipPrivacyRulesForUpdates(&reader)
            return nil

        case 0xf8227181: // updateReadMessagesContents
            let flags = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            return nil

        case 0x922e6e10: // updateReadChannelInbox
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xb75f99a9: // updateReadChannelOutbox
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            return nil

        case 0xd6b19546: // updateReadChannelDiscussionInbox
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 {
                _ = try reader.readInt64()
                _ = try reader.readInt32()
            }
            return nil

        case 0x695c9e7c: // updateReadChannelDiscussionOutbox
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x25f324f7: // updateChannelReadMessagesContents
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try readPeer(&reader) }
            _ = try reader.readVector { reader in try reader.readInt32() }
            return nil

        case 0x108d941f: // updateChannelTooLong
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            return nil

        case 0x635b4c09: // updateChannel
            _ = try reader.readInt64()
            return nil

        case 0xb23fc698: // updateChannelAvailableMessages
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            return nil

        case 0xf89a6a4e: // updateChat
            _ = try reader.readInt64()
            return nil

        case 0xf226ac08: // updateChannelMessageViews
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xd29a27f4: // updateChannelMessageForwards
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x871fb939: // updateGeoLiveViewed
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            return nil

        case 0x2661bf09: // updatePhoneCallSignalingData
            _ = try reader.readInt64()
            _ = try reader.readBytes()
            return nil

        case 0x05492a13: // updateUserPhone
            _ = try reader.readInt64()
            _ = try reader.readString()
            return nil

        case 0xa7848924: // updateUserName
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = try reader.readString()
            _ = try reader.readVector { reader in try skipUsername(&reader) }
            return nil

        case 0xe5bdf8de: // updateUserStatus
            _ = try reader.readInt64()
            try skipUserStatus(&reader)
            return nil

        case 0x2052945d: // updateUser
            _ = try reader.readInt64()
            return nil

        case 0x28373599: // updateUserEmojiStatus
            _ = try reader.readInt64()
            try skipEmojiStatus(&reader)
            return nil

        case 0xebe07752: // updatePeerBlocked
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readBool() }
            if flags & 2 != 0 { _ = try reader.readBool() }
            _ = try readPeer(&reader)
            return nil

        case 0xbb9bb9a5: // updatePeerHistoryTTL
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            if flags & 1 != 0 { _ = try reader.readInt32() }
            return nil

        case 0xc4870a49: // updateBotStopped
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readBool()
            _ = try reader.readInt32()
            return nil

        case 0x7b68920: // updateChannelViewForumAsMessages
            _ = try reader.readInt64()
            _ = try reader.readBool()
            return nil

        case 0x6a7e7366: // updatePeerSettings
            _ = try readPeer(&reader)
            try skipPeerSettingsForUpdates(&reader)
            return nil

        case 0x19360dc0: // updateFolderPeers
            _ = try reader.readVector { reader in
                _ = try readPeer(&reader)
                _ = try reader.readInt32()
                return ()
            }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xed85eab5: // updatePinnedMessages
            _ = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readVector { reader in try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x6e6fe51c: // updateDialogPinned
            let flags = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() }
            try skipDialogPeer(&reader)
            return nil

        case 0xfa0f3ca2: // updatePinnedDialogs
            let flags = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() }
            if flags & 1 != 0 {
                _ = try reader.readVector { reader in
                    try skipDialogPeer(&reader)
                    return ()
                }
            }
            return nil

        case 0x5bb98608: // updatePinnedChannelMessages
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readVector { reader in try reader.readInt32() }
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xb658f23e: // updateDialogUnreadMark
            let flags = try reader.readInt32()
            try skipDialogPeer(&reader)
            if flags & 2 != 0 { _ = try readPeer(&reader) }
            return nil

        case 0x07761198: // updateChatParticipants
            try skipChatParticipants(&reader)
            return nil

        case 0x3dda5451: // updateChatParticipantAdd
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xe32f3d77: // updateChatParticipantDelete
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            return nil

        case 0xd7ca61a2: // updateChatParticipantAdmin
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readBool()
            _ = try reader.readInt32()
            return nil

        case 0xbec268ef: // updateNotifySettings
            _ = try readNotifyPeer(&reader)
            try skipPeerNotifySettingsForUpdates(&reader)
            return nil

        case 0xebe46819: // updateServiceNotification
            let flags = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() } // inbox_date
            _ = try reader.readString()
            _ = try reader.readString()
            try skipMessageMediaForUpdates(&reader)
            _ = try reader.readVector { reader in try skipMessageEntityForUpdates(&reader) }
            return nil

        case 0xedfc111e: // updateDraftMessage
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try readPeer(&reader) }
            try skipDraftForUpdates(&reader)
            return nil

        case 0x7f891213: // updateWebPage
            try skipWebPageForUpdates(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0xa229dd06: // updateConfig
            return nil

        case 0x3354678f: // updatePtsChanged
            return nil

        case 0x9375341e, 0x7084a7be, 0x564fe691, 0x571d2742,
             0x9a422c20, 0xe511996d, 0x3504914f, 0x74d8be99,
             0xfb4c496c, 0x30f443db, 0x6f7863f4, 0xec05b097,
             0x39c67432, 0xac072444: // cache/settings invalidation updates
            return nil

        case 0x31c24808: // updateStickerSets
            _ = try reader.readInt32() // masks/emojis flags
            return nil

        case 0x0bb2d201: // updateStickerSetsOrder
            let flags = try reader.readInt32()
            _ = flags
            _ = try reader.readVector { reader in try reader.readInt64() }
            return nil

        case 0xa5d72105: // updateDialogFilterOrder
            _ = try reader.readVector { reader in try reader.readInt32() }
            return nil

        case 0xf16269d4: // updateSmsJob
            _ = try reader.readString()
            return nil

        case 0x46560264: // updateLangPackTooLong
            _ = try reader.readString()
            return nil

        case 0xaca1657b: // updateMessagePoll
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { try skipPollForUpdates(&reader) }
            try skipPollResultsForUpdates(&reader)
            return nil

        case 0x24f40e77: // updateMessagePollVote
            _ = try reader.readInt64()
            _ = try readPeer(&reader)
            _ = try reader.readVector { reader in try reader.readBytes() }
            _ = try reader.readInt32()
            return nil

        case 0x84cd5a: // updateTranscribedAudio
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = flags
            return nil

        case 0xf74e932b: // updateReadStories
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            return nil

        case 0x1e297bfa: // updateMessageReactions
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try readPeer(&reader) }
            try skipMessageReactionsForUpdates(&reader)
            return nil

        case 0xd5a41724: // updateMessageExtendedMedia
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in
                try skipExtendedMediaForUpdates(&reader)
                return ()
            }
            return nil

        case 0x75b3b798: // updateStory
            _ = try readPeer(&reader)
            try skipStoryItemForUpdates(&reader)
            return nil

        case 0x1bf335b9: // updateStoryID
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            return nil

        case 0x7d627683: // updateSentStoryReaction
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            try skipReactionForUpdates(&reader)
            return nil

        case 0x1824e40b: // updateNewStoryReaction
            _ = try reader.readInt32()
            _ = try readPeer(&reader)
            try skipReactionForUpdates(&reader)
            return nil

        case 0x7063c3db: // updatePendingJoinRequests
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt64() }
            return nil

        case 0x17b7a20b: // updateAttachMenuBots
            return nil

        case 0x0b783982: // updateGroupCallConnection
            let flags = try reader.readInt32()
            _ = flags // presentation is represented by a flag-only Bool
            try skipDataJSON(&reader)
            return nil

        case 0xa477288f: // updateGroupCallChainBlocks
            try skipInputGroupCallForUpdates(&reader)
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readBytes() }
            _ = try reader.readInt32()
            return nil

        case 0xc957a766: // updateGroupCallEncryptedMessage
            try skipInputGroupCallForUpdates(&reader)
            _ = try readPeer(&reader)
            _ = try reader.readBytes()
            return nil

        case 0x1592b79d: // updateWebViewResultSent
            _ = try reader.readInt64()
            return nil

        case 0x26ffde7d: // updateDialogFilter
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { try skipDialogFilterForUpdates(&reader) }
            return nil

        case 0x20529438: // updateUser (current spelling of updateUser)
            _ = try reader.readInt64()
            return nil

        case 0x86fccf85: // updateMoveStickerSetToTop
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            return nil

        case 0xaeaf9e74: // updateSavedDialogPinned
            _ = try reader.readInt32()
            try skipDialogPeer(&reader)
            return nil

        case 0x686c85a6: // updatePinnedSavedDialogs
            let flags = try reader.readInt32()
            if flags & 1 != 0 {
                _ = try reader.readVector { reader in
                    try skipDialogPeer(&reader)
                    return ()
                }
            }
            return nil

        case 0xae3f101d: // updatePeerWallpaper
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            if flags & 1 != 0 { try skipWallPaperForUpdates(&reader) }
            return nil

        case 0x2f2ba99f: // updateChannelWebPage
            _ = try reader.readInt64()
            try skipWebPageForUpdates(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            return nil

        case 0x54c01850: // updateChatDefaultBannedRights
            _ = try readPeer(&reader)
            try skipChatBannedRightsForUpdates(&reader)
            _ = try reader.readInt32()
            return nil

        case 0xf2a71983: // updateDeleteScheduledMessages
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readVector { reader in try reader.readInt32() }
            if flags & 1 != 0 { _ = try reader.readVector { reader in try reader.readInt32() } }
            return nil

        case 0xd087663a: // updateChatParticipant
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            if flags & 1 != 0 { try skipChatParticipant(&reader) }
            if flags & 2 != 0 { try skipChatParticipant(&reader) }
            if flags & 4 != 0 { try skipExportedChatInviteForUpdates(&reader) }
            _ = try reader.readInt32()
            return nil

        case 0x985d3abb: // updateChannelParticipant
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            if flags & 1 != 0 { try skipChannelParticipantForUpdates(&reader) }
            if flags & 2 != 0 { try skipChannelParticipantForUpdates(&reader) }
            if flags & 4 != 0 { try skipExportedChatInviteForUpdates(&reader) }
            _ = try reader.readInt32()
            return nil

        case 0x4d712f2e: // updateBotCommands
            _ = try readPeer(&reader)
            _ = try reader.readInt64()
            _ = try reader.readVector { reader in
                _ = try reader.readInt32()
                _ = try reader.readString()
                _ = try reader.readString()
                return ()
            }
            return nil

        case 0xac21d3ce: // updateBotMessageReaction
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readVector { reader in
                try skipReactionForUpdates(&reader)
                return ()
            }
            _ = try reader.readVector { reader in
                try skipReactionForUpdates(&reader)
                return ()
            }
            _ = try reader.readInt32()
            return nil

        case 0x09cb7759: // updateBotMessageReactions
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in
                try skipReactionCountForUpdates(&reader)
                return ()
            }
            _ = try reader.readInt32()
            return nil

        case 0x4e80a379: // updateStarsBalance
            try skipStarsAmountForUpdates(&reader)
            return nil

        case 0x283bd312: // updateBotPurchasedPaidMedia
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = try reader.readInt32()
            return nil

        case 0x77b0e372: // updateReadMonoForumInbox
            _ = try reader.readInt64()
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            return nil

        case 0xa4a79376: // updateReadMonoForumOutbox
            _ = try reader.readInt64()
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            return nil

        case 0x9f812b08: // updateMonoForumNoPaidException
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = flags // exception is a flag-only Bool
            _ = try readPeer(&reader)
            return nil

        case 0x3e85e92c: // updateDeleteGroupCallMessages
            try skipInputGroupCallForUpdates(&reader)
            _ = try reader.readVector { reader in try reader.readInt32() }
            return nil

        case 0x683b2c52: // updatePinnedForumTopic
            let flags = try reader.readInt32()
            _ = flags // pinned is flag-only
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            return nil

        case 0xdef143d0: // updatePinnedForumTopics
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            if flags & 1 != 0 {
                _ = try reader.readVector { reader in try reader.readInt32() }
            }
            return nil

        case 0xbd8367b9: // updateChatParticipantRank
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = try reader.readInt32()
            return nil

        default:
            throw TelegramUpdateError.unsupportedUpdate(constructor)
        }
    }

    private func skipSendMessageAction(_ reader: inout TLReader) throws {
        let constructor = UInt32(bitPattern: try reader.readInt32())
        switch constructor {
        case 0x16bf744e, 0xfd5ec8f5, 0xa187d66f, 0xd52f73f7,
             0x176f8ba1, 0x628cbc6f, 0xdd6a8f48, 0x88f27fbc,
             0xd92c2285, 0xb05ac6b1:
            return

        case 0xe9763aec, 0xf351d7ab, 0xd1d34a26, 0xaa0cd9e4,
             0x243e1c66, 0xdbda9246:
            _ = try reader.readInt32() // progress

        case 0x25972bcb: // sendMessageEmojiInteraction
            _ = try reader.readString()
            _ = try reader.readInt32()
            try skipDataJSON(&reader)

        case 0xb665902e: // sendMessageEmojiInteractionSeen
            _ = try reader.readString()

        case 0x376d975c: // sendMessageTextDraftAction
            _ = try reader.readInt64()
            try skipTextWithEntitiesForUpdates(&reader)

        default:
            throw TelegramUpdateError.unsupportedUpdate(Int32(bitPattern: constructor))
        }
    }

    private func skipDCOptionForUpdates(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x18b7a10d else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt32()
        _ = try reader.readString()
        _ = try reader.readInt32()
        if flags & (1 << 10) != 0 { _ = try reader.readBytes() }
    }

    private func skipPrivacyKeyForUpdates(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xbc2eab30, 0x500e6dfa, 0x3d662b7b, 0x39491cc8,
             0x69ec56a3, 0x96151fed, 0xd19ae46d, 0x42ffd42b,
             0x0697f414, 0xa486b761, 0x2000a518, 0x2ca4fdf8,
             0x17d348d2, 0xff7a571b:
            return
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipDialogFilterForUpdates(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xaa472651: // dialogFilter
            let flags = try reader.readInt32()
            try skipTextWithEntitiesForUpdates(&reader)
            if flags & (1 << 25) != 0 { _ = try reader.readString() }
            if flags & (1 << 27) != 0 { _ = try reader.readInt32() }
            for _ in 0..<3 {
                _ = try reader.readVector { reader in
                    try skipInputPeerForUpdates(&reader)
                    return ()
                }
            }

        case 0x363293ae: // dialogFilterDefault
            return

        case 0x96537bd7: // dialogFilterChatlist
            let flags = try reader.readInt32()
            try skipTextWithEntitiesForUpdates(&reader)
            if flags & (1 << 25) != 0 { _ = try reader.readString() }
            if flags & (1 << 27) != 0 { _ = try reader.readInt32() }
            for _ in 0..<2 {
                _ = try reader.readVector { reader in
                    try skipInputPeerForUpdates(&reader)
                    return ()
                }
            }

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipDataJSON(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x7d748d04 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readString()
    }

    private func skipDialogPeer(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xe56dbf05: // dialogPeer
            _ = try readPeer(&reader)
        case 0x514519e2: // dialogPeerFolder
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipPeerSettingsForUpdates(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xf47741f7 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & (1 << 6) != 0 { _ = try reader.readInt32() }
        if flags & (1 << 9) != 0 {
            _ = try reader.readString()
            _ = try reader.readInt32()
        }
        if flags & (1 << 13) != 0 {
            _ = try reader.readInt64()
            _ = try reader.readString()
        }
        if flags & (1 << 14) != 0 { _ = try reader.readInt64() }
        if flags & (1 << 15) != 0 { _ = try reader.readString() }
        if flags & (1 << 16) != 0 { _ = try reader.readString() }
        if flags & (1 << 17) != 0 { _ = try reader.readInt32() }
        if flags & (1 << 18) != 0 { _ = try reader.readInt32() }
    }

    private func skipChatAdminRightsForUpdates(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x5fb224d5 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readInt32()
    }

    private func skipChatBannedRightsForUpdates(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x9f120418 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readInt32()
        _ = try reader.readInt32()
    }

    private func skipStarsSubscriptionPricingForUpdates(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x05416d58 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readInt32()
        _ = try reader.readInt64()
    }

    private func skipExportedChatInviteForUpdates(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xed107ab7: // chatInvitePublicJoinRequests
            return
        case 0xa22cbd96: // chatInviteExported
            let flags = try reader.readInt32()
            _ = try reader.readString()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            if flags & (1 << 4) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 1) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 2) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 3) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 7) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 10) != 0 { _ = try reader.readInt32() }
            if flags & (1 << 8) != 0 { _ = try reader.readString() }
            if flags & (1 << 9) != 0 { try skipStarsSubscriptionPricingForUpdates(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipChannelParticipantForUpdates(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x1bd54456: // channelParticipant
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 4 != 0 { _ = try reader.readString() }

        case 0xa9478a1a: // channelParticipantSelf
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() }
            if flags & 4 != 0 { _ = try reader.readString() }

        case 0x2fe601d3: // channelParticipantCreator
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            try skipChatAdminRightsForUpdates(&reader)
            if flags & 1 != 0 { _ = try reader.readString() }

        case 0x34c3bb53: // channelParticipantAdmin
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 2 != 0 { _ = try reader.readInt64() }
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            try skipChatAdminRightsForUpdates(&reader)
            if flags & 4 != 0 { _ = try reader.readString() }

        case 0xd5f0ad91: // channelParticipantBanned
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            try skipChatBannedRightsForUpdates(&reader)
            if flags & 4 != 0 { _ = try reader.readString() }

        case 0x1b03f006: // channelParticipantLeft
            _ = try readPeer(&reader)

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipUsername(_ reader: inout TLReader) throws {
        _ = try reader.readInt32() // flags
        _ = try reader.readString()
    }

    private func skipUserStatus(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x09d05049: // userStatusEmpty
            return
        case 0xedb93949: // userStatusOnline
            _ = try reader.readInt32()
        case 0x008c703f: // userStatusOffline
            _ = try reader.readInt32()
        case 0x7b197dc8: // userStatusRecently
            let flags = try reader.readInt32()
            _ = flags
        case 0x541a1d1a: // userStatusLastWeek
            _ = try reader.readInt32()
        case 0x65899777: // userStatusLastMonth
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipEmojiStatus(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x2de11aae: // emojiStatusEmpty
            return
        case 0xe7ff068a: // emojiStatus
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case 0x7184603b: // emojiStatusCollectible
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = try reader.readString()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipChatParticipants(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x8763d3e1: // chatParticipantsForbidden
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { try skipChatParticipant(&reader) }
        case 0x3cbc93f8: // chatParticipants
            _ = try reader.readInt64()
            _ = try reader.readVector { reader in try skipChatParticipant(&reader) }
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipChatParticipant(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x38e79fde: // chatParticipant
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
        case 0xe1f867b8: // chatParticipantCreator
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
        case 0x0360d5d2: // chatParticipantAdmin
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func readNotifyPeer(_ reader: inout TLReader) throws -> NativePeer {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x9fd40bd8: // notifyPeer
            return try readPeer(&reader)
        case 0xb4c83b4c: // notifyUsers
            return .user(id: 0, accessHash: nil)
        case 0xc007cec3: // notifyChats
            return .chat(id: 0)
        case 0xd612e8ef: // notifyBroadcasts
            return .channel(id: 0, accessHash: nil)
        case 0x226e6308: // notifyForumTopic
            let peer = try readPeer(&reader)
            _ = try reader.readInt32()
            return peer
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
}

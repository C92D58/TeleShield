import Foundation

// Narrow parser bridges used by TelegramUpdates.swift. The large TL surface
// remains internal to this package and is not part of the app-facing contract.
extension TelegramAPI {
    func skipPeerNotifySettingsForUpdates(_ reader: inout TLReader) throws {
        try skipPeerNotifySettings(&reader)
    }

    func skipMessageMediaForUpdates(_ reader: inout TLReader) throws {
        _ = try skipMessageMedia(&reader)
    }

    func skipMessageEntityForUpdates(_ reader: inout TLReader) throws {
        try skipMessageEntity(&reader)
    }

    func skipWebPageForUpdates(_ reader: inout TLReader) throws {
        try skipWebPage(&reader)
    }

    func skipReactionForFiles(_ reader: inout TLReader) throws {
        try skipReaction(&reader)
    }

    func skipTextWithEntitiesForUpdates(_ reader: inout TLReader) throws {
        _ = try reader.readString()
        _ = try reader.readVector { reader in
            try skipMessageEntity(&reader)
            return ()
        }
    }

    func skipDraftForUpdates(_ reader: inout TLReader) throws {
        try skipDraft(&reader)
    }

    func skipInputPeerForUpdates(_ reader: inout TLReader) throws {
        try skipInputPeer(&reader)
    }

    func skipPrivacyRulesForUpdates(_ reader: inout TLReader) throws {
        try skipPrivacyRules(&reader)
    }

    func skipPollForUpdates(_ reader: inout TLReader) throws {
        try skipPoll(&reader)
    }

    func skipPollResultsForUpdates(_ reader: inout TLReader) throws {
        try skipPollResults(&reader)
    }

    func skipInputGroupCallForUpdates(_ reader: inout TLReader) throws {
        try skipInputGroupCall(&reader)
    }

    func skipMessageReactionsForUpdates(_ reader: inout TLReader) throws {
        try skipMessageReactions(&reader)
    }

    func skipReactionForUpdates(_ reader: inout TLReader) throws {
        try skipReaction(&reader)
    }

    func skipReactionCountForUpdates(_ reader: inout TLReader) throws {
        try skipReactionCount(&reader)
    }

    func skipExtendedMediaForUpdates(_ reader: inout TLReader) throws {
        try skipExtendedMedia(&reader)
    }

    func skipStoryItemForUpdates(_ reader: inout TLReader) throws {
        try skipStoryItem(&reader)
    }

    func skipWallPaperForUpdates(_ reader: inout TLReader) throws {
        try skipWallPaper(&reader)
    }

    func skipChatThemeForUpdates(_ reader: inout TLReader) throws {
        try skipChatTheme(&reader)
    }

    func skipStarsAmountForUpdates(_ reader: inout TLReader) throws {
        try skipStarsAmount(&reader)
    }

    func skipPaymentRequestedInfoForUpdates(_ reader: inout TLReader) throws {
        try skipPaymentRequestedInfo(&reader)
    }
}

extension TelegramAPI {
    func basicGroupAdministratorStatus(userID: Int64, chatID: Int64) async throws -> Bool {
        var request = TLWriter()
        request.writeInt32(Int32(bitPattern: 0xaeb00b34)) // messages.getFullChat
        request.writeInt64(chatID)
        let response = try await call(request.data)
        var reader = TLReader(response)
        guard try reader.readInt32() == Int32(bitPattern: 0xe5d7d19c) else {
            throw TelegramAPIError.invalidResponse
        }
        guard try reader.readInt32() == Int32(bitPattern: 0x2633421b) else {
            throw TelegramAPIError.invalidResponse
        }
        let fullChatFlags = try reader.readInt32()
        _ = try reader.readInt64()
        _ = try reader.readString()
        guard try reader.readInt32() == Int32(bitPattern: 0x3cbc93f8) else {
            // A forbidden participant list cannot prove that the target is
            // removable; the caller will skip the destructive action.
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readInt64() // chat_id
        var isAdministrator = false
        _ = try reader.readVector { reader in
            switch try reader.readInt32() {
            case Int32(bitPattern: 0x38e79fde): // chatParticipant
                let flags = try reader.readInt32()
                _ = try reader.readInt64()
                _ = try reader.readInt64()
                _ = try reader.readInt32()
                if flags & 1 != 0 { _ = try reader.readString() }
            case Int32(bitPattern: 0xe1f867b8): // chatParticipantCreator
                let flags = try reader.readInt32()
                let memberID = try reader.readInt64()
                if flags & 1 != 0 { _ = try reader.readString() }
                if memberID == userID { isAdministrator = true }
            case Int32(bitPattern: 0x360d5d2): // chatParticipantAdmin
                let flags = try reader.readInt32()
                let memberID = try reader.readInt64()
                _ = try reader.readInt64()
                _ = try reader.readInt32()
                if flags & 1 != 0 { _ = try reader.readString() }
                if memberID == userID { isAdministrator = true }
            default:
                throw TelegramAPIError.invalidResponse
            }
            return ()
        }
        _ = try reader.readInt32() // participants version
        if fullChatFlags & 4 != 0 { try skipChatPhoto(&reader) }
        try skipPeerNotifySettings(&reader)
        if fullChatFlags & (1 << 13) != 0 { try skipExportedChatInvite(&reader) }
        if fullChatFlags & (1 << 3) != 0 {
            _ = try reader.readVector { reader in
                try skipBotInfo(&reader)
                return ()
            }
        }
        if fullChatFlags & (1 << 6) != 0 { _ = try reader.readInt32() }
        if fullChatFlags & (1 << 11) != 0 { _ = try reader.readInt32() }
        if fullChatFlags & (1 << 12) != 0 { try skipInputGroupCall(&reader) }
        if fullChatFlags & (1 << 14) != 0 { _ = try reader.readInt32() }
        if fullChatFlags & (1 << 15) != 0 { _ = try readPeer(&reader) }
        if fullChatFlags & (1 << 16) != 0 { _ = try reader.readString() }
        if fullChatFlags & (1 << 17) != 0 {
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt64() }
        }
        if fullChatFlags & (1 << 18) != 0 { try skipChatReactions(&reader) }
        if fullChatFlags & (1 << 20) != 0 { _ = try reader.readInt32() }
        // messages.chatFull returns auxiliary chats/users after full_chat.
        _ = try readChatVector(&reader)
        _ = try readUserVector(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return isAdministrator
    }

    /// Decodes the complete ChannelParticipant payload before making the
    /// authorization decision. Channel admin/creator variants contain a
    /// nested ChatAdminRights constructor; skipping only the visible IDs
    /// would leave the reader misaligned and could turn a valid admin into a
    /// false negative (or vice versa) as the schema evolves.
    func readChannelParticipant(_ reader: inout TLReader, targetUserID: Int64) throws -> Bool {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x1bd54456: // channelParticipant
            let flags = try reader.readInt32()
            _ = try reader.readInt64() // user_id
            _ = try reader.readInt32() // date
            if flags & 1 != 0 { _ = try reader.readInt32() } // subscription_until_date
            if flags & 4 != 0 { _ = try reader.readString() } // rank
            return false

        case 0xa9478a1a: // channelParticipantSelf
            let flags = try reader.readInt32()
            _ = try reader.readInt64() // user_id
            _ = try reader.readInt64() // inviter_id
            _ = try reader.readInt32() // date
            if flags & 2 != 0 { _ = try reader.readInt32() } // subscription_until_date
            if flags & 4 != 0 { _ = try reader.readString() } // rank
            return false

        case 0x2fe601d3: // channelParticipantCreator
            let flags = try reader.readInt32()
            let memberID = try reader.readInt64()
            try skipChatAdminRights(&reader)
            if flags & 1 != 0 { _ = try reader.readString() } // rank
            return memberID == targetUserID

        case 0x34c3bb53: // channelParticipantAdmin
            let flags = try reader.readInt32()
            let memberID = try reader.readInt64()
            if flags & 2 != 0 { _ = try reader.readInt64() } // inviter_id
            _ = try reader.readInt64() // promoted_by
            _ = try reader.readInt32() // date
            try skipChatAdminRights(&reader)
            if flags & 4 != 0 { _ = try reader.readString() } // rank
            return memberID == targetUserID

        case 0xd5f0ad91: // channelParticipantBanned
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readInt64() // kicked_by
            _ = try reader.readInt32() // date
            try skipChatBannedRights(&reader)
            if flags & 4 != 0 { _ = try reader.readString() } // rank
            return false

        case 0x1b03f006: // channelParticipantLeft
            _ = try readPeer(&reader)
            return false

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    func writeInputUser(_ writer: inout TLWriter, _ user: NativeUser) {
        writer.writeInt32(Int32(bitPattern: 0xd8292816)) // inputUser
        writer.writeInt64(user.id)
        writer.writeInt64(user.accessHash ?? 0)
    }

    func writeInputPeer(_ writer: inout TLWriter, _ peer: NativePeer) {
        switch peer {
        case .user(let id, let accessHash):
            writer.writeInt32(-571955892)
            writer.writeInt64(id)
            writer.writeInt64(accessHash ?? 0)
        case .chat(let id):
            writer.writeInt32(900291769)
            writer.writeInt64(id)
        case .channel(let id, let accessHash):
            writer.writeInt32(666680316)
            writer.writeInt64(id)
            writer.writeInt64(accessHash ?? 0)
        }
    }

    func parseDialogs(_ data: Data) throws -> TelegramDialogPage {
        var reader = TLReader(data)
        let constructor = try reader.readInt32()
        if constructor == -253500010 {
            _ = try reader.readInt32() // count
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return TelegramDialogPage(dialogs: [], messages: [], chats: [], users: [])
        }
        guard constructor == 364538944 || constructor == 1910543603 else {
            throw TelegramAPIError.invalidResponse
        }
        if constructor == 1910543603 { _ = try reader.readInt32() }
        let peerList = try reader.readVector { reader in try readDialogPeer(&reader) }
        let messages = try readMessageVector(&reader)
        let chats = try readChatVector(&reader)
        let users = try readUserVector(&reader)
        // Telegram may repeat an entity when the same peer is referenced by
        // both a dialog and one of its messages. Keep the most complete/latest
        // occurrence instead of allowing a malformed or future response to
        // crash the parser through Dictionary(uniqueKeysWithValues:).
        let chatMap = Dictionary(chats.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let userMap = Dictionary(users.map { ($0.id, $0) }, uniquingKeysWith: { _, latest in latest })
        let dialogs = peerList.map { parsed -> NativeDialog in
            let peer = parsed.peer
            let enrichedPeer: NativePeer
            switch peer {
            case .user(let id, _):
                let user = userMap[id]
                enrichedPeer = .user(id: id, accessHash: user?.accessHash)
                return NativeDialog(peer: enrichedPeer, title: user?.displayName ?? String(id), isPrivate: true, isGroup: false, isBroadcast: false, channelPTS: nil)
            case .chat(let id):
                enrichedPeer = .chat(id: id)
                return NativeDialog(peer: enrichedPeer, title: chatMap[id]?.title ?? String(id), isPrivate: false, isGroup: true, isBroadcast: false, channelPTS: nil)
            case .channel(let id, _):
                let chat = chatMap[id]
                enrichedPeer = .channel(id: id, accessHash: chat?.accessHash)
                return NativeDialog(peer: enrichedPeer, title: chat?.title ?? String(id), isPrivate: false, isGroup: true, isBroadcast: chat?.isBroadcast ?? false, channelPTS: parsed.channelPTS)
            }
        }
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return TelegramDialogPage(dialogs: dialogs, messages: messages, chats: chats, users: users)
    }

    func parseMessages(_ data: Data) throws -> TelegramHistoryPage {
        var reader = TLReader(data)
        let constructor = try reader.readInt32()
        let messagesNotModified = Int32(bitPattern: 0x74535f21)
        guard constructor == 494135274 || constructor == 1595959062 || constructor == -948520370 || constructor == messagesNotModified else {
            throw TelegramAPIError.invalidResponse
        }
        if constructor == messagesNotModified {
            _ = try reader.readInt32() // count
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return TelegramHistoryPage(messages: [], chats: [], users: [])
        }
        if constructor == 1595959062 {
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 4 != 0 { _ = try reader.readInt32() }
            if flags & 8 != 0 { try skipSearchPostsFlood(&reader) }
        } else if constructor == -948520370 {
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 4 != 0 { _ = try reader.readInt32() }
        }
        let messages = try readMessageVector(&reader)
        try skipForumTopicVector(&reader)
        let chats = try readChatVector(&reader)
        let users = try readUserVector(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return TelegramHistoryPage(messages: messages, chats: chats, users: users)
    }

    private struct ParsedDialogPeer {
        let peer: NativePeer
        let channelPTS: Int32?
    }

    private func readDialogPeer(_ reader: inout TLReader) throws -> ParsedDialogPeer {
        switch try reader.readInt32() {
        case Int32(bitPattern: 0xd58a08c6): // dialog
            let flags = try reader.readInt32()
            let peer = try readPeer(&reader)
            _ = try reader.readInt32() // top_message
            _ = try reader.readInt32() // read_inbox_max_id
            _ = try reader.readInt32() // read_outbox_max_id
            _ = try reader.readInt32() // unread_count
            _ = try reader.readInt32() // unread_mentions_count
            _ = try reader.readInt32() // unread_reactions_count
            try skipPeerNotifySettings(&reader)
            let channelPTS = flags & 1 != 0 ? try reader.readInt32() : nil // pts
            if flags & 2 != 0 { try skipDraft(&reader) }
            if flags & 16 != 0 { _ = try reader.readInt32() } // folder_id
            if flags & 32 != 0 { _ = try reader.readInt32() } // ttl_period
            return ParsedDialogPeer(peer: peer, channelPTS: channelPTS)

        case 1908216652: // dialogFolder
            let flags = try reader.readInt32()
            try skipFolder(&reader)
            let peer = try readPeer(&reader)
            _ = try reader.readInt32() // top_message
            _ = try reader.readInt32() // unread_muted_peers_count
            _ = try reader.readInt32() // unread_unmuted_peers_count
            _ = try reader.readInt32() // unread_muted_messages_count
            _ = try reader.readInt32() // unread_unmuted_messages_count
            _ = flags
            return ParsedDialogPeer(peer: peer, channelPTS: nil)

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    func readMessageVector(_ reader: inout TLReader) throws -> [NativeMessage] {
        try reader.readVector { reader in try readMessage(&reader) }.compactMap { $0 }
    }

    func readChatVector(_ reader: inout TLReader) throws -> [NativeChat] {
        try reader.readVector { reader in try readChat(&reader) }
    }

    func readUserVector(_ reader: inout TLReader) throws -> [NativeUser] {
        try reader.readVector { reader in try readUser(&reader) }
    }

    private func skipForumTopicVector(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in
            try skipForumTopic(&reader)
            return ()
        }
    }

    private func skipForumTopic(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xcdff0eca else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt32() // id
        _ = try reader.readInt32() // date
        _ = try readPeer(&reader)
        _ = try reader.readString() // title
        _ = try reader.readInt32() // icon_color
        if flags & 1 != 0 { _ = try reader.readInt64() } // icon_emoji_id
        _ = try reader.readInt32() // top_message
        _ = try reader.readInt32() // read_inbox_max_id
        _ = try reader.readInt32() // read_outbox_max_id
        _ = try reader.readInt32() // unread_count
        _ = try reader.readInt32() // unread_mentions_count
        _ = try reader.readInt32() // unread_reactions_count
        _ = try readPeer(&reader) // from_id
        try skipPeerNotifySettings(&reader)
        if flags & 16 != 0 { try skipDraft(&reader) }
    }

    func readPeer(_ reader: inout TLReader) throws -> NativePeer {
        switch try reader.readInt32() {
        case 1498486562:
            return .user(id: try reader.readInt64(), accessHash: nil)
        case 918946202:
            return .chat(id: try reader.readInt64())
        case -1566230754:
            return .channel(id: try reader.readInt64(), accessHash: nil)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    func readUser(_ reader: inout TLReader) throws -> NativeUser {
        let constructor = try reader.readInt32()
        if constructor == -742634630 {
            return NativeUser(id: try reader.readInt64(), accessHash: nil, firstName: "", lastName: "", username: "", phone: nil, isSelf: false, isBot: false)
        }
        guard constructor == 829899656 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        let flags2 = try reader.readInt32()
        let id = try reader.readInt64()
        let accessHash = flags & 1 != 0 ? try reader.readInt64() : nil
        let firstName = flags & 2 != 0 ? try reader.readString() : ""
        let lastName = flags & 4 != 0 ? try reader.readString() : ""
        let username = flags & 8 != 0 ? try reader.readString() : ""
        let phone = flags & 16 != 0 ? try reader.readString() : nil
        if flags & 32 != 0 { try skipUserProfilePhoto(&reader) }
        if flags & 64 != 0 { try skipUserStatus(&reader) }
        if flags & (1 << 14) != 0 { _ = try reader.readInt32() }
        if flags & (1 << 18) != 0 { try skipRestrictionReasons(&reader) }
        if flags & (1 << 19) != 0 { _ = try reader.readString() }
        if flags & (1 << 22) != 0 { _ = try reader.readString() }
        if flags & (1 << 30) != 0 { try skipEmojiStatus(&reader) }
        if flags2 & 1 != 0 { try skipUsernames(&reader) }
        if flags2 & 32 != 0 { try skipRecentStory(&reader) }
        if flags2 & 256 != 0 { try skipPeerColor(&reader) }
        if flags2 & 512 != 0 { try skipPeerColor(&reader) }
        if flags2 & 4096 != 0 { _ = try reader.readInt32() }
        if flags2 & 16384 != 0 { _ = try reader.readInt64() }
        if flags2 & 32768 != 0 { _ = try reader.readInt64() }
        return NativeUser(
            id: id,
            accessHash: accessHash,
            firstName: firstName,
            lastName: lastName,
            username: username,
            phone: phone,
            isSelf: flags & (1 << 10) != 0,
            isBot: flags & (1 << 14) != 0
        )
    }

    private func readChat(_ reader: inout TLReader) throws -> NativeChat {
        let constructor = try reader.readInt32()
        switch constructor {
        case 1103884886: do {
            let flags = try reader.readInt32()
            let id = try reader.readInt64()
            let title = try reader.readString()
            try skipChatPhoto(&reader)
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 64 != 0 { try skipInputChannel(&reader) }
            if flags & 16384 != 0 { try skipChatAdminRights(&reader) }
            if flags & 262144 != 0 { try skipChatBannedRights(&reader) }
            return NativeChat(id: id, accessHash: nil, title: title, username: "", isChannel: false, isBroadcast: false, isMegagroup: false, adminRights: flags & (1 | 16384) != 0)
        }
        case 473084188: do {
            let flags = try reader.readInt32()
            let flags2 = try reader.readInt32()
            let id = try reader.readInt64()
            let accessHash = flags & 8192 != 0 ? try reader.readInt64() : nil
            let title = try reader.readString()
            let username = flags & 64 != 0 ? try reader.readString() : ""
            try skipChatPhoto(&reader)
            _ = try reader.readInt32()
            if flags & 512 != 0 { try skipRestrictionReasons(&reader) }
            if flags & 16384 != 0 { try skipChatAdminRights(&reader) }
            if flags & 32768 != 0 { try skipChatBannedRights(&reader) }
            if flags & 262144 != 0 { try skipChatBannedRights(&reader) }
            if flags & 131072 != 0 { _ = try reader.readInt32() }
            if flags2 & 1 != 0 { try skipUsernames(&reader) }
            if flags2 & 16 != 0 { try skipRecentStory(&reader) }
            if flags2 & 128 != 0 { try skipPeerColor(&reader) }
            if flags2 & 256 != 0 { try skipPeerColor(&reader) }
            if flags2 & 512 != 0 { try skipEmojiStatus(&reader) }
            if flags2 & 1024 != 0 { _ = try reader.readInt32() }
            if flags2 & 2048 != 0 { _ = try reader.readInt32() }
            if flags2 & 8192 != 0 { _ = try reader.readInt64() }
            if flags2 & 16384 != 0 { _ = try reader.readInt64() }
            if flags2 & 262144 != 0 { _ = try reader.readInt64() }
            return NativeChat(id: id, accessHash: accessHash, title: title, username: username, isChannel: true, isBroadcast: flags & 32 != 0, isMegagroup: flags & 256 != 0, adminRights: flags & (1 | 16384) != 0)
        }
        case 1704108455: do {
            let id = try reader.readInt64()
            return NativeChat(id: id, accessHash: nil, title: try reader.readString(), username: "", isChannel: false, isBroadcast: false, isMegagroup: false, adminRights: false)
        }
        case 399807445: do {
            let flags = try reader.readInt32()
            let id = try reader.readInt64()
            let accessHash = try reader.readInt64()
            let title = try reader.readString()
            if flags & 65536 != 0 { _ = try reader.readInt32() }
            return NativeChat(id: id, accessHash: accessHash, title: title, username: "", isChannel: true, isBroadcast: flags & 32 != 0, isMegagroup: flags & 256 != 0, adminRights: false)
        }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    func readMessage(_ reader: inout TLReader) throws -> NativeMessage? {
        let constructor = try reader.readInt32()
        if constructor == -1868117372 {
            let flags = try reader.readInt32()
            let id = try reader.readInt32()
            if flags & 1 != 0 { _ = try readPeer(&reader) }
            _ = id
            return nil
        }
        if constructor == Int32(bitPattern: 0x7a800e0a) {
            try skipMessageService(&reader)
            return nil
        }
        guard constructor == 988112002 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        let flags2 = try reader.readInt32()
        let id = try reader.readInt32()
        let sender = flags & 256 != 0 ? try readPeer(&reader) : nil
        if flags & 536870912 != 0 { _ = try reader.readInt32() }
        if flags2 & 4096 != 0 { _ = try reader.readString() }
        let peer = try readPeer(&reader)
        if flags & 268435456 != 0 { _ = try readPeer(&reader) }
        if flags & 4 != 0 { try skipMessageForwardHeader(&reader) }
        if flags & 2048 != 0 { _ = try reader.readInt64() }
        if flags2 & 1 != 0 { _ = try reader.readInt64() }
        if flags2 & (1 << 19) != 0 { _ = try readPeer(&reader) }
        if flags & 8 != 0 { try skipMessageReplyHeader(&reader) }
        let date = Date(timeIntervalSince1970: TimeInterval(try reader.readInt32()))
        let text = try reader.readString()
        var hasPhoto = false
        var photo: NativePhotoReference?
        if flags & 512 != 0 {
            photo = try readMessageMedia(&reader)
            hasPhoto = photo != nil
        }
        if flags & 64 != 0 { try skipReplyMarkup(&reader) }
        if flags & 128 != 0 { try skipMessageEntities(&reader) }
        if flags & 1024 != 0 { _ = try reader.readInt32() }
        if flags & 1024 != 0 { _ = try reader.readInt32() }
        if flags & 8388608 != 0 { try skipMessageReplies(&reader) }
        if flags & 32768 != 0 { _ = try reader.readInt32() }
        if flags & 65536 != 0 { _ = try reader.readString() }
        if flags & 131072 != 0 { _ = try reader.readInt64() }
        if flags & 1048576 != 0 { try skipMessageReactions(&reader) }
        if flags & 4194304 != 0 { try skipRestrictionReasons(&reader) }
        if flags & 33554432 != 0 { _ = try reader.readInt32() }
        if flags & 1073741824 != 0 { _ = try reader.readInt32() }
        if flags2 & 4 != 0 { _ = try reader.readInt64() }
        if flags2 & 8 != 0 { try skipFactCheck(&reader) }
        if flags2 & 32 != 0 { _ = try reader.readInt32() }
        if flags2 & 64 != 0 { _ = try reader.readInt64() }
        if flags2 & 128 != 0 { try skipSuggestedPost(&reader) }
        if flags2 & 1024 != 0 { _ = try reader.readInt32() }
        if flags2 & 2048 != 0 { _ = try reader.readString() }
        return NativeMessage(
            id: id,
            peerID: peerID(peer),
            senderID: sender.map(peerID),
            date: date,
            text: text,
            hasPhoto: hasPhoto,
            photo: photo,
            peerIdentity: peer.stableID,
            senderIdentity: sender?.stableID
        )
    }

    private func skipMessageService(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        _ = try reader.readInt32()
        if flags & 256 != 0 { _ = try readPeer(&reader) }
        _ = try readPeer(&reader)
        if flags & 268_435_456 != 0 { _ = try readPeer(&reader) }
        if flags & 8 != 0 { try skipMessageReplyHeader(&reader) }
        _ = try reader.readInt32()
        try skipMessageAction(&reader)
        if flags & 1_048_576 != 0 { try skipMessageReactions(&reader) }
        if flags & 33_554_432 != 0 { _ = try reader.readInt32() }
    }

    private func skipMessageAction(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xb6aef7b0:
            break
        case 0xbd47cbad:
            _ = try reader.readString()
            _ = try reader.readVector { reader in _ = try reader.readInt64(); return () }
        case 0xb5a1ce5a:
            _ = try reader.readString()
        case 0x7fcb13a8:
            try skipPhoto(&reader)
        case 0x95e3fbef:
            break
        case 0x15cefd00:
            _ = try reader.readVector { reader in _ = try reader.readInt64(); return () }
        case 0xa43f30cc, 0x31224c3, 0xe1037f92:
            _ = try reader.readInt64()
        case 0x95d2ac92:
            _ = try reader.readString()
        case 0xea3948e9:
            _ = try reader.readString(); _ = try reader.readInt64()
        case 0x94bd38ed, 0x9fbab604, 0x4792929b, 0xf3f25f76, 0xebbca3cb:
            break
        case 0x92a72876:
            _ = try reader.readInt64(); _ = try reader.readInt32()
        case 0xffa00ccc:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readBytes()
            if flags & 1 != 0 { try skipPaymentRequestedInfo(&reader) }
            if flags & 2 != 0 { _ = try reader.readString() }
            try skipPaymentCharge(&reader)
            if flags & 16 != 0 { _ = try reader.readInt32() }
        case 0xc624b16e:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 16 != 0 { _ = try reader.readInt32() }
        case 0x80e11a7f:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { try skipPhoneCallDiscardReason(&reader) }
            if flags & 2 != 0 { _ = try reader.readInt32() }
        case 0xfae69f56:
            _ = try reader.readString()
        case 0xc516d679:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { try skipBotApp(&reader) }
        case 0x98e0d697:
            _ = try readPeer(&reader); _ = try readPeer(&reader); _ = try reader.readInt32()
        case 0x7a0d7f42:
            let flags = try reader.readInt32()
            try skipInputGroupCall(&reader)
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case 0x502f92f7:
            try skipInputGroupCall(&reader)
            _ = try reader.readVector { reader in _ = try reader.readInt64(); return () }
        case 0x3c134d7b:
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt64() }
        case 0xb3a07661:
            try skipInputGroupCall(&reader); _ = try reader.readInt32()
        case 0xc3dffc04:
            _ = try reader.readString()
        case 0x47dd8079:
            _ = try reader.readString(); _ = try reader.readString()
        case 0xb4c38cb5:
            _ = try reader.readString()
        case 0x48e91302:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString(); _ = try reader.readInt64() }
            if flags & 2 != 0 { try skipTextWithEntities(&reader) }
        case 0x0d999256:
            let flags = try reader.readInt32()
            if flags & 2 == 0 { _ = try reader.readString() }
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt64() }
        case 0xc0944820:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { _ = try reader.readInt64() }
            if flags & 4 != 0 { _ = try reader.readBool() }
            if flags & 8 != 0 { _ = try reader.readBool() }
        case 0x57de635e:
            try skipPhoto(&reader)
        case 0x31518e9b:
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in _ = try readPeer(&reader); return () }
        case 0x31c48347:
            let flags = try reader.readInt32()
            if flags & 2 != 0 { _ = try readPeer(&reader) }
            _ = try reader.readInt32(); _ = try reader.readString()
            if flags & 4 != 0 { _ = try reader.readString(); _ = try reader.readInt64() }
            if flags & 8 != 0 { _ = try reader.readString(); _ = try reader.readInt64() }
            if flags & 16 != 0 { try skipTextWithEntities(&reader) }
        case 0xa80f51e4:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt64() }
        case 0x87e2f155:
            let flags = try reader.readInt32()
            _ = flags
            _ = try reader.readInt32(); _ = try reader.readInt32()
        case 0xcc02aa6d:
            _ = try reader.readInt32()
        case 0x41b3e202:
            let flags = try reader.readInt32()
            _ = try readPeer(&reader); _ = try reader.readString(); _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readBytes() }
            try skipPaymentCharge(&reader)
        case 0x45d5b021:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString(); _ = try reader.readInt64() }
            if flags & 2 != 0 { _ = try reader.readString() }
        case 0xb00c47a2:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readString(); _ = try readPeer(&reader); _ = try reader.readInt32()
            _ = flags
        case 0xac1f1fcd:
            _ = try reader.readInt32(); _ = try reader.readInt64()
        case 0x84b88578:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = flags
        case 0x2ffe2f7a:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 4 != 0 { _ = try reader.readInt32() }
            if flags & 8 != 0 { _ = try reader.readVector { reader in _ = try readPeer(&reader); return () } }
        case 0xcc7c5c89:
            _ = try reader.readVector { reader in _ = try reader.readInt32(); return () }
            _ = try reader.readVector { reader in _ = try reader.readInt32(); return () }
        case 0xee7a1596:
            let flags = try reader.readInt32()
            if flags & 4 != 0 { _ = try reader.readString() }
            if flags & 8 != 0 { _ = try reader.readInt32() }
            if flags & 16 != 0 { try skipStarsAmount(&reader) }
        case 0x95ddcf69:
            try skipStarsAmount(&reader)
        case 0x69f916f8:
            _ = try reader.readInt32()
        case 0xa8a3c699:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readString(); _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
        case 0xb07ed085, 0xe188503b:
            _ = try reader.readInt64()
        case 0xbf7d6572:
            _ = try reader.readBool(); _ = try reader.readBool()
        case 0x3e2793ba:
            _ = try reader.readInt32()
            _ = try reader.readBool(); _ = try reader.readBool()
        case 0x93b31848: // messageActionRequestedPeerSentMe
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in
                try skipRequestedPeer(&reader)
                return ()
            }
        case 0xea2c31d3: // messageActionStarGift
            let flags = try reader.readInt32()
            try skipStarGift(&reader)
            if flags & 2 != 0 { try skipTextWithEntities(&reader) }
            if flags & 16 != 0 { _ = try reader.readInt64() }
            if flags & 32 != 0 { _ = try reader.readInt32() }
            if flags & 256 != 0 { _ = try reader.readInt64() }
            if flags & 2048 != 0 { _ = try readPeer(&reader) }
            if flags & 4096 != 0 {
                _ = try readPeer(&reader)
                _ = try reader.readInt64() // saved_id
            }
            if flags & 16384 != 0 { _ = try reader.readString() }
            if flags & 32768 != 0 { _ = try reader.readInt32() }
            if flags & 262144 != 0 { _ = try readPeer(&reader) }
            if flags & 524288 != 0 { _ = try reader.readInt32() }
        case 0xe6c31522: // messageActionStarGiftUnique
            let flags = try reader.readInt32()
            try skipStarGift(&reader)
            if flags & 8 != 0 { _ = try reader.readInt32() }
            if flags & 16 != 0 { _ = try reader.readInt64() }
            if flags & 64 != 0 { _ = try readPeer(&reader) }
            if flags & 128 != 0 { _ = try readPeer(&reader); _ = try reader.readInt64() }
            if flags & 256 != 0 { try skipStarsAmount(&reader) }
            if flags & 512 != 0 { _ = try reader.readInt32() }
            if flags & 1024 != 0 { _ = try reader.readInt32() }
            if flags & 4096 != 0 { _ = try reader.readInt64() }
            if flags & 32768 != 0 { _ = try reader.readInt32() }
        case 0xc7edbc83: // messageActionTodoAppendTasks
            _ = try reader.readVector { reader in
                try skipTodoItem(&reader)
                return ()
            }
        case 0x2c8f2a25: // messageActionSuggestBirthday
            try skipBirthday(&reader)
        case 0x774278d4: // messageActionStarGiftPurchaseOffer
            _ = try reader.readInt32()
            try skipStarGift(&reader)
            try skipStarsAmount(&reader)
            _ = try reader.readInt32()
        case 0x73ada76b: // messageActionStarGiftPurchaseOfferDeclined
            _ = try reader.readInt32()
            try skipStarGift(&reader)
            try skipStarsAmount(&reader)
        case 0x1b287353: // messageActionSecureValuesSentMe
            _ = try reader.readVector { reader in
                try skipSecureValue(&reader)
                return ()
            }
            try skipSecureCredentialsEncrypted(&reader)
        case 0xd95c6154: // messageActionSecureValuesSent
            _ = try reader.readVector { reader in
                try skipSecureValueType(&reader)
                return ()
            }
        case 0xb91bbd3a: // messageActionSetChatTheme
            try skipChatTheme(&reader)
        case 0x5060a3f4: // messageActionSetChatWallPaper
            _ = try reader.readInt32()
            try skipWallPaper(&reader)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipInputGroupCall(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xd8aa840f:
            _ = try reader.readInt64(); _ = try reader.readInt64()
        case 0xfe06823f:
            _ = try reader.readString()
        case 0x8c10603f:
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipExportedChatInvite(_ reader: inout TLReader) throws {
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
            if flags & (1 << 9) != 0 {
                guard UInt32(bitPattern: try reader.readInt32()) == 0x05416d58 else {
                    throw TelegramAPIError.invalidResponse
                }
                _ = try reader.readInt32()
                _ = try reader.readInt64()
            }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipBotInfo(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x4d8a0299 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readInt64() }
        if flags & 2 != 0 { _ = try reader.readString() }
        if flags & 16 != 0 { try skipPhoto(&reader) }
        if flags & 32 != 0 { try skipDocument(&reader) }
        if flags & 4 != 0 {
            _ = try reader.readVector { reader in
                guard UInt32(bitPattern: try reader.readInt32()) == 0xc27ac8c7 else {
                    throw TelegramAPIError.invalidResponse
                }
                _ = try reader.readString()
                _ = try reader.readString()
                return ()
            }
        }
        if flags & 8 != 0 { try skipBotMenuButton(&reader) }
        if flags & 128 != 0 { _ = try reader.readString() }
        if flags & 256 != 0 { try skipBotAppSettings(&reader) }
        if flags & 512 != 0 { try skipBotVerifierSettings(&reader) }
    }

    private func skipBotMenuButton(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x7533a588, 0x4258c205:
            return
        case 0xc7b57ce6:
            _ = try reader.readString()
            _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipBotAppSettings(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xc99b1950 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readBytes() }
        if flags & 2 != 0 { _ = try reader.readInt32() }
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readInt32() }
        if flags & 16 != 0 { _ = try reader.readInt32() }
    }

    private func skipBotVerifierSettings(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xb0cd6617 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt64()
        _ = try reader.readString()
        if flags & 1 != 0 { _ = try reader.readString() }
    }

    private func skipChatReactions(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xeafc32bc: // chatReactionsNone
            return
        case 0x52928bca: // chatReactionsAll
            _ = try reader.readInt32()
        case 0x661d4037: // chatReactionsSome
            _ = try reader.readVector { reader in
                try skipReaction(&reader)
                return ()
            }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipPhoneCallDiscardReason(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x85e42301, 0xe095c1a0, 0x57adc690, 0xfaf7e8c9:
            break
        case 0x9fbbf1f7:
            _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipPaymentRequestedInfo(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readString() }
        if flags & 2 != 0 { _ = try reader.readString() }
        if flags & 4 != 0 { _ = try reader.readString() }
        if flags & 8 != 0 {
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
        }
    }

    private func skipPaymentCharge(_ reader: inout TLReader) throws {
        _ = try reader.readString(); _ = try reader.readString()
    }

    private func skipBotApp(_ reader: inout TLReader) throws {
        let constructor = try reader.readInt32()
        switch UInt32(bitPattern: constructor) {
        case 0x5da674b7:
            break
        case 0x95fcd1d6:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt64()
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
            try skipPhoto(&reader)
            if flags & 1 != 0 { try skipDocument(&reader) }
            _ = try reader.readInt64()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func peerID(_ peer: NativePeer) -> Int64 {
        switch peer {
        case .user(let id, _), .chat(let id), .channel(let id, _): return id
        }
    }

    private func skipDraft(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 453805082:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case -1763006997:
            let flags = try reader.readInt32()
            if flags & 16 != 0 { try skipInputReplyTo(&reader) }
            _ = try reader.readString()
            if flags & 8 != 0 { try skipMessageEntities(&reader) }
            if flags & 32 != 0 { try skipInputMedia(&reader) }
            _ = try reader.readInt32()
            if flags & 128 != 0 { _ = try reader.readInt64() }
            if flags & 256 != 0 { try skipSuggestedPost(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipFolder(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == -11252123 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        _ = try reader.readInt32() // id
        _ = try reader.readString() // title
        if flags & 8 != 0 { try skipChatPhoto(&reader) }
    }
    private func skipInputReplyTo(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -2036351472:
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { try skipInputPeer(&reader) }
            if flags & 4 != 0 { _ = try reader.readString() }
            if flags & 8 != 0 { try skipMessageEntities(&reader) }
            if flags & 16 != 0 { _ = try reader.readInt32() }
            if flags & 32 != 0 { try skipInputPeer(&reader) }
            if flags & 64 != 0 { _ = try reader.readInt32() }
        case 1484862010:
            try skipInputPeer(&reader)
            _ = try reader.readInt32()
        case 1775660101:
            try skipInputPeer(&reader)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputPeer(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 2134579434, 2107670217, -1182234929, -138301121:
            break
        case 900291769:
            _ = try reader.readInt64()
        case -571955892:
            _ = try reader.readInt64(); _ = try reader.readInt64()
        case 666680316:
            _ = try reader.readInt64(); _ = try reader.readInt64()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputUser(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xb98886cf, 0xf7c1b13f:
            break
        case 0xf21158c6:
            _ = try reader.readInt64(); _ = try reader.readInt64()
        case 0x1da448e2:
            try skipInputPeer(&reader)
            _ = try reader.readInt32(); _ = try reader.readInt64()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipRequestPeerType(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x5f3b8a00:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readBool() }
            if flags & 2 != 0 { _ = try reader.readBool() }
        case 0xc9f06e1b:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readBool() }
            if flags & 32 != 0 { _ = try reader.readBool() }
            if flags & 8 != 0 { _ = try reader.readBool() }
            if flags & 16 != 0 { _ = try reader.readBool() }
            if flags & 2 != 0 { try skipChatAdminRights(&reader) }
            if flags & 4 != 0 { try skipChatAdminRights(&reader) }
        case 0x339bef6c:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readBool() }
            if flags & 8 != 0 { _ = try reader.readBool() }
            if flags & 2 != 0 { try skipChatAdminRights(&reader) }
            if flags & 4 != 0 { try skipChatAdminRights(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputFile(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -181407105:
            _ = try reader.readInt64(); _ = try reader.readInt32(); _ = try reader.readString(); _ = try reader.readString()
        case -95482955:
            _ = try reader.readInt64(); _ = try reader.readInt32(); _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputPhoto(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 483901197:
            break
        case 1001634122:
            _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readBytes()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputDocument(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 1928391342:
            break
        case 448771445:
            _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readBytes()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputGeoPoint(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -457104426:
            break
        case 1210199983:
            _ = try reader.readInt64(); _ = try reader.readInt64()
            if try reader.readInt32() & 1 != 0 { _ = try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputMedia(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1771768449:
            break
        case -428884101:
            _ = try reader.readString()
        case 505969924:
            let flags = try reader.readInt32()
            try skipInputFile(&reader)
            if flags & 1 != 0 { _ = try reader.readVector { reader in try skipInputDocument(&reader); return () } }
            if flags & 2 != 0 { _ = try reader.readInt32() }
        case -1279654347:
            let flags = try reader.readInt32()
            try skipInputPhoto(&reader)
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case -104578748:
            try skipInputGeoPoint(&reader)
        case -122978821:
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
        case 58495792:
            let flags = try reader.readInt32()
            try skipInputFile(&reader)
            if flags & 4 != 0 { try skipInputFile(&reader) }
            _ = try reader.readString()
            _ = try reader.readVector { reader in try skipDocumentAttribute(&reader); return () }
            if flags & 1 != 0 { _ = try reader.readVector { reader in try skipInputDocument(&reader); return () } }
            if flags & 64 != 0 { try skipInputPhoto(&reader) }
            if flags & 128 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readInt32() }
        case -1468646731:
            let flags = try reader.readInt32()
            try skipInputDocument(&reader)
            if flags & 8 != 0 { try skipInputPhoto(&reader) }
            if flags & 16 != 0 { _ = try reader.readInt32() }
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readString() }
        case -1052959727:
            try skipInputGeoPoint(&reader)
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
        case -440664550:
            let flags = try reader.readInt32()
            _ = try reader.readString()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case -1038383031:
            _ = try reader.readInt32(); _ = try reader.readString()
        case -1759532989:
            let flags = try reader.readInt32()
            try skipInputGeoPoint(&reader)
            if flags & 4 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readInt32() }
            if flags & 8 != 0 { _ = try reader.readInt32() }
        case 261416433:
            let flags = try reader.readInt32()
            try skipPoll(&reader)
            if flags & 1 != 0 { _ = try reader.readVector { reader in try reader.readBytes() } }
            if flags & 2 != 0 { _ = try reader.readString(); try skipMessageEntities(&reader) }
        case -1979852936:
            try skipInputPeer(&reader); _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipSearchPostsFlood(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 1040931690 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        _ = try reader.readInt32(); _ = try reader.readInt32()
        if flags & 2 != 0 { _ = try reader.readInt32() }
        _ = try reader.readInt64()
    }
    private func skipPeerNotifySettings(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == -1721619444 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readBool() }
        if flags & 2 != 0 { _ = try reader.readBool() }
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { try skipNotificationSound(&reader) }
        if flags & 16 != 0 { try skipNotificationSound(&reader) }
        if flags & 32 != 0 { try skipNotificationSound(&reader) }
        if flags & 64 != 0 { _ = try reader.readBool() }
        if flags & 128 != 0 { _ = try reader.readBool() }
        if flags & 256 != 0 { try skipNotificationSound(&reader) }
        if flags & 512 != 0 { try skipNotificationSound(&reader) }
        if flags & 1024 != 0 { try skipNotificationSound(&reader) }
    }
    private func skipInputChannel(_ reader: inout TLReader) throws { _ = try reader.readInt32(); _ = try reader.readInt64(); _ = try reader.readInt64() }
    private func skipChatAdminRights(_ reader: inout TLReader) throws { _ = try reader.readInt32(); _ = try reader.readInt32() }
    private func skipChatBannedRights(_ reader: inout TLReader) throws { _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32() }
    private func skipRestrictionReasons(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString() }
    }
    private func skipUsernames(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in
            guard try reader.readInt32() == Int32(bitPattern: 0xb4073647) else {
                throw TelegramAPIError.invalidResponse
            }
            _ = try reader.readInt32() // flags: editable, active
            _ = try reader.readString()
        }
    }
    private func skipRecentStory(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 2 != 0 { _ = try reader.readInt32() }
    }
    private func skipPeerColor(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1253352753:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readInt64() }
        case -1178573926:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt32() }
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readVector { reader in try reader.readInt32() } }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipEmojiStatus(_ reader: inout TLReader) throws {
        let constructor = try reader.readInt32()
        switch constructor {
        case 769727150:
            break
        case -402717046:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        case 1904500795:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt64()
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readInt64()
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipUserProfilePhoto(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 1326562017:
            break
        case -2100168954:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 2 != 0 { _ = try reader.readBytes() }
            _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipUserStatus(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 164646985: break
        case -306628279, 9203775: _ = try reader.readInt32()
        case 2065268168, 1410997530, 1703516023: _ = try reader.readInt32()
        default: throw TelegramAPIError.invalidResponse
        }
    }
    private func skipChatPhoto(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 935395612: break
        case 476978193:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 2 != 0 { _ = try reader.readBytes() }
            _ = try reader.readInt32()
        default: throw TelegramAPIError.invalidResponse
        }
    }
    private func skipNotificationSound(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1746354498, 1863070943:
            break
        case -2096391452:
            _ = try reader.readString()
            _ = try reader.readString()
        case -9666487:
            _ = try reader.readInt64()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipMessageForwardHeader(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try readPeer(&reader) }
        if flags & 32 != 0 { _ = try reader.readString() }
        _ = try reader.readInt32()
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readString() }
        if flags & 16 != 0 {
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
        }
        if flags & 256 != 0 { _ = try readPeer(&reader) }
        if flags & 512 != 0 { _ = try reader.readString() }
        if flags & 1024 != 0 { _ = try reader.readInt32() }
        if flags & 64 != 0 { _ = try reader.readString() }
    }
    private func skipMessageReplyHeader(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 1 != 0 { _ = try readPeer(&reader) }
        if flags & 32 != 0 { try skipMessageForwardHeader(&reader) }
        if flags & 256 != 0 { _ = try skipMessageMedia(&reader) }
        if flags & 2 != 0 { _ = try reader.readInt32() }
        if flags & 64 != 0 { _ = try reader.readString() }
        if flags & 128 != 0 { try skipMessageEntities(&reader) }
        if flags & 1024 != 0 { _ = try reader.readInt32() }
        if flags & 2048 != 0 { _ = try reader.readInt32() }
        if flags & 4096 != 0 { _ = try reader.readBytes() }
    }
    private func skipReplyMarkup(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1606526075:
            _ = try reader.readInt32()
        case -2035021048:
            let flags = try reader.readInt32()
            if flags & 8 != 0 { _ = try reader.readString() }
        case -2049074735:
            let flags = try reader.readInt32()
            _ = try reader.readVector { reader in try skipKeyboardButtonRow(&reader) }
            if flags & 8 != 0 { _ = try reader.readString() }
        case 1218642516:
            _ = try reader.readVector { reader in try skipKeyboardButtonRow(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipKeyboardButtonRow(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 2002815875 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readVector { reader in try skipKeyboardButton(&reader) }
    }
    private func skipKeyboardButtonStyle(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 1339896880 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        if flags & 8 != 0 { _ = try reader.readInt64() }
    }
    private func skipKeyboardButton(_ reader: inout TLReader) throws {
        let constructor = try reader.readInt32()
        let styledConstructors: Set<Int32> = [
            2098662655, -670292500, -433338016, 1098841487, -1438582451,
            -1726768644, -1983540999, 1067792645, -183499015, 2047989634,
            -398020192, -514047120, -1127960816, -1057137399,
            1744911986, 2103314375, 1527715317, 45580630
        ]
        guard styledConstructors.contains(constructor) else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        if flags & 1024 != 0 { try skipKeyboardButtonStyle(&reader) }
        switch constructor {
        case -433338016:
            _ = try reader.readString()
            _ = try reader.readBytes()
        case -1726768644:
            _ = try reader.readString()
            _ = try reader.readString()
            if flags & 2 != 0 {
                _ = try reader.readVector { reader in _ = try reader.readInt32(); return () }
            }
        case -183499015:
            _ = try reader.readString()
            if flags & 1 != 0 { _ = try reader.readString() }
            _ = try reader.readString()
            _ = try reader.readInt32()
        case 2047989634:
            if flags & 1 != 0 { _ = try reader.readBool() }
            _ = try reader.readString()
        case 1527715317:
            _ = try reader.readString()
            _ = try reader.readInt32()
            try skipRequestPeerType(&reader)
            _ = try reader.readInt32()
        case 45580630:
            _ = try reader.readString()
            _ = try reader.readInt32()
            try skipRequestPeerType(&reader)
            _ = try reader.readInt32()
        case 1744911986:
            if flags & 2 != 0 { _ = try reader.readString() }
            _ = try reader.readString()
            try skipInputUser(&reader)
        case 2103314375:
            _ = try reader.readString()
            try skipInputUser(&reader)
        case -1057137399:
            _ = try reader.readString()
            _ = try reader.readInt64()
        case -1127960816:
            _ = try reader.readString()
            _ = try reader.readString()
        case -670292500, -398020192, -514047120:
            _ = try reader.readString()
            _ = try reader.readString()
        default:
            _ = try reader.readString()
        }
    }
    private func skipPhoto(_ reader: inout TLReader) throws {
        _ = try readPhotoReference(&reader)
    }
    private func readPhotoReference(_ reader: inout TLReader) throws -> NativePhotoReference? {
        switch try reader.readInt32() {
        case 590459437:
            _ = try reader.readInt64()
            return nil
        case -82216347:
            let flags = try reader.readInt32()
            let id = try reader.readInt64()
            let accessHash = try reader.readInt64()
            let fileReference = try reader.readBytes()
            _ = try reader.readInt32()
            let sizes = try readPhotoSizes(&reader)
            if flags & 2 != 0 { try skipVideoSizes(&reader) }
            let dcID = try reader.readInt32()
            let thumbSize = sizes.max { $0.byteCount < $1.byteCount }?.type ?? ""
            return NativePhotoReference(id: id, accessHash: accessHash, fileReference: fileReference, dcID: dcID, thumbSize: thumbSize)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func readPhotoSizes(_ reader: inout TLReader) throws -> [(type: String, byteCount: Int)] {
        try reader.readVector { reader in
            switch try reader.readInt32() {
            case 236446268:
                return (try reader.readString(), 0)
            case 1976012384:
                let type = try reader.readString()
                _ = try reader.readInt32(); _ = try reader.readInt32()
                return (type, Int(try reader.readInt32()))
            case 35527382:
                let type = try reader.readString()
                _ = try reader.readInt32(); _ = try reader.readInt32()
                let bytes = try reader.readBytes()
                return (type, bytes.count)
            case -525288402, -668906175:
                let type = try reader.readString()
                let bytes = try reader.readBytes()
                return (type, bytes.count)
            case -96535659:
                let type = try reader.readString()
                _ = try reader.readInt32(); _ = try reader.readInt32()
                let sizes = try reader.readVector { reader in try reader.readInt32() }
                return (type, Int(sizes.max() ?? 0))
            default:
                throw TelegramAPIError.invalidResponse
            }
        }
    }
    private func skipPhotoSizes(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in
            switch try reader.readInt32() {
            case 236446268:
                _ = try reader.readString()
            case 1976012384:
                _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
            case 35527382:
                _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readBytes()
            case -525288402:
                _ = try reader.readString(); _ = try reader.readBytes()
            case -96535659:
                _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readVector { reader in try reader.readInt32() }
            case -668906175:
                _ = try reader.readString(); _ = try reader.readBytes()
            default:
                throw TelegramAPIError.invalidResponse
            }
        }
    }
    private func skipVideoSizes(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in
            switch try reader.readInt32() {
            case -567037804:
                let flags = try reader.readInt32()
                _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
                if flags & 1 != 0 { _ = try reader.readInt64() }
            case -128171716:
                _ = try reader.readInt64()
                _ = try reader.readVector { reader in try reader.readInt32() }
            default:
                throw TelegramAPIError.invalidResponse
            }
        }
    }
    private func readMessageMedia(_ reader: inout TLReader) throws -> NativePhotoReference? {
        let constructor = try reader.readInt32()
        if constructor == 1766936791 {
            let flags = try reader.readInt32()
            let photo = flags & 1 != 0 ? try readPhotoReference(&reader) : nil
            if flags & 4 != 0 { _ = try reader.readInt32() }
            return photo
        }
        _ = try skipMessageMediaBody(constructor, reader: &reader)
        return nil
    }
    private func skipMessageMedia(_ reader: inout TLReader) throws -> Bool {
        try skipMessageMediaBody(try reader.readInt32(), reader: &reader)
    }
    private func skipMessageMediaBody(_ constructor: Int32, reader: inout TLReader) throws -> Bool {
        switch constructor {
        case 1038967584, -1618676578:
            return false
        case 1766936791:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try readPhotoReference(&reader) }
            if flags & 4 != 0 { _ = try reader.readInt32() }
            return true
        case 1389939929:
            try skipDocumentMedia(&reader)
            return false
        case 1457575028:
            try skipGeoPoint(&reader)
            return false
        case 1882335561:
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readInt64()
            return false
        case -571405253:
            let flags = try reader.readInt32()
            _ = flags
            try skipWebPage(&reader)
            return false
        case 784356159:
            try skipGeoPoint(&reader)
            _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
            return false
        case -1186937242:
            let flags = try reader.readInt32()
            try skipGeoPoint(&reader)
            if flags & 1 != 0 { _ = try reader.readInt32() }
            _ = try reader.readInt32()
            if flags & 2 != 0 { _ = try reader.readInt32() }
            return false
        case 1272375192:
            try skipPoll(&reader)
            try skipPollResults(&reader)
            return false
        case 147581959:
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readString()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            return false
        case -156940077:
            let flags = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readString()
            if flags & 1 != 0 { try skipWebDocument(&reader) }
            if flags & 4 != 0 { _ = try reader.readInt32() }
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readString()
            if flags & 16 != 0 { try skipExtendedMedia(&reader) }
            return false
        case -1467669359:
            _ = try reader.readInt64()
            _ = try reader.readVector { reader in try skipExtendedMedia(&reader); return () }
            return false
        case -1974226924: // messageMediaToDo
            let flags = try reader.readInt32()
            try skipTodoList(&reader)
            if flags & 1 != 0 {
                _ = try reader.readVector { reader in
                    try skipTodoCompletion(&reader)
                    return ()
                }
            }
            return false
        case -899896439: // messageMediaVideoStream
            let flags = try reader.readInt32()
            if flags & 1 == 0 { try skipInputGroupCall(&reader) }
            return false
        case 1758159491: // messageMediaStory
            let flags = try reader.readInt32()
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            if flags & 1 != 0 { try skipStoryItem(&reader) }
            return false
        case -1442366485:
            let flags = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt64() }
            if flags & 2 != 0 { _ = try reader.readVector { reader in try reader.readString() } }
            if flags & 8 != 0 { _ = try reader.readString() }
            _ = try reader.readInt32()
            if flags & 16 != 0 { _ = try reader.readInt32() }
            if flags & 32 != 0 { _ = try reader.readInt64() }
            _ = try reader.readInt32()
            return false
        case -827703647:
            let flags = try reader.readInt32()
            _ = flags
            _ = try reader.readInt64()
            if flags & 8 != 0 { _ = try reader.readInt32() }
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
            _ = try reader.readVector { reader in try reader.readInt64() }
            if flags & 16 != 0 { _ = try reader.readInt32() }
            if flags & 32 != 0 { _ = try reader.readInt64() }
            if flags & 2 != 0 { _ = try reader.readString() }
            _ = try reader.readInt32()
            return false
        case -38694904:
            try skipGame(&reader)
            return false
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipGeoPoint(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 286776671:
            break
        case -1297942941:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipDocumentMedia(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 1 != 0 { try skipDocument(&reader) }
        if flags & 32 != 0 { _ = try reader.readVector { reader in try skipDocument(&reader); return () } }
        if flags & 512 != 0 { try skipPhoto(&reader) }
        if flags & 1024 != 0 { _ = try reader.readInt32() }
        if flags & 4 != 0 { _ = try reader.readInt32() }
    }
    private func skipDocument(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 922273905:
            _ = try reader.readInt64()
        case -1881881384:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readBytes(); _ = try reader.readInt32()
            _ = try reader.readString(); _ = try reader.readInt64()
            if flags & 1 != 0 { try skipPhotoSizes(&reader) }
            if flags & 2 != 0 { try skipVideoSizes(&reader) }
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in try skipDocumentAttribute(&reader); return () }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipDocumentAttribute(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 1815593308:
            _ = try reader.readInt32(); _ = try reader.readInt32()
        case 297109817, -1744710921:
            break
        case 1662637586:
            let flags = try reader.readInt32()
            _ = try reader.readString()
            try skipInputStickerSet(&reader)
            if flags & 1 != 0 { try skipMaskCoords(&reader) }
        case 1137015880:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readInt32(); _ = try reader.readInt32()
            if flags & 4 != 0 { _ = try reader.readInt32() }
            if flags & 16 != 0 { _ = try reader.readInt64() }
            if flags & 32 != 0 { _ = try reader.readString() }
        case -1739392570:
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { _ = try reader.readBytes() }
        case 358154344:
            _ = try reader.readString()
        case -48981863:
            let flags = try reader.readInt32()
            _ = try reader.readString()
            try skipInputStickerSet(&reader)
            _ = flags
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipInputStickerSet(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -4838507, 42402760, 215889721, -930399486, 80008398, 701560302, 1153562857, 1232373075, 485912992:
            break
        case -1645763991:
            _ = try reader.readInt64(); _ = try reader.readInt64()
        case -2044933984, -427863538:
            _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipMaskCoords(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == -1361650766 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readInt32(); _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readInt64()
    }
    private func skipWebPage(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 555358905:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
        case -1328464313:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
            _ = try reader.readInt32()
        case -392411726:
            let flags = try reader.readInt32()
            _ = try reader.readInt64(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { _ = try reader.readString() }
            if flags & 8 != 0 { _ = try reader.readString() }
            if flags & 16 != 0 { try skipPhoto(&reader) }
            if flags & 32 != 0 { _ = try reader.readString(); _ = try reader.readString() }
            if flags & 64 != 0 { _ = try reader.readInt32(); _ = try reader.readInt32() }
            if flags & 128 != 0 { _ = try reader.readInt32() }
            if flags & 256 != 0 { _ = try reader.readString() }
            if flags & 512 != 0 { try skipDocument(&reader) }
            if flags & 1024 != 0 { throw TelegramAPIError.invalidResponse }
        case 1930545681:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipWebDocument(_ reader: inout TLReader) throws {
        let constructor = try reader.readInt32()
        switch constructor {
        case 475467473:
            _ = try reader.readString(); _ = try reader.readInt64(); _ = try reader.readInt32(); _ = try reader.readString()
            _ = try reader.readVector { reader in try skipDocumentAttribute(&reader); return () }
        case -104284986:
            _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readString()
            _ = try reader.readVector { reader in try skipDocumentAttribute(&reader); return () }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipGame(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == -1107729093 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        _ = try reader.readInt64(); _ = try reader.readInt64(); _ = try reader.readString(); _ = try reader.readString(); _ = try reader.readString()
        try skipPhoto(&reader)
        if flags & 1 != 0 { try skipDocument(&reader) }
    }

    private func skipStoryItem(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x51e6ee4f: // storyItemDeleted
            _ = try reader.readInt32()
        case 0xffadc913: // storyItemSkipped
            _ = try reader.readInt32() // flags
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
        case 0xedf164f1: // storyItem
            let flags = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            if flags & 262_144 != 0 { _ = try readPeer(&reader) }
            if flags & 131_072 != 0 { try skipStoryForwardHeader(&reader) }
            _ = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { try skipMessageEntities(&reader) }
            _ = try skipMessageMedia(&reader)
            if flags & 16_384 != 0 {
                _ = try reader.readVector { reader in
                    try skipMediaArea(&reader)
                    return ()
                }
            }
            if flags & 4 != 0 { try skipPrivacyRules(&reader) }
            if flags & 8 != 0 { try skipStoryViews(&reader) }
            if flags & 32_768 != 0 { try skipReaction(&reader) }
            if flags & 524_288 != 0 { _ = try reader.readVector { reader in try reader.readInt32() } }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipStoryForwardHeader(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try readPeer(&reader) }
        if flags & 2 != 0 { _ = try reader.readString() }
        if flags & 4 != 0 { _ = try reader.readInt32() }
    }

    private func skipStoryViews(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x8d595cd6 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt32()
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readVector { reader in try skipReactionCount(&reader); return () } }
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 1 != 0 { _ = try reader.readVector { reader in try reader.readInt64() } }
    }

    private func skipPrivacyRules(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in
            switch UInt32(bitPattern: try reader.readInt32()) {
            case 0xfffe1bac, 0x65427b82, 0xf888fa1a, 0x8b73e763, 0xf7e8d89b, 0xece9814b,
                 0x21461b5d, 0xa27bb1d0, 0xf6a5f82f:
                break
            case 0xb8905fb2, 0xe4621141, 0x6b134e8e, 0x41c87565:
                _ = try reader.readVector { reader in try reader.readInt64() }
            default:
                throw TelegramAPIError.invalidResponse
            }
            return ()
        }
    }

    private func skipMediaArea(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xbe82db9c: // mediaAreaVenue
            try skipMediaAreaCoordinatesBody(&reader)
            try skipGeoPoint(&reader)
            _ = try reader.readString()
            _ = try reader.readString()
            _ = try reader.readString()
            _ = try reader.readString()
            _ = try reader.readString()
        case 0xcad5452d: // mediaAreaGeoPoint
            let flags = try reader.readInt32()
            try skipMediaAreaCoordinatesBody(&reader)
            try skipGeoPoint(&reader)
            if flags & 1 != 0 { try skipGeoPointAddress(&reader) }
        case 0x14455871: // mediaAreaSuggestedReaction
            _ = try reader.readInt32()
            try skipMediaAreaCoordinatesBody(&reader)
            try skipReaction(&reader)
        case 0x770416af: // mediaAreaChannelPost
            try skipMediaAreaCoordinatesBody(&reader)
            _ = try reader.readInt64()
            _ = try reader.readInt32()
        case 0x37381085: // mediaAreaUrl
            try skipMediaAreaCoordinatesBody(&reader)
            _ = try reader.readString()
        case 0x49a6549c: // mediaAreaWeather
            try skipMediaAreaCoordinatesBody(&reader)
            _ = try reader.readString()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
        case 0x5787686d: // mediaAreaStarGift
            try skipMediaAreaCoordinatesBody(&reader)
            _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipMediaAreaCoordinatesBody(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xcfc9e002 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt64()
        _ = try reader.readInt64()
        _ = try reader.readInt64()
        _ = try reader.readInt64()
        _ = try reader.readInt64()
        if flags & 1 != 0 { _ = try reader.readInt64() }
    }

    private func skipGeoPointAddress(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xde4c5d93 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readString()
        if flags & 1 != 0 { _ = try reader.readString() }
        if flags & 2 != 0 { _ = try reader.readString() }
        if flags & 4 != 0 { _ = try reader.readString() }
    }
    private func skipExtendedMedia(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1386050360:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { try skipPhotoSize(&reader) }
            if flags & 4 != 0 { _ = try reader.readInt32() }
        case -297296796:
            _ = try skipMessageMedia(&reader)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipPhotoSize(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 236446268:
            _ = try reader.readString()
        case 1976012384:
            _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
        case 35527382:
            _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readBytes()
        case -525288402, -668906175:
            _ = try reader.readString(); _ = try reader.readBytes()
        case -96535659:
            _ = try reader.readString(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readVector { reader in try reader.readInt32() }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipMessageEntities(_ reader: inout TLReader) throws {
        _ = try reader.readVector { reader in try skipMessageEntity(&reader); return () }
    }
    private func skipMessageEntity(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1148011883, -100378723, 1868782349, 1827637959, 1859134776,
             1692693954, -1117713463, -2106619040, 681706865, -1687559349,
             1280209983, -1672577397, -1090087980, 1981704948, 852137487:
            _ = try reader.readInt32(); _ = try reader.readInt32()
        case 1938967520:
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readString()
        case 1990644519:
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readString()
        case -595914432, -925956616:
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt64()
        case -238245204:
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
        case -1874147385:
            _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32(); _ = try reader.readInt32()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipMessageReplies(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        _ = try reader.readInt32(); _ = try reader.readInt32()
        if flags & 2 != 0 { _ = try reader.readVector { reader in try readPeer(&reader) } }
        if flags & 1 != 0 { _ = try reader.readInt64() }
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readInt32() }
    }
    private func skipMessageReactions(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x0a339f0b else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readVector { reader in try skipReactionCount(&reader); return () }
        if flags & 2 != 0 { _ = try reader.readVector { reader in try skipMessagePeerReaction(&reader); return () } }
        if flags & 16 != 0 { _ = try reader.readVector { reader in try skipMessageReactor(&reader); return () } }
    }
    private func skipReactionCount(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xa3d1cb80 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readInt32() }
        try skipReaction(&reader)
        _ = try reader.readInt32()
    }
    private func skipReaction(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case 2046153753, 1379771627:
            break
        case 455247544:
            _ = try reader.readString()
        case -1992950669:
            _ = try reader.readInt64()
        case Int32(bitPattern: 0x523da4eb): // reactionPaid
            break
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
    private func skipMessagePeerReaction(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x8c79b63c else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = flags
        _ = try readPeer(&reader)
        _ = try reader.readInt32()
        try skipReaction(&reader)
    }
    private func skipMessageReactor(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x4ba3a95a else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 8 != 0 { _ = try readPeer(&reader) }
        _ = try reader.readInt32()
    }
    private func skipFactCheck(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 2 != 0 {
            _ = try reader.readString()
            try skipTextWithEntities(&reader)
        }
        _ = try reader.readInt64()
    }
    private func skipSuggestedPost(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        if flags & 8 != 0 { try skipStarsAmount(&reader) }
        if flags & 1 != 0 { _ = try reader.readInt32() }
    }
    private func skipStarsAmount(_ reader: inout TLReader) throws {
        switch try reader.readInt32() {
        case -1145654109:
            _ = try reader.readInt64(); _ = try reader.readInt32()
        case 1957618656:
            _ = try reader.readInt64()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipTodoItem(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == -878074577 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readInt32()
        try skipTextWithEntities(&reader)
    }

    private func skipTodoList(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 1236871718 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readInt32() // flags
        try skipTextWithEntities(&reader)
        _ = try reader.readVector { reader in
            try skipTodoItem(&reader)
            return ()
        }
    }

    private func skipTodoCompletion(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 572241380 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readInt32()
        _ = try readPeer(&reader)
        _ = try reader.readInt32()
    }

    private func skipBirthday(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 1821253126 else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        _ = try reader.readInt32()
        _ = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readInt32() }
    }

    private func skipRequestedPeer(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xd62ff46a: // requestedPeerUser
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { try skipPhoto(&reader) }
        case 0x7307544f: // requestedPeerChat
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { try skipPhoto(&reader) }
        case 0x8ba403e4: // requestedPeerChannel
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 1 != 0 { _ = try reader.readString() }
            if flags & 2 != 0 { _ = try reader.readString() }
            if flags & 4 != 0 { try skipPhoto(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipStarGift(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x313a9547: // starGift
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            try skipDocument(&reader)
            _ = try reader.readInt64()
            if flags & 1 != 0 {
                _ = try reader.readInt32()
                _ = try reader.readInt32()
            }
            if flags & 16 != 0 { _ = try reader.readInt64() }
            _ = try reader.readInt64()
            if flags & 2 != 0 {
                _ = try reader.readInt32()
                _ = try reader.readInt32()
            }
            if flags & 8 != 0 { _ = try reader.readInt64() }
            if flags & 16 != 0 { _ = try reader.readInt64() }
            if flags & 32 != 0 { _ = try reader.readString() }
            if flags & 64 != 0 { _ = try readPeer(&reader) }
            if flags & 256 != 0 {
                _ = try reader.readInt32()
                _ = try reader.readInt32()
            }
            if flags & 512 != 0 { _ = try reader.readInt32() }
            if flags & 2048 != 0 {
                _ = try reader.readString()
                _ = try reader.readInt32()
                _ = try reader.readInt32()
            }
            if flags & 4096 != 0 { _ = try reader.readInt32() }
            if flags & 8192 != 0 { try skipStarGiftBackground(&reader) }
        case 0x85f0a9cd: // starGiftUnique
            try skipStarGiftUniqueBody(&reader)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipStarGiftUniqueBody(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        _ = try reader.readInt64()
        _ = try reader.readInt64()
        _ = try reader.readString()
        _ = try reader.readString()
        _ = try reader.readInt32()
        if flags & 1 != 0 { _ = try readPeer(&reader) }
        if flags & 2 != 0 { _ = try reader.readString() }
        if flags & 4 != 0 { _ = try reader.readString() }
        _ = try reader.readVector { reader in
            try skipStarGiftAttribute(&reader)
            return ()
        }
        _ = try reader.readInt32()
        _ = try reader.readInt32()
        if flags & 8 != 0 { _ = try reader.readString() }
        if flags & 16 != 0 {
            _ = try reader.readVector { reader in
                try skipStarsAmount(&reader)
                return ()
            }
        }
        if flags & 32 != 0 { _ = try readPeer(&reader) }
        if flags & 256 != 0 {
            _ = try reader.readInt64()
            _ = try reader.readString()
            _ = try reader.readInt64()
        }
        if flags & 1024 != 0 { _ = try readPeer(&reader) }
        if flags & 2048 != 0 { try skipPeerColor(&reader) }
        if flags & 4096 != 0 { _ = try reader.readInt64() }
        if flags & 8192 != 0 { _ = try reader.readInt32() }
        if flags & 65536 != 0 { _ = try reader.readInt32() }
    }

    private func skipStarGiftAttribute(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x565251e2: // starGiftAttributeModel
            let flags = try reader.readInt32()
            _ = try reader.readString()
            try skipDocument(&reader)
            try skipStarGiftAttributeRarity(&reader)
            _ = flags
        case 0x4e7085ea: // starGiftAttributePattern
            _ = try reader.readString()
            try skipDocument(&reader)
            try skipStarGiftAttributeRarity(&reader)
        case 0x9f2504e4: // starGiftAttributeBackdrop
            _ = try reader.readString()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            try skipStarGiftAttributeRarity(&reader)
        case 0xe0bff26c: // starGiftAttributeOriginalDetails
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try readPeer(&reader) }
            _ = try readPeer(&reader)
            _ = try reader.readInt32()
            if flags & 2 != 0 { try skipTextWithEntities(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipStarGiftAttributeRarity(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x36437737:
            _ = try reader.readInt32()
        case 0xdbce6389, 0xf08d516b, 0x78fbf3a8, 0xcef7e7a8:
            break
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipStarGiftBackground(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xaff56398 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readInt32()
        _ = try reader.readInt32()
        _ = try reader.readInt32()
    }

    private func skipSecureValueType(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x9d2a81e3, 0x3dac6a00, 0x06e425c4, 0xa0d0744b,
             0x99a48f23, 0xcbe31e26, 0xfc36954e, 0x89137c0d,
             0x8b883488, 0x99e3806a, 0xea02ec33, 0xb320aadb,
             0x8e3ca7ee:
            break
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipSecureFile(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x64199744:
            break
        case 0x7d09c27e:
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            _ = try reader.readBytes()
            _ = try reader.readBytes()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipSecureData(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x8aeabec3 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readBytes()
        _ = try reader.readBytes()
        _ = try reader.readBytes()
    }

    private func skipSecurePlainData(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x7d6099dd, 0x21ec5a5f:
            _ = try reader.readString()
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipSecureValue(_ reader: inout TLReader) throws {
        let flags = try reader.readInt32()
        try skipSecureValueType(&reader)
        if flags & 1 != 0 { try skipSecureData(&reader) }
        if flags & 2 != 0 { try skipSecureFile(&reader) }
        if flags & 4 != 0 { try skipSecureFile(&reader) }
        if flags & 64 != 0 {
            _ = try reader.readVector { reader in
                try skipSecureFile(&reader)
                return ()
            }
        }
        if flags & 16 != 0 {
            _ = try reader.readVector { reader in
                try skipSecureFile(&reader)
                return ()
            }
        }
        if flags & 32 != 0 { try skipSecurePlainData(&reader) }
        _ = try reader.readBytes()
    }

    private func skipSecureCredentialsEncrypted(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x33f0ea47 else {
            throw TelegramAPIError.invalidResponse
        }
        _ = try reader.readBytes()
        _ = try reader.readBytes()
        _ = try reader.readBytes()
    }

    private func skipChatTheme(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xc3dffc04:
            _ = try reader.readString()
        case 0x3458f9c8:
            try skipStarGift(&reader)
            _ = try reader.readVector { reader in
                try skipThemeSettings(&reader)
                return ()
            }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipThemeSettings(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0xfa58b6d4 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        try skipBaseTheme(&reader)
        _ = try reader.readInt32() // accent_color
        if flags & 8 != 0 { _ = try reader.readInt32() }
        if flags & 1 != 0 { _ = try reader.readVector { reader in try reader.readInt32() } }
        if flags & 2 != 0 { try skipWallPaper(&reader) }
    }

    private func skipWallPaper(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xa437c3ed:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            _ = try reader.readInt64() // access_hash
            _ = try reader.readString()
            try skipDocument(&reader)
            if flags & 4 != 0 { try skipWallPaperSettings(&reader) }
        case 0xe0804116:
            let flags = try reader.readInt32()
            _ = try reader.readInt64()
            if flags & 4 != 0 { try skipWallPaperSettings(&reader) }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipWallPaperSettings(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x372efcd0 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 1 != 0 { _ = try reader.readInt32() }
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 32 != 0 { _ = try reader.readInt32() }
        if flags & 64 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readInt32() }
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 128 != 0 { _ = try reader.readString() }
    }

    private func skipBaseTheme(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xc3a12462, 0xfbd81688, 0xb7b31ea8, 0x6d5f77ee, 0x5b11125a:
            break
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipTextWithEntities(_ reader: inout TLReader) throws {
        _ = try reader.readString()
        try skipMessageEntities(&reader)
    }
    private func skipPoll(_ reader: inout TLReader) throws {
        guard try reader.readInt32() == 1484026161 else { throw TelegramAPIError.invalidResponse }
        _ = try reader.readInt64()
        let flags = try reader.readInt32()
        try skipTextWithEntities(&reader)
        _ = try reader.readVector { reader in
            guard try reader.readInt32() == -15277366 else { throw TelegramAPIError.invalidResponse }
            try skipTextWithEntities(&reader)
            _ = try reader.readBytes()
            return ()
        }
        if flags & 16 != 0 { _ = try reader.readInt32() }
        if flags & 32 != 0 { _ = try reader.readInt32() }
    }
    private func skipPollResults(_ reader: inout TLReader) throws {
        guard UInt32(bitPattern: try reader.readInt32()) == 0x7adf2420 else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        if flags & 2 != 0 {
            _ = try reader.readVector { reader in
                guard try reader.readInt32() == 997055186 else { throw TelegramAPIError.invalidResponse }
                let resultFlags = try reader.readInt32()
                _ = resultFlags
                _ = try reader.readBytes(); _ = try reader.readInt32()
                return ()
            }
        }
        if flags & 4 != 0 { _ = try reader.readInt32() }
        if flags & 8 != 0 { _ = try reader.readVector { reader in try readPeer(&reader) } }
        if flags & 16 != 0 {
            _ = try reader.readString()
            try skipMessageEntities(&reader)
        }
    }
}

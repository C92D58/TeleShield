import Foundation

extension TelegramAPI {
    func contactUserIDs() async throws -> Set<Int64> {
        var request = TLWriter()
        request.writeInt32(1574346258)
        request.writeInt64(0)
        let response = try await call(request.data)
        var reader = TLReader(response)
        switch try reader.readInt32() {
        case Int32(bitPattern: 0xb74ba9d2): // contacts.contactsNotModified
            // This implementation does not keep a contact-list hash/cache;
            // treating "not modified" as an empty list would make every
            // unknown sender actionable. Fail closed instead.
            throw TelegramAPIError.invalidResponse
        case Int32(bitPattern: 0xeae87e42): // contacts.contacts
            let contacts = try reader.readVector { reader in
                guard try reader.readInt32() == Int32(bitPattern: 0x145ade0b) else {
                    throw TelegramAPIError.invalidResponse
                }
                let userID = try reader.readInt64()
                _ = try reader.readBool()
                return userID
            }
            _ = try reader.readInt32() // saved_count
            _ = try reader.readVector { reader in try readUser(&reader) }
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return Set(contacts)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    /// Returns the participant role for a supergroup/channel. We query the
    /// target user instead of assuming that a message author is removable;
    /// Telegram admins and creators must be protected from destructive actions.
    func isAdministrator(_ user: NativeUser, in chat: NativeChat) async throws -> Bool {
        if !chat.isChannel {
            return try await basicGroupAdministratorStatus(userID: user.id, chatID: chat.id)
        }
        guard let channelAccessHash = chat.accessHash, let userAccessHash = user.accessHash else {
            // Missing hashes do not prove that a user is removable. Callers
            // treat this as a fail-closed error before destructive actions.
            throw TelegramAPIError.invalidResponse
        }
        var request = TLWriter()
        request.writeInt32(Int32(bitPattern: 0xa0ab6cc6))
        request.writeInt32(Int32(bitPattern: 0xf35aec28))
        request.writeInt64(chat.id)
        request.writeInt64(channelAccessHash)
        writeInputPeer(&request, .user(id: user.id, accessHash: userAccessHash))
        let response = try await call(request.data)
        var reader = TLReader(response)
        guard try reader.readInt32() == Int32(bitPattern: 0xdfb80317) else {
            throw TelegramAPIError.invalidResponse
        }
        let isAdministrator = try readChannelParticipant(&reader, targetUserID: user.id)
        // channels.channelParticipant contains the participant followed by
        // auxiliary chats/users vectors.  They are not needed for the
        // authorization decision, but must be consumed before validating the
        // response boundary; otherwise every real response is rejected as
        // trailing garbage and group protection always fails closed.
        _ = try readChatVector(&reader)
        _ = try readUserVector(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return isAdministrator
    }

    func block(user: NativeUser) async throws {
        guard let accessHash = user.accessHash else { throw TelegramAPIError.invalidResponse }
        var request = TLWriter()
        request.writeInt32(774801204)
        request.writeInt32(0)
        request.writeInt32(-571955892)
        request.writeInt64(user.id)
        request.writeInt64(accessHash)
        _ = try await call(request.data)
    }

    func unblock(user: NativeUser) async throws {
        guard let accessHash = user.accessHash else { throw TelegramAPIError.invalidResponse }
        var request = TLWriter()
        request.writeInt32(-1252994264)
        request.writeInt32(0)
        request.writeInt32(-571955892)
        request.writeInt64(user.id)
        request.writeInt64(accessHash)
        _ = try await call(request.data)
    }

    func kick(user: NativeUser, from chat: NativeChat) async throws {
        var request = TLWriter()
        if chat.isChannel {
            guard let chatAccessHash = chat.accessHash, let userAccessHash = user.accessHash else {
                throw TelegramAPIError.invalidResponse
            }
            request.writeInt32(-1763259007) // channels.editBanned
            request.writeInt32(-212145112) // inputChannel
            request.writeInt64(chat.id)
            request.writeInt64(chatAccessHash)
            request.writeInt32(-571955892) // inputPeerUser
            request.writeInt64(user.id)
            request.writeInt64(userAccessHash)
            request.writeInt32(-1626209256) // chatBannedRights
            request.writeInt32(1)
            request.writeInt32(0)
        } else {
            guard user.accessHash != nil else { throw TelegramAPIError.invalidResponse }
            request.writeInt32(Int32(bitPattern: 0xa2185cab)) // messages.deleteChatUser
            request.writeInt32(0) // revoke_history = false
            request.writeInt64(chat.id)
            writeInputUser(&request, user)
        }
        _ = try await call(request.data)
    }
}

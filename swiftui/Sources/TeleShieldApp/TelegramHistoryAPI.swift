import Foundation

struct TelegramDialogPage: Sendable {
    let dialogs: [NativeDialog]
    let messages: [NativeMessage]
    let chats: [NativeChat]
    let users: [NativeUser]
}

struct TelegramHistoryPage: Sendable {
    let messages: [NativeMessage]
    let chats: [NativeChat]
    let users: [NativeUser]
}

extension TelegramAPI {
    /// Internal response boundary used by package tests and offline fixtures.
    /// Keeping the decoder behind the API actor mirrors the production call
    /// path without opening any socket or requiring a Telegram account.
    func parseHistoryResponse(_ data: Data) throws -> TelegramHistoryPage {
        try parseMessages(data)
    }

    func getDialogs(limit: Int) async throws -> TelegramDialogPage {
        var request = TLWriter()
        request.writeInt32(-1594569905)
        request.writeInt32(1) // exclude_pinned
        request.writeInt32(0) // offset_date
        request.writeInt32(0) // offset_id
        request.writeInt32(2134579434)
        request.writeInt32(Int32(max(1, min(limit, 100))))
        request.writeInt64(0)
        let response = try await call(request.data)
        return try parseDialogs(response)
    }

    func getHistory(peer: NativePeer, limit: Int, offsetID: Int32 = 0, minID: Int32 = 0) async throws -> TelegramHistoryPage {
        var request = TLWriter()
        request.writeInt32(1143203525)
        writeInputPeer(&request, peer)
        request.writeInt32(offsetID)
        request.writeInt32(0)
        request.writeInt32(0)
        request.writeInt32(Int32(max(1, min(limit, 100))))
        request.writeInt32(0)
        request.writeInt32(max(0, minID))
        request.writeInt64(0)
        let response = try await call(request.data)
        return try parseMessages(response)
    }
}

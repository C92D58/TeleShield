import XCTest
@testable import TeleShieldApp

final class NativeStackTests: XCTestCase {
    func testTLBytesRoundTripPreservesPadding() throws {
        var writer = TLWriter()
        writer.writeInt32(42)
        try writer.writeString("TeleShield")
        try writer.writeBytes(Data(repeating: 0x7f, count: 257))

        var reader = TLReader(writer.data)
        XCTAssertEqual(try reader.readInt32(), 42)
        XCTAssertEqual(try reader.readString(), "TeleShield")
        XCTAssertEqual(try reader.readBytes(), Data(repeating: 0x7f, count: 257))
        XCTAssertEqual(reader.remaining, 0)
    }

    func testTLWriterRejectsOversizedBytes() {
        var writer = TLWriter()
        XCTAssertThrowsError(try writer.writeBytes(Data(repeating: 0, count: 0x00ff_ffff + 1))) { error in
            XCTAssertEqual(error as? TLCodecError, .invalidLength)
        }
    }

    func testAESIGERoundTrip() throws {
        let key = Data((0..<32).map(UInt8.init))
        let iv = Data((32..<64).map(UInt8.init))
        let plaintext = Data((0..<32).map { UInt8(($0 * 3) & 0xff) })

        let encrypted = try MTProtoCrypto.aesIGE(plaintext, key: key, iv: iv, encrypt: true)
        let decrypted = try MTProtoCrypto.aesIGE(encrypted, key: key, iv: iv, encrypt: false)

        XCTAssertNotEqual(encrypted, plaintext)
        XCTAssertEqual(decrypted, plaintext)
    }

    func testAESIGEMatchesIndependentKnownVector() throws {
        let key = Data((0..<32).map(UInt8.init))
        let iv = Data((32..<64).map(UInt8.init))
        let plaintext = Data((0..<32).map(UInt8.init))
        let expected = Data(hex: "42e66e1a756cccf5b27acc47523ad074ee39bf54e3db37bbdf415df6b400fca9")

        XCTAssertEqual(try MTProtoCrypto.aesIGE(plaintext, key: key, iv: iv, encrypt: true), expected)
    }

    func testAESCTRMatchesNISTBigEndianCounterVector() throws {
        let key = Data(hex: "603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4")
        let iv = Data(hex: "f0f1f2f3f4f5f6f7f8f9fafbfcfdfeff")
        let plaintext = Data(hex: "6bc1bee22e409f96e93d7e117393172aae2d8a571e03ac9c9eb76fac45af8e5130c81c46a35ce411e5fbc1191a0a52eff69f2445df4f9b17ad2b417be66c3710")
        let expected = Data(hex: "601ec313775789a5b7a7f504bbf3d228f443e3ca4d62b59aca84e990cacaf5c52b0930daa23de94ce87017ba2d84988ddfc9c58db67aada613c2dd08457941a6")

        XCTAssertEqual(try MTProtoCrypto.aesCTR(plaintext, key: key, iv: iv), expected)
    }

    func testGZipDecoderRoundTrip() throws {
        let compressed = try XCTUnwrap(Data(base64Encoded: "H4sIAAAAAAAAAytJzUlNL0rM1U2vyizQLUktLuECAPTbByQTAAAA"))
        XCTAssertEqual(try GZipDecoder.decompress(compressed), Data("telegram-gzip-test\n".utf8))
    }

    func testGZipDecoderHonorsOutputLimit() throws {
        let compressed = try XCTUnwrap(Data(base64Encoded: "H4sIAAAAAAAAAytJzUlNL0rM1U2vyizQLUktLuECAPTbByQTAAAA"))
        XCTAssertThrowsError(try GZipDecoder.decompress(compressed, maximumOutputSize: 4)) { error in
            XCTAssertEqual(error as? GZipDecoderError, .outputLimitExceeded)
        }
    }

    func testEncryptedMessageRoundTripIncludesSessionEnvelope() throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let message = Data([1, 2, 3, 4])
        let encrypted = try MTProtoCrypto.encryptedMessage(
            authKey: authKey,
            salt: 7,
            sessionID: 8,
            messageID: 9,
            sequence: 1,
            body: message,
            direction: .serverToClient
        )
        let decrypted = try MTProtoCrypto.decryptMessage(
            authKey: authKey,
            sessionID: 8,
            packet: encrypted.packet
        )
        XCTAssertEqual(decrypted.salt, 7)
        XCTAssertEqual(decrypted.messageID, 9)
        XCTAssertEqual(decrypted.sequence, 1)
        XCTAssertEqual(decrypted.body, message)
    }

    func testEncryptedMessageRejectsTampering() throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let encrypted = try MTProtoCrypto.encryptedMessage(
            authKey: authKey,
            salt: 7,
            sessionID: 8,
            messageID: 9,
            sequence: 1,
            body: Data([1, 2, 3, 4]),
            direction: .serverToClient
        )
        var tampered = encrypted.packet
        tampered[tampered.index(before: tampered.endIndex)] ^= 0x01
        XCTAssertThrowsError(try MTProtoCrypto.decryptMessage(authKey: authKey, sessionID: 8, packet: tampered))
    }

    func testMTProtoClientUsesInjectedTransportForRPCResult() async throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 7, serverSalt: 11, date: Date())
        let transport = ScriptedMTProtoTransport(authKey: authKey, result: Data([9, 8, 7, 6]), sessionID: 77)
        let client = MTProtoClient(apiID: 123, session: session, dcID: 2, transport: transport, sessionID: 77)

        let result = try await client.call(Data([1, 2, 3, 4]), wrapInInitConnection: false)

        XCTAssertEqual(result, Data([9, 8, 7, 6]))
        let sendCount = await transport.sendCount
        let receiveCount = await transport.receiveCount
        XCTAssertEqual(sendCount, 1)
        XCTAssertEqual(receiveCount, 1)
    }

    func testMTProtoClientRetriesBadServerSaltForSameRequest() async throws {
        let authKey = Data((0..<256).map { UInt8((255 - $0) & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 7, serverSalt: 11, date: Date())
        let transport = ScriptedMTProtoTransport(
            authKey: authKey,
            result: Data([4, 3, 2, 1]),
            firstResponse: .badServerSalt(99),
            sessionID: 88
        )
        let client = MTProtoClient(apiID: 123, session: session, dcID: 2, transport: transport, sessionID: 88)

        let result = try await client.call(Data([1, 2, 3, 4]), wrapInInitConnection: false)

        XCTAssertEqual(result, Data([4, 3, 2, 1]))
        let sendCount = await transport.sendCount
        let receiveCount = await transport.receiveCount
        XCTAssertEqual(sendCount, 2)
        XCTAssertEqual(receiveCount, 2)
    }

    func testAuthRequestsUseLayer223FieldOrder() async throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 0, serverSalt: 11, date: Date())
        let transport = RecordingMTProtoTransport(
            authKey: authKey,
            resultBodies: [try makeSentCodeFixture(), try makeAuthorizationFixture()],
            sessionID: 91
        )
        let api = TelegramAPI(
            apiID: 123,
            apiHash: "fixture-hash",
            session: session,
            transport: transport,
            sessionID: 91
        )

        let sentCode = try await api.sendCode(phone: "+886900000000")
        XCTAssertEqual(sentCode.phoneCodeHash, "fixture-code-hash")
        XCTAssertEqual(sentCode.deliveryDescription, "SMS")

        let user = try await api.signIn(phone: "+886900000000", phoneCodeHash: "fixture-code-hash", code: "12345")
        XCTAssertEqual(user.id, 99)

        let requestBodies = await transport.requestBodiesSnapshot
        XCTAssertEqual(requestBodies.count, 2)

        var sendCodeReader = TLReader(try unwrapInitConnectionQuery(requestBodies[0]))
        let sendCodeConstructor = try sendCodeReader.readInt32()
        let sendCodePhone = try sendCodeReader.readString()
        let sendCodeAPIID = try sendCodeReader.readInt32()
        let sendCodeAPIHash = try sendCodeReader.readString()
        let sendCodeSettings = try sendCodeReader.readInt32()
        let sendCodeSettingsFlags = try sendCodeReader.readInt32()
        XCTAssertEqual(sendCodeConstructor, -1502141361)
        XCTAssertEqual(sendCodePhone, "+886900000000")
        XCTAssertEqual(sendCodeAPIID, 123)
        XCTAssertEqual(sendCodeAPIHash, "fixture-hash")
        XCTAssertEqual(sendCodeSettings, -1390068360)
        XCTAssertEqual(sendCodeSettingsFlags, 0)
        XCTAssertEqual(sendCodeReader.remaining, 0)

        var signInReader = TLReader(try unwrapInitConnectionQuery(requestBodies[1]))
        let signInConstructor = try signInReader.readInt32()
        let signInFlags = try signInReader.readInt32()
        let signInPhone = try signInReader.readString()
        let signInHash = try signInReader.readString()
        let signInCode = try signInReader.readString()
        XCTAssertEqual(signInConstructor, Int32(bitPattern: 0x8d52a951))
        XCTAssertEqual(signInFlags, 1)
        XCTAssertEqual(signInPhone, "+886900000000")
        XCTAssertEqual(signInHash, "fixture-code-hash")
        XCTAssertEqual(signInCode, "12345")
        XCTAssertEqual(signInReader.remaining, 0)
    }

    func testTelegramHistoryFixtureAcceptsCurrentNotModifiedShape() async throws {
        var writer = TLWriter()
        writer.writeInt32(Int32(bitPattern: 0x74535f21))
        writer.writeInt32(37) // count

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseHistoryResponse(writer.data)

        XCTAssertTrue(page.messages.isEmpty)
        XCTAssertTrue(page.chats.isEmpty)
        XCTAssertTrue(page.users.isEmpty)
    }

    func testTelegramHistoryFixtureAcceptsCurrentMessagesSliceVectors() async throws {
        var writer = TLWriter()
        writer.writeInt32(Int32(bitPattern: 0x5f206716)) // messages.messagesSlice
        writer.writeInt32(0) // flags
        writer.writeInt32(0) // count
        for _ in 0..<4 {
            writer.writeInt32(TLConstructor.vector)
            writer.writeInt32(0)
        }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseHistoryResponse(writer.data)

        XCTAssertTrue(page.messages.isEmpty)
        XCTAssertTrue(page.chats.isEmpty)
        XCTAssertTrue(page.users.isEmpty)
    }

    func testTelegramHistoryFixtureConsumesCurrentMessageMediaLayout() async throws {
        var message = TLWriter()
        message.writeInt32(Int32(bitPattern: 0x3ae56482)) // message
        message.writeInt32(512) // media present
        message.writeInt32(0) // flags2
        message.writeInt32(1) // id
        message.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
        message.writeInt64(7)
        message.writeInt32(1_700_000_000)
        try message.writeString("fixture")
        message.writeInt32(Int32(bitPattern: 0x695150d7)) // messageMediaPhoto
        message.writeInt32(0) // no photo, no ttl

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x1d73e7ea)) // messages.messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(1)
        response.append(message.data)
        for _ in 0..<3 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseHistoryResponse(response.data)

        XCTAssertEqual(page.messages.count, 1)
        XCTAssertEqual(page.messages.first?.text, "fixture")
        XCTAssertFalse(page.messages.first?.hasPhoto ?? true)
    }

    func testTelegramHistoryFixtureConsumesCurrentStoryMediaLayout() async throws {
        var message = TLWriter()
        message.writeInt32(Int32(bitPattern: 0x3ae56482)) // message
        message.writeInt32(512) // media present
        message.writeInt32(0) // flags2
        message.writeInt32(2) // id
        message.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
        message.writeInt64(7)
        message.writeInt32(1_700_000_001)
        try message.writeString("story fixture")
        message.writeInt32(Int32(bitPattern: 0x68cb6283)) // messageMediaStory
        message.writeInt32(0) // via_mention is a flag, not a serialized Bool
        message.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
        message.writeInt64(9)
        message.writeInt32(10) // story id

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x1d73e7ea)) // messages.messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(1)
        response.append(message.data)
        for _ in 0..<3 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseHistoryResponse(response.data)

        XCTAssertEqual(page.messages.count, 1)
        XCTAssertEqual(page.messages.first?.text, "story fixture")
        XCTAssertFalse(page.messages.first?.hasPhoto ?? true)
    }

    func testTelegramUpdateStateFixtureUsesLayer223Shape() async throws {
        var writer = TLWriter()
        writer.writeInt32(Int32(bitPattern: 0xa56c2a3e)) // updates.state
        writer.writeInt32(101)
        writer.writeInt32(202)
        writer.writeInt32(1_700_000_000)
        writer.writeInt32(303)
        writer.writeInt32(4)

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let state = try await api.parseUpdateStateResponse(writer.data)

        XCTAssertEqual(state, NativeUpdateState(pts: 101, qts: 202, date: 1_700_000_000, seq: 303, unreadCount: 4))
    }

    func testTelegramChannelDifferenceEmptyFixtureUsesIndependentPTS() async throws {
        var writer = TLWriter()
        writer.writeInt32(Int32(bitPattern: 0x3e11affb)) // updates.channelDifferenceEmpty
        writer.writeInt32(1) // final
        writer.writeInt32(4_242) // pts

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseChannelDifferenceResponse(writer.data, channelID: 9001)

        guard case .empty(let state, let isFinal) = page else {
            XCTFail("Expected updates.channelDifferenceEmpty")
            return
        }
        XCTAssertTrue(isFinal)
        XCTAssertEqual(state, NativeChannelUpdateState(channelID: 9001, pts: 4_242))
    }

    func testTelegramChannelDifferenceFixtureConsumesMessagesAndEntityVectors() async throws {
        var message = TLWriter()
        message.writeInt32(Int32(bitPattern: 0x3ae56482)) // message
        message.writeInt32(256) // sender_id present
        message.writeInt32(0) // flags2
        message.writeInt32(88) // id
        writeFixtureUserPeer(&message, id: 77)
        message.writeInt32(-1_566_230_754) // peerChannel
        message.writeInt64(9001)
        message.writeInt32(1_700_000_100)
        try message.writeString("channel difference fixture")

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x2064674e)) // updates.channelDifference
        response.writeInt32(1) // final
        response.writeInt32(4_243) // pts
        response.writeVector([message.data]) { writer, value in writer.append(value) }
        response.writeVector([Int32]()) { _, _ in }
        response.writeVector([Int32]()) { _, _ in }
        response.writeVector([Int32]()) { _, _ in }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseChannelDifferenceResponse(response.data, channelID: 9001)

        guard case .difference(let difference) = page else {
            XCTFail("Expected updates.channelDifference")
            return
        }
        XCTAssertTrue(difference.isFinal)
        XCTAssertEqual(difference.state, NativeChannelUpdateState(channelID: 9001, pts: 4_243))
        XCTAssertEqual(difference.messages.map(\.id), [88])
        XCTAssertEqual(difference.messages.first?.peerID, 9001)
        XCTAssertEqual(difference.messages.first?.senderID, 77)
    }

    func testTelegramChannelDifferenceTooLongResetsToDialogPTS() async throws {
        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0xa4bcc6fe)) // updates.channelDifferenceTooLong
        response.writeInt32(1) // final
        response.writeInt32(Int32(bitPattern: 0xd58a08c6)) // dialog
        response.writeInt32(1) // pts is present
        response.writeInt32(-1_566_230_754) // peerChannel
        response.writeInt64(9001)
        for _ in 0..<6 { response.writeInt32(0) }
        response.writeInt32(-1_721_619_444) // peerNotifySettings
        response.writeInt32(0)
        response.writeInt32(4_500) // dialog pts
        for _ in 0..<3 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseChannelDifferenceResponse(response.data, channelID: 9001)

        guard case .tooLong(let state) = page else {
            XCTFail("Expected updates.channelDifferenceTooLong")
            return
        }
        XCTAssertEqual(state, NativeChannelUpdateState(channelID: 9001, pts: 4_500))
    }

    func testTelegramChannelDifferenceRequestUsesInputChannelAndEmptyFilter() async throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 7, serverSalt: 11, date: Date())
        var empty = TLWriter()
        empty.writeInt32(Int32(bitPattern: 0x3e11affb)) // updates.channelDifferenceEmpty
        empty.writeInt32(1) // final
        empty.writeInt32(321)
        let transport = RecordingMTProtoTransport(authKey: authKey, resultBodies: [empty.data], sessionID: 93)
        let api = TelegramAPI(apiID: 123, apiHash: "fixture", session: session, transport: transport, sessionID: 93)
        let channel = NativeChat(
            id: 9001,
            accessHash: 123_456,
            title: "Fixture channel",
            username: "fixture",
            isChannel: true,
            isBroadcast: false,
            isMegagroup: true,
            adminRights: true
        )

        let difference = try await api.getChannelDifference(channel: channel, from: 320, limit: 500)
        XCTAssertEqual(difference.state, NativeChannelUpdateState(channelID: 9001, pts: 321))

        let requestBodies = await transport.requestBodiesSnapshot
        XCTAssertEqual(requestBodies.count, 1)
        var reader = TLReader(try unwrapInitConnectionQuery(requestBodies[0]))
        XCTAssertEqual(try reader.readInt32(), Int32(bitPattern: 0x03173d78))
        XCTAssertEqual(try reader.readInt32(), 0) // force
        XCTAssertEqual(try reader.readInt32(), Int32(bitPattern: 0xf35aec28))
        XCTAssertEqual(try reader.readInt64(), 9001)
        XCTAssertEqual(try reader.readInt64(), 123_456)
        XCTAssertEqual(try reader.readInt32(), Int32(bitPattern: 0x94d42ee7))
        XCTAssertEqual(try reader.readInt32(), 320)
        XCTAssertEqual(try reader.readInt32(), 100) // clamped protocol limit
        XCTAssertEqual(reader.remaining, 0)
    }

    func testChannelParticipantResponseConsumesAuxiliaryVectors() async throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 7, serverSalt: 11, date: Date())

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0xdfb80317)) // channels.channelParticipant
        response.writeInt32(Int32(bitPattern: 0x2fe601d3)) // channelParticipantCreator
        response.writeInt32(0) // flags
        response.writeInt64(42) // user_id
        response.writeInt32(Int32(bitPattern: 0x5fb224d5)) // chatAdminRights
        response.writeInt32(0)
        response.writeVector([Int32]()) { _, _ in }
        response.writeVector([Int32]()) { _, _ in }

        let transport = RecordingMTProtoTransport(authKey: authKey, resultBodies: [response.data], sessionID: 94)
        let api = TelegramAPI(apiID: 123, apiHash: "fixture", session: session, transport: transport, sessionID: 94)
        let user = NativeUser(id: 42, accessHash: 123, firstName: "Admin", lastName: "", username: "", phone: nil, isSelf: false, isBot: false)
        let chat = NativeChat(id: 9001, accessHash: 456, title: "Fixture group", username: "fixture", isChannel: true, isBroadcast: false, isMegagroup: true, adminRights: true)

        let isAdministrator = try await api.isAdministrator(user, in: chat)
        XCTAssertTrue(isAdministrator)
        let sendCount = await transport.sendCount
        XCTAssertEqual(sendCount, 1)
    }

    func testBasicGroupParticipantResponseConsumesFullChatShape() async throws {
        let authKey = Data((0..<256).map { UInt8($0 & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 7, serverSalt: 11, date: Date())

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0xe5d7d19c)) // messages.chatFull
        response.writeInt32(Int32(bitPattern: 0x2633421b)) // chatFull
        response.writeInt32(1 << 18) // flags: available_reactions
        response.writeInt64(9001) // id
        try response.writeString("") // about
        response.writeInt32(Int32(bitPattern: 0x3cbc93f8)) // chatParticipants
        response.writeInt64(9001) // chat_id
        response.writeVector([Int32(42)]) { writer, _ in
            writer.writeInt32(Int32(bitPattern: 0x360d5d2)) // chatParticipantAdmin
            writer.writeInt32(0) // flags
            writer.writeInt64(42) // user_id
            writer.writeInt64(7) // inviter_id
            writer.writeInt32(1_700_000_000)
        }
        response.writeInt32(1) // participants version
        response.writeInt32(-1721619444) // peerNotifySettings
        response.writeInt32(0)
        response.writeInt32(Int32(bitPattern: 0x661d4037)) // chatReactionsSome
        response.writeVector([Int32(1)]) { writer, _ in
            writer.writeInt32(Int32(bitPattern: 0x523da4eb)) // reactionPaid
        }
        response.writeVector([Int32]()) { _, _ in } // chats
        response.writeVector([Int32]()) { _, _ in } // users

        let transport = RecordingMTProtoTransport(authKey: authKey, resultBodies: [response.data], sessionID: 95)
        let api = TelegramAPI(apiID: 123, apiHash: "fixture", session: session, transport: transport, sessionID: 95)
        let user = NativeUser(id: 42, accessHash: 123, firstName: "Admin", lastName: "", username: "", phone: nil, isSelf: false, isBot: false)
        let chat = NativeChat(id: 9001, accessHash: nil, title: "Fixture basic group", username: "", isChannel: false, isBroadcast: false, isMegagroup: false, adminRights: true)

        let isAdministrator = try await api.isAdministrator(user, in: chat)
        XCTAssertTrue(isAdministrator)
        let sendCount = await transport.sendCount
        XCTAssertEqual(sendCount, 1)
    }

    func testTelegramDifferenceFixtureConsumesMessagesAndOtherUpdates() async throws {
        var message = TLWriter()
        message.writeInt32(Int32(bitPattern: 0x3ae56482)) // message
        message.writeInt32(0) // flags
        message.writeInt32(0) // flags2
        message.writeInt32(77) // id
        message.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
        message.writeInt64(42)
        message.writeInt32(1_700_000_001)
        try message.writeString("difference fixture")

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x00f49ca0)) // updates.difference
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // new_messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // new_encrypted_messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(2) // other_updates
        response.writeInt32(Int32(bitPattern: 0x1f2b0afd)) // updateNewMessage
        response.append(message.data)
        response.writeInt32(901) // pts
        response.writeInt32(1) // pts_count
        response.writeInt32(Int32(bitPattern: 0x2f2f21bf)) // updateReadHistoryOutbox
        response.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
        response.writeInt64(42)
        response.writeInt32(77) // max_id
        response.writeInt32(902) // pts
        response.writeInt32(1) // pts_count
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // chats
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // users
        response.writeInt32(Int32(bitPattern: 0xa56c2a3e)) // state
        response.writeInt32(902)
        response.writeInt32(202)
        response.writeInt32(1_700_000_002)
        response.writeInt32(304)
        response.writeInt32(5)

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseUpdateDifferenceResponse(response.data)

        guard case .difference(let difference) = page else {
            XCTFail("Expected updates.difference")
            return
        }
        XCTAssertEqual(difference.messages.map(\.id), [77])
        XCTAssertEqual(difference.messages.first?.peerID, 42)
        XCTAssertEqual(difference.state.pts, 902)
        XCTAssertEqual(difference.state.date, 1_700_000_002)
    }

    func testTelegramDifferenceFixtureConsumesCommonLayer223Updates() async throws {
        func writePeerUser(_ writer: inout TLWriter, id: Int64) {
            writer.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
            writer.writeInt64(id)
        }

        var otherUpdates = TLWriter()

        otherUpdates.writeInt32(Int32(bitPattern: 0x2a17bf5c)) // updateUserTyping
        otherUpdates.writeInt32(1) // top_msg_id is present
        otherUpdates.writeInt64(7)
        otherUpdates.writeInt32(99)
        otherUpdates.writeInt32(Int32(bitPattern: 0xaa0cd9e4)) // upload document action
        otherUpdates.writeInt32(25)

        otherUpdates.writeInt32(Int32(bitPattern: 0x83487af0)) // updateChatUserTyping
        otherUpdates.writeInt64(8)
        writePeerUser(&otherUpdates, id: 9)
        otherUpdates.writeInt32(Int32(bitPattern: 0x25972bcb)) // emoji interaction action
        try otherUpdates.writeString("👍")
        otherUpdates.writeInt32(100)
        otherUpdates.writeInt32(Int32(bitPattern: 0x7d748d04)) // dataJSON
        try otherUpdates.writeString("{}")

        otherUpdates.writeInt32(Int32(bitPattern: 0x8c88c923)) // updateChannelUserTyping
        otherUpdates.writeInt32(1) // top_msg_id is present
        otherUpdates.writeInt64(10)
        otherUpdates.writeInt32(101)
        writePeerUser(&otherUpdates, id: 11)
        otherUpdates.writeInt32(Int32(bitPattern: 0x376d975c)) // text draft action
        otherUpdates.writeInt64(1234)
        try otherUpdates.writeString("draft")
        otherUpdates.writeVector([Int32]()) { _, _ in }

        otherUpdates.writeInt32(Int32(bitPattern: 0x8951abef)) // updateNewAuthorization
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt64(123)
        otherUpdates.writeInt32(1_700_000_010)
        try otherUpdates.writeString("fixture-device")
        try otherUpdates.writeString("Taipei")

        otherUpdates.writeInt32(Int32(bitPattern: 0x25f324f7)) // updateChannelReadMessagesContents
        otherUpdates.writeInt32(3) // top_msg_id and saved_peer_id
        otherUpdates.writeInt64(12)
        otherUpdates.writeInt32(102)
        writePeerUser(&otherUpdates, id: 13)
        otherUpdates.writeVector([Int32(103)]) { writer, value in writer.writeInt32(value) }

        otherUpdates.writeInt32(Int32(bitPattern: 0xebe07752)) // updatePeerBlocked
        otherUpdates.writeInt32(3) // blocked and blocked_my_stories_from
        otherUpdates.writeBool(true)
        otherUpdates.writeBool(false)
        writePeerUser(&otherUpdates, id: 14)

        otherUpdates.writeInt32(Int32(bitPattern: 0xbb9bb9a5)) // updatePeerHistoryTTL
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 15)
        otherUpdates.writeInt32(86_400)

        otherUpdates.writeInt32(Int32(bitPattern: 0x6a7e7366)) // updatePeerSettings
        writePeerUser(&otherUpdates, id: 16)
        otherUpdates.writeInt32(Int32(bitPattern: 0xf47741f7)) // peerSettings
        let settingsFlags: Int32 = (1 << 6) | (1 << 9) | (1 << 13) | (1 << 14) | (1 << 15) | (1 << 16) | (1 << 17) | (1 << 18)
        otherUpdates.writeInt32(settingsFlags)
        otherUpdates.writeInt32(42)
        try otherUpdates.writeString("join request")
        otherUpdates.writeInt32(1_700_000_011)
        otherUpdates.writeInt64(17)
        try otherUpdates.writeString("https://example.test/manage")
        otherUpdates.writeInt64(3)
        try otherUpdates.writeString("2026-07")
        try otherUpdates.writeString("TW")
        otherUpdates.writeInt32(1_700_000_012)
        otherUpdates.writeInt32(1_700_000_013)

        otherUpdates.writeInt32(Int32(bitPattern: 0xed85eab5)) // updatePinnedMessages
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 18)
        otherUpdates.writeVector([Int32(19)]) { writer, value in writer.writeInt32(value) }
        otherUpdates.writeInt32(110)
        otherUpdates.writeInt32(1)

        otherUpdates.writeInt32(Int32(bitPattern: 0x5bb98608)) // updatePinnedChannelMessages
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt64(20)
        otherUpdates.writeVector([Int32(21)]) { writer, value in writer.writeInt32(value) }
        otherUpdates.writeInt32(111)
        otherUpdates.writeInt32(1)

        otherUpdates.writeInt32(Int32(bitPattern: 0xb658f23e)) // updateDialogUnreadMark
        otherUpdates.writeInt32(2) // saved_peer_id is present
        otherUpdates.writeInt32(Int32(bitPattern: 0xe56dbf05)) // dialogPeer
        writePeerUser(&otherUpdates, id: 22)
        writePeerUser(&otherUpdates, id: 23)

        otherUpdates.writeInt32(Int32(bitPattern: 0x54c01850)) // updateChatDefaultBannedRights
        writePeerUser(&otherUpdates, id: 24)
        otherUpdates.writeInt32(Int32(bitPattern: 0x9f120418)) // chatBannedRights
        otherUpdates.writeInt32(7)
        otherUpdates.writeInt32(1_700_000_014)
        otherUpdates.writeInt32(112)

        otherUpdates.writeInt32(Int32(bitPattern: 0xf2a71983)) // updateDeleteScheduledMessages
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 25)
        otherUpdates.writeVector([Int32(26)]) { writer, value in writer.writeInt32(value) }
        otherUpdates.writeVector([Int32(27)]) { writer, value in writer.writeInt32(value) }

        otherUpdates.writeInt32(Int32(bitPattern: 0xd087663a)) // updateChatParticipant
        otherUpdates.writeInt32(7)
        otherUpdates.writeInt64(28)
        otherUpdates.writeInt32(1_700_000_015)
        otherUpdates.writeInt64(29)
        otherUpdates.writeInt64(30)
        otherUpdates.writeInt32(Int32(bitPattern: 0x38e79fde)) // chatParticipant
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt64(31)
        otherUpdates.writeInt64(32)
        otherUpdates.writeInt32(1_700_000_016)
        try otherUpdates.writeString("member")
        otherUpdates.writeInt32(Int32(bitPattern: 0xe1f867b8)) // chatParticipantCreator
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt64(33)
        try otherUpdates.writeString("creator")
        otherUpdates.writeInt32(Int32(bitPattern: 0xed107ab7)) // chatInvitePublicJoinRequests
        otherUpdates.writeInt32(113) // qts

        otherUpdates.writeInt32(Int32(bitPattern: 0x985d3abb)) // updateChannelParticipant
        otherUpdates.writeInt32(7)
        otherUpdates.writeInt64(34)
        otherUpdates.writeInt32(1_700_000_017)
        otherUpdates.writeInt64(35)
        otherUpdates.writeInt64(36)
        otherUpdates.writeInt32(Int32(bitPattern: 0x34c3bb53)) // channelParticipantAdmin
        otherUpdates.writeInt32(4) // rank is present
        otherUpdates.writeInt64(37)
        otherUpdates.writeInt64(38) // promoted_by
        otherUpdates.writeInt32(1_700_000_018)
        otherUpdates.writeInt32(Int32(bitPattern: 0x5fb224d5)) // chatAdminRights
        otherUpdates.writeInt32(3)
        try otherUpdates.writeString("admin")
        otherUpdates.writeInt32(Int32(bitPattern: 0xd5f0ad91)) // channelParticipantBanned
        otherUpdates.writeInt32(4) // rank is present
        writePeerUser(&otherUpdates, id: 39)
        otherUpdates.writeInt64(40)
        otherUpdates.writeInt32(1_700_000_019)
        otherUpdates.writeInt32(Int32(bitPattern: 0x9f120418)) // chatBannedRights
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt32(1_700_000_020)
        try otherUpdates.writeString("banned")
        otherUpdates.writeInt32(Int32(bitPattern: 0xed107ab7)) // chatInvitePublicJoinRequests
        otherUpdates.writeInt32(114) // qts

        otherUpdates.writeInt32(Int32(bitPattern: 0x4d712f2e)) // updateBotCommands
        writePeerUser(&otherUpdates, id: 41)
        otherUpdates.writeInt64(42)
        otherUpdates.writeVector([Int32(0)]) { writer, _ in
            writer.writeInt32(Int32(bitPattern: 0xc27ac8c7)) // botCommand
            try? writer.writeString("start")
            try? writer.writeString("Start the bot")
        }

        otherUpdates.writeInt32(Int32(bitPattern: 0x7063c3db)) // updatePendingJoinRequests
        writePeerUser(&otherUpdates, id: 43)
        otherUpdates.writeInt32(1)
        otherUpdates.writeVector([Int64(44)]) { writer, value in writer.writeInt64(value) }

        otherUpdates.writeInt32(Int32(bitPattern: 0x84cd5a)) // updateTranscribedAudio
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 45)
        otherUpdates.writeInt32(46)
        otherUpdates.writeInt64(47)
        try otherUpdates.writeString("transcribed")

        otherUpdates.writeInt32(Int32(bitPattern: 0xf74e932b)) // updateReadStories
        writePeerUser(&otherUpdates, id: 48)
        otherUpdates.writeInt32(49)

        otherUpdates.writeInt32(Int32(bitPattern: 0x1bf335b9)) // updateStoryID
        otherUpdates.writeInt32(50)
        otherUpdates.writeInt64(51)

        otherUpdates.writeInt32(Int32(bitPattern: 0x86fccf85)) // updateMoveStickerSetToTop
        otherUpdates.writeInt32(3)
        otherUpdates.writeInt64(52)

        otherUpdates.writeInt32(Int32(bitPattern: 0x9375341e)) // updateSavedGifs

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x00f49ca0)) // updates.difference
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // new_messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // new_encrypted_messages
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(22) // other_updates
        response.append(otherUpdates.data)
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // chats
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(0) // users
        response.writeInt32(Int32(bitPattern: 0xa56c2a3e)) // state
        response.writeInt32(1_200)
        response.writeInt32(2_200)
        response.writeInt32(1_700_000_021)
        response.writeInt32(3_200)
        response.writeInt32(6)

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseUpdateDifferenceResponse(response.data)

        guard case .difference(let difference) = page else {
            XCTFail("Expected updates.difference")
            return
        }
        XCTAssertTrue(difference.messages.isEmpty)
        XCTAssertEqual(difference.state.pts, 1_200)
        XCTAssertEqual(difference.state.qts, 2_200)
        XCTAssertEqual(difference.state.seq, 3_200)
    }

    func testTelegramDifferenceFixtureConsumesEncryptedReactionAndForumUpdates() async throws {
        func writePeerUser(_ writer: inout TLWriter, id: Int64) {
            writer.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
            writer.writeInt64(id)
        }

        func writeInputGroupCall(_ writer: inout TLWriter, id: Int64) {
            writer.writeInt32(Int32(bitPattern: 0xd8aa840f)) // inputGroupCall
            writer.writeInt64(id)
            writer.writeInt64(id + 1)
        }

        func writeReactionEmoji(_ writer: inout TLWriter, _ value: String = "👍") {
            writer.writeInt32(455247544) // reactionEmoji
            try? writer.writeString(value)
        }

        func writeReactionCount(_ writer: inout TLWriter, count: Int32) {
            writer.writeInt32(Int32(bitPattern: 0xa3d1cb80)) // reactionCount
            writer.writeInt32(0)
            writeReactionEmoji(&writer)
            writer.writeInt32(count)
        }

        func writeMessageReactions(_ writer: inout TLWriter) {
            writer.writeInt32(Int32(bitPattern: 0x0a339f0b)) // messageReactions
            writer.writeInt32(0)
            writer.writeVector([Int32(1)]) { writer, count in
                writeReactionCount(&writer, count: count)
            }
        }

        var otherUpdates = TLWriter()

        otherUpdates.writeInt32(Int32(bitPattern: 0x12bcbd9a)) // updateNewEncryptedMessage
        otherUpdates.writeInt32(Int32(bitPattern: 0x23734b06)) // encryptedMessageService
        otherUpdates.writeInt64(1)
        otherUpdates.writeInt32(2)
        otherUpdates.writeInt32(1_700_000_030)
        try otherUpdates.writeBytes(Data([1, 2, 3]))
        otherUpdates.writeInt32(4) // qts

        otherUpdates.writeInt32(Int32(bitPattern: 0x1710f156)) // updateEncryptedChatTyping
        otherUpdates.writeInt32(5)

        otherUpdates.writeInt32(Int32(bitPattern: 0x38fe25b7)) // updateEncryptedMessagesRead
        otherUpdates.writeInt32(6)
        otherUpdates.writeInt32(7)
        otherUpdates.writeInt32(8)

        otherUpdates.writeInt32(Int32(bitPattern: 0x46560264)) // updateLangPackTooLong
        try otherUpdates.writeString("zh-hant")

        otherUpdates.writeInt32(Int32(bitPattern: 0x24f40e77)) // updateMessagePollVote
        otherUpdates.writeInt64(9)
        writePeerUser(&otherUpdates, id: 10)
        otherUpdates.writeVector([Data([11, 12])]) { writer, value in
            try? writer.writeBytes(value)
        }
        otherUpdates.writeInt32(13)

        otherUpdates.writeInt32(Int32(bitPattern: 0x0b783982)) // updateGroupCallConnection
        otherUpdates.writeInt32(0)
        otherUpdates.writeInt32(Int32(bitPattern: 0x7d748d04)) // dataJSON
        try otherUpdates.writeString("{}")

        otherUpdates.writeInt32(Int32(bitPattern: 0xa477288f)) // updateGroupCallChainBlocks
        writeInputGroupCall(&otherUpdates, id: 14)
        otherUpdates.writeInt32(15)
        otherUpdates.writeVector([Data([16, 17])]) { writer, value in
            try? writer.writeBytes(value)
        }
        otherUpdates.writeInt32(18)

        otherUpdates.writeInt32(Int32(bitPattern: 0xc957a766)) // updateGroupCallEncryptedMessage
        writeInputGroupCall(&otherUpdates, id: 19)
        writePeerUser(&otherUpdates, id: 20)
        try otherUpdates.writeBytes(Data([21, 22]))

        otherUpdates.writeInt32(Int32(bitPattern: 0x1e297bfa)) // updateMessageReactions
        otherUpdates.writeInt32(0)
        writePeerUser(&otherUpdates, id: 23)
        otherUpdates.writeInt32(24)
        writeMessageReactions(&otherUpdates)

        otherUpdates.writeInt32(Int32(bitPattern: 0x7d627683)) // updateSentStoryReaction
        writePeerUser(&otherUpdates, id: 25)
        otherUpdates.writeInt32(26)
        writeReactionEmoji(&otherUpdates)

        otherUpdates.writeInt32(Int32(bitPattern: 0x1824e40b)) // updateNewStoryReaction
        otherUpdates.writeInt32(27)
        writePeerUser(&otherUpdates, id: 28)
        writeReactionEmoji(&otherUpdates, "❤️")

        otherUpdates.writeInt32(Int32(bitPattern: 0xac21d3ce)) // updateBotMessageReaction
        writePeerUser(&otherUpdates, id: 29)
        otherUpdates.writeInt32(30)
        otherUpdates.writeInt32(1_700_000_031)
        writePeerUser(&otherUpdates, id: 31)
        otherUpdates.writeVector([Int32(0)]) { writer, _ in writeReactionEmoji(&writer) }
        otherUpdates.writeVector([Int32(0)]) { writer, _ in writeReactionEmoji(&writer, "🔥") }
        otherUpdates.writeInt32(32)

        otherUpdates.writeInt32(Int32(bitPattern: 0x09cb7759)) // updateBotMessageReactions
        writePeerUser(&otherUpdates, id: 33)
        otherUpdates.writeInt32(34)
        otherUpdates.writeInt32(1_700_000_032)
        otherUpdates.writeVector([Int32(2)]) { writer, count in writeReactionCount(&writer, count: count) }
        otherUpdates.writeInt32(35)

        otherUpdates.writeInt32(Int32(bitPattern: 0x4e80a379)) // updateStarsBalance
        otherUpdates.writeInt32(-1_145_654_109) // starsAmount
        otherUpdates.writeInt64(36)
        otherUpdates.writeInt32(37)

        otherUpdates.writeInt32(Int32(bitPattern: 0x77b0e372)) // updateReadMonoForumInbox
        otherUpdates.writeInt64(38)
        writePeerUser(&otherUpdates, id: 39)
        otherUpdates.writeInt32(40)

        otherUpdates.writeInt32(Int32(bitPattern: 0xa4a79376)) // updateReadMonoForumOutbox
        otherUpdates.writeInt64(41)
        writePeerUser(&otherUpdates, id: 42)
        otherUpdates.writeInt32(43)

        otherUpdates.writeInt32(Int32(bitPattern: 0x9f812b08)) // updateMonoForumNoPaidException
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt64(44)
        writePeerUser(&otherUpdates, id: 45)

        otherUpdates.writeInt32(Int32(bitPattern: 0x3e85e92c)) // updateDeleteGroupCallMessages
        writeInputGroupCall(&otherUpdates, id: 46)
        otherUpdates.writeVector([Int32(47)]) { writer, value in writer.writeInt32(value) }

        otherUpdates.writeInt32(Int32(bitPattern: 0x683b2c52)) // updatePinnedForumTopic
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 48)
        otherUpdates.writeInt32(49)

        otherUpdates.writeInt32(Int32(bitPattern: 0xdef143d0)) // updatePinnedForumTopics
        otherUpdates.writeInt32(1)
        writePeerUser(&otherUpdates, id: 50)
        otherUpdates.writeVector([Int32(51)]) { writer, value in writer.writeInt32(value) }

        otherUpdates.writeInt32(Int32(bitPattern: 0xbd8367b9)) // updateChatParticipantRank
        otherUpdates.writeInt64(52)
        otherUpdates.writeInt64(53)
        try otherUpdates.writeString("helper")
        otherUpdates.writeInt32(54)

        otherUpdates.writeInt32(Int32(bitPattern: 0x31c24808)) // updateStickerSets
        otherUpdates.writeInt32(3)

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x00f49ca0)) // updates.difference
        for _ in 0..<2 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(22)
        response.append(otherUpdates.data)
        for _ in 0..<2 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }
        response.writeInt32(Int32(bitPattern: 0xa56c2a3e)) // state
        response.writeInt32(1_400)
        response.writeInt32(2_400)
        response.writeInt32(1_700_000_033)
        response.writeInt32(3_400)
        response.writeInt32(8)

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseUpdateDifferenceResponse(response.data)

        guard case .difference(let difference) = page else {
            XCTFail("Expected updates.difference")
            return
        }
        XCTAssertTrue(difference.messages.isEmpty)
        XCTAssertEqual(difference.state, NativeUpdateState(pts: 1_400, qts: 2_400, date: 1_700_000_033, seq: 3_400, unreadCount: 8))
    }

    func testTelegramDifferenceFixtureConsumesConfigDraftPollAndDialogUpdates() async throws {
        func writePeerUser(_ writer: inout TLWriter, id: Int64) {
            writer.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
            writer.writeInt64(id)
        }

        func writeDialogPeerFolder(_ writer: inout TLWriter, id: Int32) {
            writer.writeInt32(Int32(bitPattern: 0x514519e2)) // dialogPeerFolder
            writer.writeInt32(id)
        }

        var otherUpdates = TLWriter()

        otherUpdates.writeInt32(Int32(bitPattern: 0x8e5e9873)) // updateDcOptions
        otherUpdates.writeVector([Int32(0)]) { writer, _ in
            writer.writeInt32(Int32(bitPattern: 0x18b7a10d)) // dcOption
            writer.writeInt32(0)
            writer.writeInt32(2)
            try? writer.writeString("149.154.167.51")
            writer.writeInt32(443)
        }

        otherUpdates.writeInt32(Int32(bitPattern: 0xee3b272a)) // updatePrivacy
        otherUpdates.writeInt32(Int32(bitPattern: 0xbc2eab30)) // privacyKeyStatusTimestamp
        otherUpdates.writeVector([Int32(0)]) { writer, _ in
            writer.writeInt32(Int32(bitPattern: 0x65427b82)) // privacyValueAllowAll
        }

        otherUpdates.writeInt32(Int32(bitPattern: 0x6e6fe51c)) // updateDialogPinned
        otherUpdates.writeInt32(2) // folder_id is present
        otherUpdates.writeInt32(4)
        writeDialogPeerFolder(&otherUpdates, id: 4)

        otherUpdates.writeInt32(Int32(bitPattern: 0xfa0f3ca2)) // updatePinnedDialogs
        otherUpdates.writeInt32(3) // folder_id and order are present
        otherUpdates.writeInt32(5)
        otherUpdates.writeVector([Int32(0)]) { writer, _ in
            writeDialogPeerFolder(&writer, id: 5)
        }

        otherUpdates.writeInt32(Int32(bitPattern: 0xedfc111e)) // updateDraftMessage
        otherUpdates.writeInt32(3) // top_msg_id and saved_peer_id are present
        writePeerUser(&otherUpdates, id: 6)
        otherUpdates.writeInt32(7)
        writePeerUser(&otherUpdates, id: 8)
        otherUpdates.writeInt32(Int32(bitPattern: 0x1b0c841a)) // draftMessageEmpty
        otherUpdates.writeInt32(0)

        otherUpdates.writeInt32(Int32(bitPattern: 0xaca1657b)) // updateMessagePoll
        otherUpdates.writeInt32(1) // poll is present
        otherUpdates.writeInt64(9)
        otherUpdates.writeInt32(Int32(bitPattern: 0x58747131)) // poll
        otherUpdates.writeInt64(10)
        otherUpdates.writeInt32(0)
        try otherUpdates.writeString("Question")
        otherUpdates.writeVector([Int32]()) { _, _ in }
        otherUpdates.writeVector([Int32]()) { _, _ in }
        otherUpdates.writeInt32(Int32(bitPattern: 0x7adf2420)) // pollResults
        otherUpdates.writeInt32(0)

        otherUpdates.writeInt32(Int32(bitPattern: 0x26ffde7d)) // updateDialogFilter
        otherUpdates.writeInt32(1)
        otherUpdates.writeInt32(11)
        otherUpdates.writeInt32(Int32(bitPattern: 0x363293ae)) // dialogFilterDefault

        otherUpdates.writeInt32(Int32(bitPattern: 0xaeaf9e74)) // updateSavedDialogPinned
        otherUpdates.writeInt32(0)
        writeDialogPeerFolder(&otherUpdates, id: 6)

        otherUpdates.writeInt32(Int32(bitPattern: 0x686c85a6)) // updatePinnedSavedDialogs
        otherUpdates.writeInt32(1)
        otherUpdates.writeVector([Int32(0)]) { writer, _ in
            writeDialogPeerFolder(&writer, id: 7)
        }

        var response = TLWriter()
        response.writeInt32(Int32(bitPattern: 0x00f49ca0)) // updates.difference
        for _ in 0..<2 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }
        response.writeInt32(TLConstructor.vector)
        response.writeInt32(9)
        response.append(otherUpdates.data)
        for _ in 0..<2 {
            response.writeInt32(TLConstructor.vector)
            response.writeInt32(0)
        }
        response.writeInt32(Int32(bitPattern: 0xa56c2a3e)) // state
        response.writeInt32(1_300)
        response.writeInt32(2_300)
        response.writeInt32(1_700_000_022)
        response.writeInt32(3_300)
        response.writeInt32(7)

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let page = try await api.parseUpdateDifferenceResponse(response.data)

        guard case .difference(let difference) = page else {
            XCTFail("Expected updates.difference")
            return
        }
        XCTAssertEqual(difference.state, NativeUpdateState(pts: 1_300, qts: 2_300, date: 1_700_000_022, seq: 3_300, unreadCount: 7))
    }

    func testTelegramDifferenceFixturesPreserveSliceAndTooLongBoundaries() async throws {
        let api = TelegramAPI(apiID: 123, apiHash: "fixture")

        var slice = TLWriter()
        slice.writeInt32(Int32(bitPattern: 0xa8fb1981)) // updates.differenceSlice
        for _ in 0..<5 {
            slice.writeInt32(TLConstructor.vector)
            slice.writeInt32(0)
        }
        slice.writeInt32(Int32(bitPattern: 0xa56c2a3e))
        slice.writeInt32(6)
        slice.writeInt32(7)
        slice.writeInt32(8)
        slice.writeInt32(9)
        slice.writeInt32(10)

        let slicePage = try await api.parseUpdateDifferenceResponse(slice.data)
        guard case .slice(let sliceDifference) = slicePage else {
            XCTFail("Expected updates.differenceSlice")
            return
        }
        XCTAssertEqual(sliceDifference.state, NativeUpdateState(pts: 6, qts: 7, date: 8, seq: 9, unreadCount: 10))

        var tooLong = TLWriter()
        tooLong.writeInt32(Int32(bitPattern: 0x4afe8f6d)) // updates.differenceTooLong
        tooLong.writeInt32(999)
        let tooLongPage = try await api.parseUpdateDifferenceResponse(tooLong.data)
        guard case .tooLong(let serverPTS) = tooLongPage else {
            XCTFail("Expected updates.differenceTooLong")
            return
        }
        XCTAssertEqual(serverPTS, 999)
    }

    func testTelegramCDNFixturesDecodeRedirectAndIntegrityMetadata() async throws {
        let encrypted = Data(hex: "00112233445566778899aabbccddeeff")
        var redirect = TLWriter()
        redirect.writeInt32(Int32(bitPattern: 0xf18cda44))
        redirect.writeInt32(7)
        try redirect.writeBytes(Data([1, 2, 3]))
        try redirect.writeBytes(Data(repeating: 0x11, count: 32))
        try redirect.writeBytes(Data(repeating: 0x22, count: 16))
        redirect.writeVector([TelegramFileHash(offset: 0, limit: Int32(encrypted.count), hash: MTProtoCrypto.sha256(encrypted))]) { writer, fileHash in
            writer.writeInt32(Int32(bitPattern: 0xf39b035c))
            writer.writeInt64(fileHash.offset)
            writer.writeInt32(fileHash.limit)
            try? writer.writeBytes(fileHash.hash)
        }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let response = try await api.parseUploadFileResponse(redirect.data)
        guard case .cdnRedirect(let parsed) = response else {
            XCTFail("Expected upload.fileCdnRedirect")
            return
        }
        XCTAssertEqual(parsed.dcID, 7)
        XCTAssertEqual(parsed.fileToken, Data([1, 2, 3]))
        XCTAssertEqual(parsed.fileHashes.first?.hash, MTProtoCrypto.sha256(encrypted))
    }

    func testTelegramCDNFixturesDecodeDataAndReuploadResponses() async throws {
        let api = TelegramAPI(apiID: 123, apiHash: "fixture")

        var dataResponse = TLWriter()
        dataResponse.writeInt32(Int32(bitPattern: 0xa99fca4f))
        try dataResponse.writeBytes(Data([9, 8, 7]))
        guard case .bytes(let bytes) = try await api.parseCDNFileResponse(dataResponse.data) else {
            XCTFail("Expected upload.cdnFile")
            return
        }
        XCTAssertEqual(bytes, Data([9, 8, 7]))

        var reuploadResponse = TLWriter()
        reuploadResponse.writeInt32(Int32(bitPattern: 0xeea8e46))
        try reuploadResponse.writeBytes(Data([4, 5, 6]))
        guard case .reuploadNeeded(let requestToken) = try await api.parseCDNFileResponse(reuploadResponse.data) else {
            XCTFail("Expected upload.cdnFileReuploadNeeded")
            return
        }
        XCTAssertEqual(requestToken, Data([4, 5, 6]))
    }

    func testTelegramCDNConfigFixturesDecodeOnlyCDNOptions() async throws {
        var config = TLWriter()
        config.writeInt32(Int32(bitPattern: 0xcc1a241e))
        config.writeInt32(0)
        config.writeInt32(1_700_000_000)
        config.writeInt32(1_700_000_900)
        config.writeBool(false)
        config.writeInt32(2)
        config.writeVector([true, false]) { writer, isCDN in
            writer.writeInt32(Int32(bitPattern: 0x18b7a10d))
            writer.writeInt32(isCDN ? 8 : 0)
            writer.writeInt32(isCDN ? 7 : 2)
            try? writer.writeString(isCDN ? "cdn.example.test" : "149.154.167.51")
            writer.writeInt32(443)
        }
        try config.writeString("")
        for _ in 0..<17 { config.writeInt32(0) }
        for _ in 0..<4 { config.writeInt32(0) }
        try config.writeString("https://t.me/")
        for _ in 0..<3 { config.writeInt32(0) }

        let api = TelegramAPI(apiID: 123, apiHash: "fixture")
        let endpoints = try await api.parseConfigCDNOptions(config.data)
        XCTAssertEqual(endpoints, [TelegramCDNEndpoint(dcID: 7, host: "cdn.example.test", port: 443, isIPv6: false, isCDN: true)])
    }

    func testStoreConfigurationMutationAndActionRecordingAreSerialized() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldStore-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = TeleShieldStore(root: root)
        let account = try await store.createAccount(id: "account-transaction")
        try await store.updateConfiguration(accountID: account.id) { configuration in
            configuration.whitelist["42"] = ListEntry(userID: "42", username: "trusted", added: "now", reason: "fixture")
        }
        try await store.recordAction(
            BlockRecord(time: "2026-01-01T00:00:00Z", source: "private", userID: "7", name: "spam", reason: "fixture"),
            accountID: account.id
        )

        let configuration = try await store.configuration(accountID: account.id)
        let records = try await store.blockRecords(accountID: account.id)
        XCTAssertEqual(configuration.whitelist["42"]?.username, "trusted")
        XCTAssertEqual(configuration.blockedCount, 1)
        XCTAssertEqual(records.count, 1)
    }

    func testMigrationErrorExtractsValidatedDataCenter() {
        XCTAssertEqual(TelegramMTProtoError.rpc(code: 303, message: "PHONE_MIGRATE_4").migratedDataCenter, 4)
        XCTAssertNil(TelegramMTProtoError.rpc(code: 400, message: "BAD_REQUEST").migratedDataCenter)
        XCTAssertNil(TelegramMTProtoError.rpc(code: 303, message: "PHONE_MIGRATE_9").migratedDataCenter)
    }

    func testAsyncSemaphoreCancellationDoesNotConsumePermit() async throws {
        let semaphore = AsyncSemaphore()
        try await semaphore.acquire()

        let waiter = Task<Bool, Never> {
            do {
                try await semaphore.acquire()
                await semaphore.release()
                return false
            } catch is CancellationError {
                return true
            } catch {
                return false
            }
        }
        await Task.yield()
        waiter.cancel()

        let wasCancelled = await waiter.value
        XCTAssertTrue(wasCancelled)
        await semaphore.release()
        try await semaphore.acquire()
        await semaphore.release()
    }

    func testStoreKeepsAccountStateIsolated() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldStore-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = TeleShieldStore(root: root)
        let first = try await store.createAccount(id: "account-a")
        let second = try await store.createAccount(id: "account-b")
        var firstConfig = try await store.configuration(accountID: first.id)
        firstConfig.whitelist["1"] = ListEntry(userID: "1", username: "trusted", added: "now", reason: "test")
        try await store.saveConfiguration(firstConfig, accountID: first.id)

        let secondConfig = try await store.configuration(accountID: second.id)
        XCTAssertTrue(secondConfig.whitelist.isEmpty)
        let accountIDs = try await store.listAccounts().map(\.id)
        XCTAssertEqual(accountIDs, ["account-a", "account-b"])
    }

    func testStorePersistsAndClearsTelegramUpdateState() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldStore-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = TeleShieldStore(root: root)
        let account = try await store.createAccount(id: "account-updates")
        let state = NativeUpdateState(pts: 11, qts: 12, date: 13, seq: 14, unreadCount: 15)
        try await store.saveUpdateState(state, accountID: account.id)
        let loadedState = try await store.loadUpdateState(accountID: account.id)
        XCTAssertEqual(loadedState, state)

        try await store.clearUpdateState(accountID: account.id)
        let clearedState = try await store.loadUpdateState(accountID: account.id)
        XCTAssertNil(clearedState)

        let firstChannelState = NativeChannelUpdateState(channelID: 100, pts: 10)
        let secondChannelState = NativeChannelUpdateState(channelID: 200, pts: 20)
        try await store.saveChannelUpdateState(firstChannelState, accountID: account.id)
        try await store.saveChannelUpdateState(secondChannelState, accountID: account.id)
        try await store.saveChannelUpdateState(NativeChannelUpdateState(channelID: 100, pts: 11), accountID: account.id)
        let channelStates = try await store.loadChannelUpdateStates(accountID: account.id)
        XCTAssertEqual(channelStates[100]?.pts, 11)
        XCTAssertEqual(channelStates[200], secondChannelState)

        try await store.clearChannelUpdateState(channelID: 100, accountID: account.id)
        let clearedChannelStates = try await store.loadChannelUpdateStates(accountID: account.id)
        XCTAssertNil(clearedChannelStates[100])
        XCTAssertEqual(clearedChannelStates[200], secondChannelState)
    }

    func testStoreRejectsInvalidSessionKey() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldStore-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = TeleShieldStore(root: root)
        let account = try await store.createAccount(id: "account-session")
        let session = NativeSession(dcID: 2, authKey: Data([0x01]), userID: 1, date: Date())
        do {
            try await store.saveSession(session, accountID: account.id)
            XCTFail("Expected invalid session key to be rejected")
        } catch let error as NativeStoreError {
            guard case .invalidSession = error else {
                XCTFail("Unexpected store error: \(error)")
                return
            }
        }
    }

    func testLegacyMigrationRetiresPlaintextRootArtifacts() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldStore-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)

        let legacyConfig = """
        {"api_id":123,"api_hash":"legacy-secret","phone":"+886900000000","user_id":99,"username":"legacy","display_name":"Legacy"}
        """
        try Data(legacyConfig.utf8).write(to: root.appendingPathComponent("config.json"))
        try Data("legacy session".utf8).write(to: root.appendingPathComponent("user.session"))
        try Data("legacy blocks".utf8).write(to: root.appendingPathComponent("block_log.json"))
        try Data("legacy patterns".utf8).write(to: root.appendingPathComponent("learned_patterns.json"))

        let store = TeleShieldStore(root: root)
        let accounts = try await store.listAccounts()
        XCTAssertEqual(accounts.map(\.id), ["legacy-account"])
        let configuration = try await store.configuration(accountID: "legacy-account")
        XCTAssertEqual(configuration.apiID, 123)
        XCTAssertEqual(configuration.apiHash, "legacy-secret")
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("config.json").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("user.session").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("block_log.json").path))
        XCTAssertFalse(FileManager.default.fileExists(atPath: root.appendingPathComponent("learned_patterns.json").path))
    }

    func testProtectionScanEvaluatesMessageAndExecutesPrivateBlock() async throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("TeleShieldProtection-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: root) }

        let store = TeleShieldStore(root: root)
        let account = try await store.createAccount(id: "account-protection")
        let authKey = Data((0..<256).map { UInt8((255 - $0) & 0xff) })
        let session = NativeSession(dcID: 2, authKey: authKey, userID: 99, serverSalt: 11, date: Date())
        let transport = RecordingMTProtoTransport(
            authKey: authKey,
            resultBodies: [
                try makePrivateDialogsFixture(),
                try makeEmptyContactsFixture(),
                try makePrivateHistoryFixture(),
                Data()
            ],
            sessionID: 92
        )
        let api = TelegramAPI(
            apiID: 123,
            apiHash: "fixture-hash",
            session: session,
            transport: transport,
            sessionID: 92
        )
        let configuration = try await store.configuration(accountID: account.id)
        let coordinator = ProtectionCoordinator(accountID: account.id, api: api, store: store, configuration: configuration)

        let result = try await coordinator.scan(scope: "private", dryRun: false, settings: ScanSettings.defaults) { _ in }
        XCTAssertEqual(result.matched, 1)
        XCTAssertEqual(result.acted, 1)
        XCTAssertEqual(result.findings.first?.userID, "7")
        XCTAssertEqual(result.findings.first?.reason, "命中內建廣告規則")
        XCTAssertTrue(result.errors.isEmpty)

        let records = try await store.blockRecords(accountID: account.id)
        XCTAssertEqual(records.count, 1)
        XCTAssertEqual(records.first?.userID, "7")
        XCTAssertEqual(records.first?.source, "private")

        let sendCount = await transport.sendCount
        XCTAssertEqual(sendCount, 4)
    }

    func testSpamRulesRespectListsAndNormalizeText() {
        let user = NativeUser(id: 42, accessHash: nil, firstName: "", lastName: "", username: "", phone: nil, isSelf: false, isBot: false)
        let message = NativeMessage(id: 1, peerID: 10, senderID: user.id, date: Date(), text: "投資高收益，立即點擊", hasPhoto: false)

        var configuration = StoredConfiguration.empty
        let rules = SpamRuleEngine(configuration: configuration)
        XCTAssertEqual(rules.evaluate(user: user, message: message), "命中內建廣告規則")

        configuration.whitelist[String(user.id)] = ListEntry(userID: String(user.id), username: "", added: "", reason: "test")
        XCTAssertNil(SpamRuleEngine(configuration: configuration).evaluate(user: user, message: message))

        configuration.whitelist.removeValue(forKey: String(user.id))
        configuration.blacklist[String(user.id)] = ListEntry(userID: String(user.id), username: "", added: "", reason: "test")
        XCTAssertEqual(SpamRuleEngine(configuration: configuration).evaluate(user: user, message: message), "黑名單使用者")
    }
}

private func unwrapInitConnectionQuery(_ data: Data) throws -> Data {
    var reader = TLReader(data)
    guard try reader.readInt32() == TLConstructor.invokeWithLayer else {
        throw TLCodecError.invalidConstructor(0)
    }
    _ = try reader.readInt32() // layer
    guard try reader.readInt32() == TLConstructor.initConnection else {
        throw TLCodecError.invalidConstructor(0)
    }
    _ = try reader.readInt32() // flags
    _ = try reader.readInt32() // api_id
    for _ in 0..<6 { _ = try reader.readString() }
    guard try reader.readInt32() == TLConstructor.invokeWithoutUpdates else {
        throw TLCodecError.invalidConstructor(0)
    }
    return try reader.readFixed(count: reader.remaining)
}

private func makeSentCodeFixture() throws -> Data {
    var writer = TLWriter()
    writer.writeInt32(1577067778) // auth.sentCode
    writer.writeInt32(0)
    writer.writeInt32(Int32(bitPattern: 0xc000bba2)) // auth.sentCodeTypeSms
    writer.writeInt32(60)
    try writer.writeString("fixture-code-hash")
    return writer.data
}

private func makeAuthorizationFixture() throws -> Data {
    var writer = TLWriter()
    writer.writeInt32(782418132) // auth.authorization
    writer.writeInt32(0)
    try writeFixtureUser(&writer, id: 99, accessHash: 999, firstName: "Signed")
    return writer.data
}

private func writeFixtureUser(_ writer: inout TLWriter, id: Int64, accessHash: Int64, firstName: String) throws {
    writer.writeInt32(829899656) // user
    writer.writeInt32(3) // access_hash + first_name
    writer.writeInt32(0) // flags2
    writer.writeInt64(id)
    writer.writeInt64(accessHash)
    try writer.writeString(firstName)
}

private func writeFixtureUserPeer(_ writer: inout TLWriter, id: Int64) {
    writer.writeInt32(Int32(bitPattern: 0x59511722)) // peerUser
    writer.writeInt64(id)
}

private func makePrivateDialogsFixture() throws -> Data {
    var writer = TLWriter()
    writer.writeInt32(364538944) // messages.dialogs
    writer.writeVector([0]) { writer, _ in
        writer.writeInt32(Int32(bitPattern: 0xd58a08c6)) // dialog
        writer.writeInt32(0)
        writeFixtureUserPeer(&writer, id: 7)
        for _ in 0..<6 { writer.writeInt32(0) }
        writer.writeInt32(-1721619444) // peerNotifySettings
        writer.writeInt32(0)
    }
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // messages
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // chats
    writer.writeVector([0]) { writer, _ in
        try? writeFixtureUser(&writer, id: 7, accessHash: 123, firstName: "Spammer")
    }
    return writer.data
}

private func makeEmptyContactsFixture() throws -> Data {
    var writer = TLWriter()
    writer.writeInt32(Int32(bitPattern: 0xeae87e42)) // contacts.contacts
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // contacts
    writer.writeInt32(0) // saved_count
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // users
    return writer.data
}

private func makePrivateHistoryFixture() throws -> Data {
    var writer = TLWriter()
    writer.writeInt32(Int32(bitPattern: 0x1d73e7ea)) // messages.messages
    writer.writeVector([0]) { writer, _ in
        writer.writeInt32(Int32(bitPattern: 0x3ae56482)) // message
        writer.writeInt32(256) // sender_id present
        writer.writeInt32(0) // flags2
        writer.writeInt32(1) // id
        writeFixtureUserPeer(&writer, id: 7) // sender_id
        writeFixtureUserPeer(&writer, id: 7) // peer_id
        writer.writeInt32(Int32(Date().timeIntervalSince1970))
        try? writer.writeString("投資高收益")
    }
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // forum topics
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // chats
    writer.writeInt32(TLConstructor.vector)
    writer.writeInt32(0) // users
    return writer.data
}

private extension Data {
    init(hex: String) {
        let values = stride(from: 0, to: hex.count, by: 2).compactMap { index -> UInt8? in
            let start = hex.index(hex.startIndex, offsetBy: index)
            let end = hex.index(start, offsetBy: 2)
            return UInt8(hex[start..<end], radix: 16)
        }
        self.init(values)
    }
}

private actor RecordingMTProtoTransport: MTProtoTransporting {
    private let authKey: Data
    private let resultBodies: [Data]
    private let sessionID: Int64
    private var ready = false
    private var resultIndex = 0
    private var pendingResponse: Data?
    private(set) var requestBodies: [Data] = []

    init(authKey: Data, resultBodies: [Data], sessionID: Int64) {
        self.authKey = authKey
        self.resultBodies = resultBodies
        self.sessionID = sessionID
    }

    var requestBodiesSnapshot: [Data] { requestBodies }
    var sendCount: Int { requestBodies.count }

    func isReady() -> Bool { ready }

    func connect() {
        ready = true
    }

    func close() {
        ready = false
        pendingResponse = nil
    }

    func send(_ packet: Data) throws {
        guard ready else { throw MTProtoTransportError.notConnected }
        guard resultIndex < resultBodies.count else { throw MTProtoTransportError.connectionClosed }
        let request = try MTProtoCrypto.decryptMessage(
            authKey: authKey,
            sessionID: sessionID,
            packet: packet,
            direction: .clientToServer
        )
        requestBodies.append(request.body)
        let result = resultBodies[resultIndex]
        resultIndex += 1

        var responseBody = TLWriter()
        responseBody.writeInt32(-212046591) // rpc_result
        responseBody.writeInt64(request.messageID)
        responseBody.append(result)
        let encrypted = try MTProtoCrypto.encryptedMessage(
            authKey: authKey,
            salt: request.salt,
            sessionID: sessionID,
            messageID: request.messageID + 1,
            sequence: 0,
            body: responseBody.data,
            direction: .serverToClient
        )
        pendingResponse = encrypted.packet
    }

    func receive() throws -> Data {
        guard ready, let pendingResponse else { throw MTProtoTransportError.connectionClosed }
        self.pendingResponse = nil
        return pendingResponse
    }
}

private actor ScriptedMTProtoTransport: MTProtoTransporting {
    enum FirstResponse: Sendable {
        case result
        case badServerSalt(Int64)
    }

    private let authKey: Data
    private let result: Data
    private let firstResponse: FirstResponse
    private let sessionID: Int64
    private var ready = false
    private var pendingResponse: Data?
    private var responseCount = 0
    private(set) var sendCount = 0
    private(set) var receiveCount = 0

    init(authKey: Data, result: Data, firstResponse: FirstResponse = .result, sessionID: Int64 = 0) {
        self.authKey = authKey
        self.result = result
        self.firstResponse = firstResponse
        self.sessionID = sessionID
    }

    func isReady() -> Bool { ready }

    func connect() {
        ready = true
    }

    func close() {
        ready = false
        pendingResponse = nil
    }

    func send(_ packet: Data) throws {
        guard ready else { throw MTProtoTransportError.notConnected }
        let request = try MTProtoCrypto.decryptMessage(
            authKey: authKey,
            sessionID: sessionID,
            packet: packet,
            direction: .clientToServer
        )
        sendCount += 1
        let body: Data
        if responseCount == 0, case .badServerSalt(let newSalt) = firstResponse {
            var writer = TLWriter()
            writer.writeInt32(Int32(bitPattern: 0xedab447b))
            writer.writeInt64(request.messageID)
            writer.writeInt32(request.sequence)
            writer.writeInt32(48)
            writer.writeInt64(newSalt)
            body = writer.data
        } else {
            var writer = TLWriter()
            writer.writeInt32(-212046591)
            writer.writeInt64(request.messageID)
            writer.append(result)
            body = writer.data
        }
        responseCount += 1
        let encrypted = try MTProtoCrypto.encryptedMessage(
            authKey: authKey,
            salt: responseCount == 1 ? request.salt : 99,
            sessionID: sessionID,
            messageID: request.messageID + 1,
            sequence: 0,
            body: body,
            direction: .serverToClient
        )
        pendingResponse = encrypted.packet
    }

    func receive() throws -> Data {
        guard ready, let pendingResponse else { throw MTProtoTransportError.connectionClosed }
        self.pendingResponse = nil
        receiveCount += 1
        return pendingResponse
    }

}

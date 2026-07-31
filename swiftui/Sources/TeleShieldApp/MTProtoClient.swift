import Foundation
import BigInt

private func defaultMTProtoHost(for dcID: Int) -> String {
    switch dcID {
    case 1: return "149.154.175.53"
    case 2: return "149.154.167.51"
    case 3: return "149.154.175.100"
    case 4: return "149.154.167.91"
    case 5: return "91.108.56.130"
    default: return "149.154.167.51"
    }
}

enum TelegramMTProtoError: LocalizedError {
    case invalidHandshake
    case invalidDHParameters
    case invalidNonce
    case factorizationFailed
    case transport(code: Int32)
    case rpc(code: Int32, message: String)
    case badMessage(code: Int32)
    case retryLimitExceeded
    case unsupportedResponse(Int32)

    var migratedDataCenter: Int? {
        guard case .rpc(_, let message) = self else { return nil }
        guard let value = message.uppercased().split(separator: "_").last,
              let dcID = Int(value), (1...5).contains(dcID) else { return nil }
        guard message.uppercased().contains("MIGRATE_") else { return nil }
        return dcID
    }

    var errorDescription: String? {
        switch self {
        case .invalidHandshake: return "Telegram MTProto 握手回應無效"
        case .invalidDHParameters: return "Telegram DH 參數無效"
        case .invalidNonce: return "Telegram nonce 驗證失敗"
        case .factorizationFailed: return "Telegram PQ 分解失敗"
        case .transport(let code):
            switch code {
            case -404: return "Telegram MTProto 握手資料被拒絕（-404）"
            case -444: return "Telegram MTProto 資料中心類型不符（-444）"
            default: return "Telegram MTProto transport 錯誤（\(code)）"
            }
        case .rpc(let code, let message): return "Telegram API \(code)：\(message)"
        case .badMessage(let code): return "Telegram MTProto 訊息無效（error code \(code)）"
        case .retryLimitExceeded: return "Telegram MTProto 訊息重送次數已達上限"
        case .unsupportedResponse(let id): return "Telegram 回傳未支援的 response：\(id)"
        }
    }
}

actor MTProtoClient {
    // Keep the layer in one place so a schema upgrade does not require
    // hunting through the transport and API call sites.
    // The current public Telegram TL schema is layer 223. Keep this value
    // versioned in one place so protocol upgrades are explicit and reviewable.
    private static let apiLayer: Int32 = 223

    private enum Constructor {
        static let reqPQMulti: Int32 = Int32(bitPattern: 0xbe7e8ef1)
        static let resPQ: Int32 = 0x05162463
        static let pqInnerDataDC: Int32 = Int32(bitPattern: 0xa9f55f95)
        static let reqDHParams: Int32 = Int32(bitPattern: 0xd712e4be)
        static let serverDHParamsOK: Int32 = Int32(bitPattern: 0xd0e8075c)
        static let serverDHInnerData: Int32 = Int32(bitPattern: 0xb5890dba)
        static let setClientDHParams: Int32 = Int32(bitPattern: 0xf5045f1f)
        static let clientDHInnerData: Int32 = Int32(bitPattern: 0x6643b654)
        static let dhGenOK: Int32 = Int32(bitPattern: 0x3bcbf734)
        static let dhGenRetry: Int32 = Int32(bitPattern: 0x46dc1fb9)
        static let dhGenFail: Int32 = Int32(bitPattern: 0xa69dae02)
        static let rpcResult: Int32 = -212046591
        static let rpcError: Int32 = 0x2144ca19
        static let msgContainer: Int32 = 0x73f1f8dc
        static let gzipPacked: Int32 = 0x3072cfa1
        static let badServerSalt: Int32 = Int32(bitPattern: 0xedab447b)
        static let badMsgNotification: Int32 = Int32(bitPattern: 0xa7eff811)
        static let newSessionCreated: Int32 = Int32(bitPattern: 0x9ec20908)
        static let msgsAck: Int32 = 0x62d6b459
        static let pong: Int32 = 0x347773c5
        static let msgDetailedInfo: Int32 = 0x276d3ec6
        static let msgNewDetailedInfo: Int32 = Int32(bitPattern: 0x809db6df)
        static let futureSalts: Int32 = Int32(bitPattern: 0xae500895)
        static let msgsStateInfo: Int32 = 0x04deb57d
        static let msgsAllInfo: Int32 = Int32(bitPattern: 0x8cc0d131)
        static let updatesTooLong: Int32 = -484987010
    }

    private enum ResponseDisposition {
        case result(Data)
        case retry(resetSession: Bool)
        case ignored
    }

    private var transport: any MTProtoTransporting
    private let transportFactory: @Sendable (Int) -> any MTProtoTransporting
    private let rsaKeys: [MTProtoRSAPublicKey]
    private let callGate = AsyncSemaphore()
    private var dcID: Int
    private var apiID: Int
    private var session: NativeSession?
    private var sequence = 0
    private var timeOffset: TimeInterval = 0
    private var salt: Int64 = 0
    private var sessionID: Int64 = Int64.random(in: Int64.min...Int64.max)
    private let fixedSessionID: Int64?
    private var lastMessageID: Int64 = 0
    private var receivedMessageIDs = Set<Int64>()
    private var connected = false

    init(
        apiID: Int,
        session: NativeSession? = nil,
        dcID: Int = 2,
        transport: (any MTProtoTransporting)? = nil,
        sessionID: Int64? = nil,
        host: String? = nil,
        port: UInt16 = 443,
        rsaKeys: [MTProtoRSAPublicKey] = MTProtoCrypto.masterRSAPublicKeys
    ) {
        self.apiID = apiID
        self.session = session
        self.dcID = dcID
        self.rsaKeys = rsaKeys
        self.fixedSessionID = sessionID
        let suppliedTransport = transport
        let initialDCID = dcID
        let initialHost = host ?? defaultMTProtoHost(for: dcID)
        let initialTransport = suppliedTransport ?? MTProtoTransport(host: initialHost, port: port)
        let initialSalt = session?.serverSalt ?? 0
        let initialSessionID = sessionID ?? Int64.random(in: Int64.min...Int64.max)
        let factory: @Sendable (Int) -> any MTProtoTransporting = { targetDCID in
            if let suppliedTransport { return suppliedTransport }
            let targetHost = targetDCID == initialDCID ? initialHost : defaultMTProtoHost(for: targetDCID)
            return MTProtoTransport(host: targetHost, port: port)
        }
        self.transport = initialTransport
        self.salt = initialSalt
        self.sessionID = initialSessionID
        self.transportFactory = factory
    }

    func updateAPIID(_ apiID: Int) {
        self.apiID = apiID
    }

    func currentSession() -> NativeSession? {
        session
    }

    func currentDataCenter() -> Int { dcID }

    func updateSessionUserID(_ userID: Int64) {
        guard var session else { return }
        session.userID = userID
        self.session = session
    }

    /// Creates a fresh auth key at a Telegram DC. An auth key is DC-bound, so
    /// reusing the old key on another endpoint would be invalid; callers that
    /// already have authorization must export/import it around this method.
    func switchDataCenter(to newDCID: Int) async throws {
        guard (1...5).contains(newDCID) else { throw TelegramMTProtoError.invalidHandshake }
        guard newDCID != dcID || session?.dcID != newDCID else { return }
        await transport.close()
        transport = transportFactory(newDCID)
        dcID = newDCID
        session = nil
        connected = false
        sessionID = fixedSessionID ?? Int64.random(in: Int64.min...Int64.max)
        sequence = 0
        timeOffset = 0
        salt = 0
        lastMessageID = 0
        receivedMessageIDs.removeAll(keepingCapacity: true)
        try await connect()
    }

    func connect() async throws {
        let transportReady = await transport.isReady()
        let wasConnected = connected && transportReady
        do {
            if !transportReady {
                await transport.close()
                connected = false
            }
            try await transport.connect()
            connected = true
            if !wasConnected {
                sessionID = fixedSessionID ?? Int64.random(in: Int64.min...Int64.max)
                sequence = 0
                lastMessageID = 0
                receivedMessageIDs.removeAll(keepingCapacity: true)
                salt = session?.serverSalt ?? 0
            }
            if session == nil || session?.authKey.count != 256 {
                let newSession = try await performHandshake()
                session = newSession
                salt = newSession.serverSalt
            } else {
                salt = session?.serverSalt ?? salt
            }
        } catch {
            await transport.close()
            connected = false
            throw error
        }
    }

    func disconnect() async {
        await transport.close()
        connected = false
    }

    private func resetConnection() async {
        await transport.close()
        connected = false
        sessionID = fixedSessionID ?? Int64.random(in: Int64.min...Int64.max)
        sequence = 0
        lastMessageID = 0
        receivedMessageIDs.removeAll(keepingCapacity: true)
    }

    func call(_ query: Data, wrapInInitConnection: Bool = true) async throws -> Data {
        try await callGate.acquire()
        do {
            let result = try await performCall(query, wrapInInitConnection: wrapInInitConnection)
            await callGate.release()
            return result
        } catch {
            await callGate.release()
            throw error
        }
    }

    private func performCall(_ query: Data, wrapInInitConnection: Bool) async throws -> Data {
        do {
            try await connect()
            guard let session, session.authKey.count == 256 else {
                throw TelegramMTProtoError.invalidHandshake
            }
            let body = wrapInInitConnection ? try makeInitConnection(query) : query
            attemptLoop: for attempt in 0..<4 {
                try Task.checkCancellation()
                let messageID = nextMessageID()
                let seqNo = nextSequence()
                let encrypted = try MTProtoCrypto.encryptedMessage(
                    authKey: session.authKey,
                    salt: salt,
                    sessionID: sessionID,
                    messageID: messageID,
                    sequence: seqNo,
                    body: body
                )
                try await transport.send(encrypted.packet)

                while true {
                    try Task.checkCancellation()
                    let frame = try await transport.receive()
                    let response = try MTProtoCrypto.decryptMessage(authKey: session.authKey, sessionID: sessionID, packet: frame)
                    guard response.messageID & 3 == 1 || response.messageID & 3 == 3 else {
                        throw MTProtoCryptoError.invalidMessage
                    }
                    guard registerServerMessageID(response.messageID) else { continue }
                    updateServerSalt(response.salt)
                    switch try extractRPCResult(response.body, expectedMessageID: messageID, remoteMessageID: response.messageID) {
                    case .result(let result):
                        return result
                    case .ignored:
                        continue
                    case .retry(let resetSession):
                        if attempt == 3 { throw TelegramMTProtoError.retryLimitExceeded }
                        if resetSession {
                            await resetConnection()
                            try await connect()
                        }
                        continue attemptLoop
                    }
                }
            }
            throw TelegramMTProtoError.retryLimitExceeded
        } catch let error as TelegramMTProtoError {
            if case .rpc = error {
                throw error
            }
            await resetConnection()
            throw error
        } catch {
            await resetConnection()
            throw error
        }
    }

    private func updateServerSalt(_ value: Int64) {
        salt = value
        session?.serverSalt = value
    }

    private func registerServerMessageID(_ messageID: Int64) -> Bool {
        guard messageID & 3 == 1 || messageID & 3 == 3 else { return false }
        guard !receivedMessageIDs.contains(messageID) else { return false }
        if let lowest = receivedMessageIDs.min(), messageID < lowest { return false }
        if receivedMessageIDs.count >= 2_048, let lowest = receivedMessageIDs.min() {
            receivedMessageIDs.remove(lowest)
        }
        receivedMessageIDs.insert(messageID)
        return true
    }

    private func performHandshake() async throws -> NativeSession {
        let nonce = try MTProtoCrypto.randomData(count: 16)
        var pqRequest = TLWriter()
        pqRequest.writeInt32(Constructor.reqPQMulti)
        pqRequest.writeInt128(nonce)
        let pqResponse = try await sendPlain(pqRequest.data)
        var pqReader = TLReader(pqResponse)
        guard try pqReader.readInt32() == Constructor.resPQ else { throw TelegramMTProtoError.invalidHandshake }
        guard try pqReader.readInt128() == nonce else { throw TelegramMTProtoError.invalidNonce }
        let serverNonce = try pqReader.readInt128()
        let pq = try pqReader.readBytes()
        let fingerprints = try pqReader.readVector { reader in try reader.readUInt64() }
        guard pqReader.remaining == 0 else { throw TLCodecError.invalidLength }
        let (factorP, factorQ) = try factorPQ(MTProtoCrypto.bigUInt(pq))
        let p = min(factorP, factorQ)
        let q = max(factorP, factorQ)
        let newNonce = try MTProtoCrypto.randomData(count: 32)
        guard let rsaKey = fingerprints.lazy.compactMap({ fingerprint in
            self.rsaKeys.first(where: { $0.fingerprint == fingerprint })
        }).first else {
            throw MTProtoCryptoError.unknownServerKey
        }
        let fingerprint = rsaKey.fingerprint

        var inner = TLWriter()
        inner.writeInt32(Constructor.pqInnerDataDC)
        try inner.writeBytes(pq)
        try inner.writeBytes(p.serialize())
        try inner.writeBytes(q.serialize())
        inner.writeInt128(nonce)
        inner.writeInt128(serverNonce)
        inner.writeInt256(newNonce)
        inner.writeInt32(Int32(dcID))
        let encryptedInner = try MTProtoCrypto.rsaEncrypt(inner.data, publicKey: rsaKey)

        var dhRequest = TLWriter()
        dhRequest.writeInt32(Constructor.reqDHParams)
        dhRequest.writeInt128(nonce)
        dhRequest.writeInt128(serverNonce)
        try dhRequest.writeBytes(p.serialize())
        try dhRequest.writeBytes(q.serialize())
        dhRequest.writeInt64(Int64(bitPattern: fingerprint))
        try dhRequest.writeBytes(encryptedInner)
        let dhResponse = try await sendPlain(dhRequest.data)
        var dhReader = TLReader(dhResponse)
        guard try dhReader.readInt32() == Constructor.serverDHParamsOK else {
            throw TelegramMTProtoError.invalidHandshake
        }
        guard try dhReader.readInt128() == nonce, try dhReader.readInt128() == serverNonce else {
            throw TelegramMTProtoError.invalidNonce
        }
        let encryptedAnswer = try dhReader.readBytes()
        guard dhReader.remaining == 0 else { throw TLCodecError.invalidLength }
        let (tmpKey, tmpIV) = makeTemporaryKey(newNonce: newNonce, serverNonce: serverNonce)
        let decryptedAnswer = try MTProtoCrypto.aesIGE(encryptedAnswer, key: tmpKey, iv: tmpIV, encrypt: false)
        guard decryptedAnswer.count >= 20 else { throw TelegramMTProtoError.invalidHandshake }
        let serverPayload = decryptedAnswer.dropFirst(20)
        var answerReader = TLReader(Data(serverPayload))
        guard try answerReader.readInt32() == Constructor.serverDHInnerData else {
            throw TelegramMTProtoError.invalidHandshake
        }
        guard try answerReader.readInt128() == nonce, try answerReader.readInt128() == serverNonce else {
            throw TelegramMTProtoError.invalidNonce
        }
        let generator = try answerReader.readInt32()
        let dhPrimeData = try answerReader.readBytes()
        let gAData = try answerReader.readBytes()
        guard generator >= 2, generator <= 7,
              dhPrimeData.count == 256,
              gAData.count == 256,
              MTProtoCrypto.isTrustedDHPrime(dhPrimeData, generator: generator) else {
            throw TelegramMTProtoError.invalidDHParameters
        }
        let g = BigUInt(UInt64(generator))
        let dhPrime = MTProtoCrypto.bigUInt(dhPrimeData)
        let gA = MTProtoCrypto.bigUInt(gAData)
        let serverTime = try answerReader.readInt32()
        let answerLength = answerReader.offset
        guard answerReader.remaining < 16,
              MTProtoCrypto.sha1(Data(serverPayload.prefix(answerLength))) == decryptedAnswer.prefix(20) else {
            throw TelegramMTProtoError.invalidHandshake
        }
        guard g > 1, gA > 1, gA < dhPrime - 1, dhPrime > 2 else {
            throw TelegramMTProtoError.invalidDHParameters
        }
        timeOffset = TimeInterval(serverTime) - Date().timeIntervalSince1970

        let secret = try MTProtoCrypto.randomData(count: 256)
        let secretValue = MTProtoCrypto.bigUInt(secret)
        let gB = g.power(secretValue, modulus: dhPrime)
        guard gB > 1, gB < dhPrime - 1 else {
            throw TelegramMTProtoError.invalidDHParameters
        }
        let authKeyValue = gA.power(secretValue, modulus: dhPrime)
        guard authKeyValue > 1, authKeyValue < dhPrime - 1 else {
            throw TelegramMTProtoError.invalidDHParameters
        }
        let authKey = MTProtoCrypto.fixedBigUInt(authKeyValue, count: 256)
        var clientInner = TLWriter()
        clientInner.writeInt32(Constructor.clientDHInnerData)
        clientInner.writeInt128(nonce)
        clientInner.writeInt128(serverNonce)
        clientInner.writeInt64(0)
        try clientInner.writeBytes(MTProtoCrypto.fixedBigUInt(gB, count: 256))
        let clientPayload = try padForIGE(MTProtoCrypto.sha1(clientInner.data) + clientInner.data)
        let encryptedClient = try MTProtoCrypto.aesIGE(clientPayload, key: tmpKey, iv: tmpIV, encrypt: true)
        var setDH = TLWriter()
        setDH.writeInt32(Constructor.setClientDHParams)
        setDH.writeInt128(nonce)
        setDH.writeInt128(serverNonce)
        try setDH.writeBytes(encryptedClient)
        let dhGen = try await sendPlain(setDH.data)
        var genReader = TLReader(dhGen)
        let genConstructor = try genReader.readInt32()
        guard genConstructor == Constructor.dhGenOK else {
            if genConstructor == Constructor.dhGenRetry || genConstructor == Constructor.dhGenFail {
                throw TelegramMTProtoError.invalidHandshake
            }
            throw TelegramMTProtoError.unsupportedResponse(genConstructor)
        }
        guard try genReader.readInt128() == nonce, try genReader.readInt128() == serverNonce else {
            throw TelegramMTProtoError.invalidNonce
        }
        let expectedHash = MTProtoCrypto.sha1(newNonce + Data([1]) + MTProtoCrypto.sha1(authKey).prefix(8)).subdata(in: 4..<20)
        guard Data(try genReader.readInt128()) == expectedHash else {
            throw TelegramMTProtoError.invalidHandshake
        }
        guard genReader.remaining == 0 else { throw TLCodecError.invalidLength }
        var newNonceReader = TLReader(Data(newNonce.prefix(8)))
        var serverNonceReader = TLReader(Data(serverNonce.prefix(8)))
        let serverSalt = try newNonceReader.readInt64() ^ serverNonceReader.readInt64()
        return NativeSession(dcID: dcID, authKey: authKey, userID: 0, serverSalt: serverSalt, date: Date())
    }

    private func sendPlain(_ body: Data) async throws -> Data {
        if !connected {
            try await transport.connect()
            connected = true
        }
        let messageID = nextMessageID()
        var packet = TLWriter()
        packet.writeInt64(0)
        packet.writeInt64(messageID)
        packet.writeInt32(Int32(body.count))
        packet.append(body)
        try await transport.send(packet.data)
        let response = try await transport.receive()
        if response.count == 4 {
            var errorReader = TLReader(response)
            let code = try errorReader.readInt32()
            if code < 0 {
                throw TelegramMTProtoError.transport(code: code)
            }
        }
        var reader = TLReader(response)
        guard try reader.readInt64() == 0 else { throw TelegramMTProtoError.invalidHandshake }
        _ = try reader.readInt64()
        let length = Int(try reader.readInt32())
        guard length >= 0, length <= reader.remaining else { throw TelegramMTProtoError.invalidHandshake }
        let body = try reader.readFixed(count: length)
        guard reader.remaining == 0 else { throw TelegramMTProtoError.invalidHandshake }
        return body
    }

    private func makeInitConnection(_ query: Data) throws -> Data {
        var withoutUpdates = TLWriter()
        withoutUpdates.writeInt32(TLConstructor.invokeWithoutUpdates)
        withoutUpdates.append(query)
        var initConnection = TLWriter()
        initConnection.writeInt32(TLConstructor.initConnection)
        initConnection.writeInt32(0)
        initConnection.writeInt32(Int32(apiID))
        try initConnection.writeString("TeleShield")
        try initConnection.writeString(ProcessInfo.processInfo.operatingSystemVersionString)
        try initConnection.writeString("1.0")
        try initConnection.writeString("en")
        try initConnection.writeString("")
        try initConnection.writeString("en")
        initConnection.append(withoutUpdates.data)
        var result = TLWriter()
        result.writeInt32(TLConstructor.invokeWithLayer)
        result.writeInt32(Self.apiLayer)
        result.append(initConnection.data)
        return result.data
    }

    private func extractRPCResult(_ data: Data, expectedMessageID: Int64, remoteMessageID: Int64) throws -> ResponseDisposition {
        var reader = TLReader(data)
        let constructor = try reader.readInt32()
        switch constructor {
        case Constructor.rpcResult:
            let requestID = try reader.readInt64()
            let result = try unwrapGZipIfNeeded(try reader.readFixed(count: reader.remaining))
            guard requestID == expectedMessageID else { return .ignored }
            return .result(try unwrapRPCErrorIfNeeded(result))
        case Constructor.rpcError:
            let code = try reader.readInt32()
            let message = try reader.readString()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            throw TelegramMTProtoError.rpc(code: code, message: message)
        case Constructor.msgContainer:
            let count = Int(try reader.readInt32())
            guard count >= 0, count <= 1_024 else { throw TLCodecError.invalidVector }
            var shouldRetry = false
            var shouldResetSession = false
            for _ in 0..<count {
                let innerMessageID = try reader.readInt64()
                _ = try reader.readInt32()
                let length = Int(try reader.readInt32())
                guard innerMessageID & 3 == 1 || innerMessageID & 3 == 3,
                      innerMessageID < remoteMessageID,
                      length >= 4,
                      length.isMultiple(of: 4),
                      length <= reader.remaining else {
                    throw TLCodecError.invalidLength
                }
                let inner = try reader.readFixed(count: length)
                guard registerServerMessageID(innerMessageID) else { continue }
                switch try extractRPCResult(inner, expectedMessageID: expectedMessageID, remoteMessageID: remoteMessageID) {
                case .result(let result): return .result(result)
                case .retry(let resetSession):
                    shouldRetry = true
                    shouldResetSession = shouldResetSession || resetSession
                case .ignored: break
                }
            }
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return shouldRetry ? .retry(resetSession: shouldResetSession) : .ignored
        case Constructor.gzipPacked:
            let packed = try reader.readBytes()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            let unpacked = try GZipDecoder.decompress(packed)
            return try extractRPCResult(unpacked, expectedMessageID: expectedMessageID, remoteMessageID: remoteMessageID)
        case Constructor.badServerSalt:
            let badMessageID = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            let newSalt = try reader.readInt64()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            guard badMessageID == expectedMessageID else { return .ignored }
            updateServerSalt(newSalt)
            return .retry(resetSession: false)
        case Constructor.badMsgNotification:
            let badMessageID = try reader.readInt64()
            _ = try reader.readInt32()
            let errorCode = try reader.readInt32()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            guard badMessageID == expectedMessageID else { return .ignored }
            switch errorCode {
            case 16, 17:
                updateTimeOffset(using: remoteMessageID)
                return .retry(resetSession: false)
            case 18:
                return .retry(resetSession: false)
            case 32:
                return .retry(resetSession: true)
            case 33:
                return .retry(resetSession: true)
            default:
                throw TelegramMTProtoError.badMessage(code: errorCode)
            }
        case Constructor.newSessionCreated:
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            updateServerSalt(try reader.readInt64())
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.msgsAck:
            _ = try reader.readVector { reader in try reader.readInt64() }
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.pong:
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.msgDetailedInfo:
            _ = try reader.readInt64()
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.msgNewDetailedInfo:
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.futureSalts:
            _ = try reader.readInt64()
            _ = try reader.readInt32()
            _ = try reader.readVector { reader in
                _ = try reader.readInt32()
                _ = try reader.readInt32()
                _ = try reader.readInt64()
                return ()
            }
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.msgsStateInfo:
            _ = try reader.readInt64()
            _ = try reader.readBytes()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.msgsAllInfo:
            _ = try reader.readVector { reader in try reader.readInt64() }
            _ = try reader.readBytes()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        case Constructor.updatesTooLong:
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            return .ignored
        default:
            // Updates and other server-side notifications may arrive while a
            // request is in flight. They are deliberately ignored here; the
            // coordinator uses bounded polling and never treats an unsolicited
            // object as the result of the current RPC.
            return .ignored
        }
    }

    private func unwrapGZipIfNeeded(_ data: Data) throws -> Data {
        var current = data
        for _ in 0..<4 {
            var reader = TLReader(current)
            guard let constructor = try? reader.readInt32(), constructor == Constructor.gzipPacked else {
                return current
            }
            let packed = try reader.readBytes()
            guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
            current = try GZipDecoder.decompress(packed)
        }
        throw TelegramMTProtoError.unsupportedResponse(Constructor.gzipPacked)
    }

    private func unwrapRPCErrorIfNeeded(_ data: Data) throws -> Data {
        var reader = TLReader(data)
        guard let constructor = try? reader.readInt32(), constructor == Constructor.rpcError else {
            return data
        }
        let code = try reader.readInt32()
        let message = try reader.readString()
        guard reader.remaining == 0 else { throw TLCodecError.invalidLength }
        throw TelegramMTProtoError.rpc(code: code, message: message)
    }

    private func nextMessageID() -> Int64 {
        let seconds = Date().timeIntervalSince1970 + timeOffset
        var value = Int64(seconds * 4_294_967_296.0)
        value &= ~Int64(3)
        if value <= lastMessageID { value = lastMessageID &+ 4 }
        lastMessageID = value
        return value
    }

    private func updateTimeOffset(using remoteMessageID: Int64) {
        let remoteSeconds = remoteMessageID >> 32
        timeOffset = TimeInterval(remoteSeconds) - Date().timeIntervalSince1970
        lastMessageID = 0
    }

    private func nextSequence() -> Int32 {
        defer { sequence += 1 }
        return Int32(sequence * 2 + 1)
    }

    private func makeTemporaryKey(newNonce: Data, serverNonce: Data) -> (Data, Data) {
        let tmpA = MTProtoCrypto.sha1(newNonce + serverNonce)
        let tmpB = MTProtoCrypto.sha1(serverNonce + newNonce)
        let tmpC = MTProtoCrypto.sha1(newNonce + newNonce)
        let key = tmpA + tmpB.prefix(12)
        let iv = tmpB.suffix(8) + tmpC + newNonce.prefix(4)
        return (key, iv)
    }

    private func padForIGE(_ data: Data) throws -> Data {
        var result = data
        var count = (16 - result.count % 16) % 16
        if count == 0 { count = 16 }
        result.append(try MTProtoCrypto.randomData(count: count))
        return result
    }

    private func factorPQ(_ value: BigUInt) throws -> (BigUInt, BigUInt) {
        guard value > 3 else { throw TelegramMTProtoError.factorizationFailed }
        if value % 2 == 0 { return (2, value / 2) }
        for _ in 0..<32 {
            var x = BigUInt(2)
            var y = BigUInt(2)
            let c = BigUInt(try MTProtoCrypto.randomData(count: 8)) % (value - 1) + 1
            var divisor = BigUInt(1)
            for _ in 0..<100_000 {
                x = (x * x + c) % value
                y = (y * y + c) % value
                y = (y * y + c) % value
                let difference = x > y ? x - y : y - x
                divisor = difference.greatestCommonDivisor(with: value)
                if divisor > 1 && divisor < value { return (divisor, value / divisor) }
                if divisor == value { break }
            }
        }
        throw TelegramMTProtoError.factorizationFailed
    }

}

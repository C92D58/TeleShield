import Foundation
import BigInt

struct TelegramSentCode: Sendable {
    let phoneCodeHash: String
    let deliveryDescription: String
}

struct TelegramPasswordChallenge: Sendable {
    let srpID: Int64
    let srpB: Data
    let salt1: Data
    let salt2: Data
    let generator: Int32
    let prime: Data
    let hint: String
}

enum TelegramAPIError: LocalizedError {
    case invalidResponse
    case unsupportedPasswordAlgorithm
    case passwordParameterInvalid
    case signUpRequired

    var errorDescription: String? {
        switch self {
        case .invalidResponse: return "Telegram API 回應格式無效"
        case .unsupportedPasswordAlgorithm: return "目前 Telegram 密碼演算法尚未支援"
        case .passwordParameterInvalid: return "Telegram SRP 參數無效"
        case .signUpRequired: return "此電話號碼尚未註冊 Telegram 帳號"
        }
    }
}

actor TelegramAPI {
    private enum Method {
        static let sendCode: Int32 = -1502141361
        static let signIn: Int32 = -1923962543
        static let checkPassword: Int32 = -779399914
        static let getPassword: Int32 = 1418342645
        static let logOut: Int32 = 1047706137
    }

    private enum Constructor {
        static let sentCode: Int32 = 1577067778
        static let sentCodeSuccess: Int32 = 596704836
        static let authorization: Int32 = 782418132
        static let authorizationSignUpRequired: Int32 = 1148485274
        static let password: Int32 = -1787080453
        static let passwordKDF: Int32 = 982592842
        static let user: Int32 = 829899656
        static let inputCheckPasswordSRP: Int32 = -763367294
        static let exportedAuthorization: Int32 = -1271602504
    }

    let client: MTProtoClient
    private let apiID: Int
    private let apiHash: String
    private let sessionDidChange: (@Sendable (NativeSession) async -> Void)?

    init(
        apiID: Int,
        apiHash: String,
        session: NativeSession? = nil,
        sessionDidChange: (@Sendable (NativeSession) async -> Void)? = nil,
        transport: (any MTProtoTransporting)? = nil,
        sessionID: Int64? = nil
    ) {
        self.apiID = apiID
        self.apiHash = apiHash
        self.sessionDidChange = sessionDidChange
        // An auth key is bound to its data center. Reusing the default DC
        // would make a valid session fail after a DC migration or import.
        self.client = MTProtoClient(
            apiID: apiID,
            session: session,
            dcID: session?.dcID ?? 2,
            transport: transport,
            sessionID: sessionID
        )
    }

    func connect() async throws {
        try await client.connect()
    }

    func disconnect() async {
        await client.disconnect()
    }

    func session() async -> NativeSession? {
        await client.currentSession()
    }

    func currentDataCenter() async -> Int {
        await client.currentDataCenter()
    }

    /// Creates an isolated, unauthenticated MTProto client for a Telegram CDN
    /// data center. CDN auth keys are deliberately not installed on the
    /// account client: the CDN protocol uses its own RSA key and file token,
    /// and the account session must remain bound to its home DC.
    func makeCDNClient(host: String, port: UInt16, dcID: Int, rsaKeys: [MTProtoRSAPublicKey]) -> MTProtoClient {
        MTProtoClient(apiID: apiID, dcID: dcID, host: host, port: port, rsaKeys: rsaKeys)
    }

    /// File media may redirect to a file DC. Restore the account API client
    /// after the transfer so the next ordinary request does not needlessly
    /// bounce between the file DC and the account's home DC.
    func restoreDataCenter(_ targetDCID: Int) async throws {
        guard await client.currentDataCenter() != targetDCID else { return }
        try await migrate(to: targetDCID)
    }

    /// Executes an API request and handles Telegram's explicit DC migration
    /// errors. Unauthenticated flows only create a fresh auth key at the new
    /// DC; authenticated flows export/import the authorization before retrying
    /// the original request.
    func call(_ request: Data) async throws -> Data {
        do {
            return try await client.call(request)
        } catch let error as TelegramMTProtoError {
            guard let targetDCID = error.migratedDataCenter else { throw error }
            try await migrate(to: targetDCID)
            return try await client.call(request)
        }
    }

    private func migrate(to targetDCID: Int) async throws {
        let currentSession = await client.currentSession()
        guard currentSession?.userID ?? 0 != 0 else {
            try await client.switchDataCenter(to: targetDCID)
            return
        }

        var exportRequest = TLWriter()
        exportRequest.writeInt32(-440401971) // auth.exportAuthorization
        exportRequest.writeInt32(Int32(targetDCID))
        let exported = try await client.call(exportRequest.data)
        var exportReader = TLReader(exported)
        guard try exportReader.readInt32() == Constructor.exportedAuthorization else {
            throw TelegramAPIError.invalidResponse
        }
        let exportID = try exportReader.readInt64()
        let exportBytes = try exportReader.readBytes()
        guard exportReader.remaining == 0 else { throw TelegramAPIError.invalidResponse }

        try await client.switchDataCenter(to: targetDCID)
        var importRequest = TLWriter()
        importRequest.writeInt32(-1518699091) // auth.importAuthorization
        importRequest.writeInt64(exportID)
        try importRequest.writeBytes(exportBytes)
        let imported = try await client.call(importRequest.data)
        let user = try parseAuthorization(imported)
        await client.updateSessionUserID(user.id)
        if let updatedSession = await client.currentSession() {
            await sessionDidChange?(updatedSession)
        }
    }

    func sendCode(phone: String) async throws -> TelegramSentCode {
        var request = TLWriter()
        request.writeInt32(Method.sendCode)
        request.writeInt32(0) // flags: allow_flashcall/current_number
        try request.writeString(phone)
        request.writeInt32(Int32(apiID))
        try request.writeString(apiHash)
        request.writeInt32(-1390068360)
        request.writeInt32(0)
        let response = try await call(request.data)
        var reader = TLReader(response)
        guard try reader.readInt32() == Constructor.sentCode else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        let delivery = try readSentCodeType(&reader)
        let phoneCodeHash = try reader.readString()
        if flags & 2 != 0 { try skipCodeType(&reader) }
        if flags & 4 != 0 { _ = try reader.readInt32() }
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return TelegramSentCode(phoneCodeHash: phoneCodeHash, deliveryDescription: delivery)
    }

    func signIn(phone: String, phoneCodeHash: String, code: String) async throws -> NativeUser {
        var request = TLWriter()
        request.writeInt32(Method.signIn)
        request.writeInt32(1)
        try request.writeString(phone)
        try request.writeString(phoneCodeHash)
        try request.writeString(code)
        let response = try await call(request.data)
        return try parseAuthorization(response)
    }

    func passwordChallenge() async throws -> TelegramPasswordChallenge {
        var request = TLWriter()
        request.writeInt32(Method.getPassword)
        let response = try await call(request.data)
        var reader = TLReader(response)
        guard try reader.readInt32() == Constructor.password else { throw TelegramAPIError.invalidResponse }
        let flags = try reader.readInt32()
        guard flags & 4 != 0 else { throw TelegramAPIError.invalidResponse }
        guard try reader.readInt32() == Constructor.passwordKDF else {
            throw TelegramAPIError.unsupportedPasswordAlgorithm
        }
        let salt1 = try reader.readBytes()
        let salt2 = try reader.readBytes()
        let generator = try reader.readInt32()
        let prime = try reader.readBytes()
        let srpB = try reader.readBytes()
        let srpID = try reader.readInt64()
        let hint = flags & 8 != 0 ? try reader.readString() : ""
        if flags & 16 != 0 { _ = try reader.readString() }
        try skipPasswordKDFAlgorithm(&reader) // new_algo
        try skipSecurePasswordKDFAlgorithm(&reader)
        _ = try reader.readBytes() // secure_random
        if flags & 32 != 0 { _ = try reader.readInt32() }
        if flags & 64 != 0 { _ = try reader.readString() }
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return TelegramPasswordChallenge(
            srpID: srpID,
            srpB: srpB,
            salt1: salt1,
            salt2: salt2,
            generator: generator,
            prime: prime,
            hint: hint
        )
    }

    func checkPassword(_ challenge: TelegramPasswordChallenge, password: String) async throws -> NativeUser {
        let passwordData = Data(password.utf8)
        let hash1 = MTProtoCrypto.sha256(challenge.salt1 + passwordData + challenge.salt1)
        let hash2 = MTProtoCrypto.sha256(challenge.salt2 + hash1 + challenge.salt2)
        let hash3 = try MTProtoCrypto.pbkdf2SHA512(password: hash2, salt: challenge.salt1, iterations: 100_000)
        let passwordHash = MTProtoCrypto.sha256(challenge.salt2 + hash3 + challenge.salt2)
        let prime = MTProtoCrypto.bigUInt(challenge.prime)
        let generator = BigUInt(UInt64(challenge.generator))
        let B = MTProtoCrypto.bigUInt(challenge.srpB)
        guard challenge.prime.count == 256,
              challenge.srpB.count == 256,
              prime > 2,
              generator >= 2,
              generator <= 7,
              MTProtoCrypto.isTrustedDHPrime(challenge.prime, generator: challenge.generator),
              B > 1,
              B < prime - 1 else {
            throw TelegramAPIError.passwordParameterInvalid
        }
        let p = MTProtoCrypto.fixedBigUInt(prime, count: 256)
        let g = MTProtoCrypto.fixedBigUInt(generator, count: 256)
        let b = MTProtoCrypto.fixedBigUInt(B, count: 256)
        let x = MTProtoCrypto.bigUInt(passwordHash)
        let gx = generator.power(x, modulus: prime)
        let k = MTProtoCrypto.bigUInt(MTProtoCrypto.sha256(p + g))
        let kgx = (k * gx) % prime
        var secret: BigUInt
        var A: BigUInt
        var ABytes: Data
        var u: BigUInt
        repeat {
            secret = MTProtoCrypto.bigUInt(try MTProtoCrypto.randomData(count: 256))
            A = generator.power(secret, modulus: prime)
            ABytes = MTProtoCrypto.fixedBigUInt(A, count: 256)
            u = MTProtoCrypto.bigUInt(MTProtoCrypto.sha256(ABytes + b))
        } while A <= 1 || A >= prime - 1 || u == 0
        let gb = (B + prime - kgx) % prime
        guard gb > 1 else { throw TelegramAPIError.passwordParameterInvalid }
        let shared = gb.power(secret + u * x, modulus: prime)
        guard shared > 1 else { throw TelegramAPIError.passwordParameterInvalid }
        let key = MTProtoCrypto.sha256(MTProtoCrypto.fixedBigUInt(shared, count: 256))
        let saltHash1 = MTProtoCrypto.sha256(challenge.salt1)
        let saltHash2 = MTProtoCrypto.sha256(challenge.salt2)
        let digest = xor(
            MTProtoCrypto.sha256(p),
            MTProtoCrypto.sha256(g)
        )
        let M1 = MTProtoCrypto.sha256(digest + saltHash1 + saltHash2 + ABytes + b + key)

        var request = TLWriter()
        request.writeInt32(Method.checkPassword)
        request.writeInt32(Constructor.inputCheckPasswordSRP)
        request.writeInt64(challenge.srpID)
        try request.writeBytes(ABytes)
        try request.writeBytes(M1)
        let response = try await call(request.data)
        return try parseAuthorization(response)
    }

    func logOut() async throws {
        var request = TLWriter()
        request.writeInt32(Method.logOut)
        let response = try await call(request.data)
        var reader = TLReader(response)
        switch try reader.readInt32() {
        case Int32(bitPattern: 0x997275b5), Int32(bitPattern: 0xbc799737): // Bool
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func parseAuthorization(_ data: Data) throws -> NativeUser {
        var reader = TLReader(data)
        let constructor = try reader.readInt32()
        switch constructor {
        case Constructor.authorization:
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readInt32() }
            if flags & 2 != 0 { _ = try reader.readInt32() }
            if flags & 4 != 0 { _ = try reader.readBytes() }
            let user = try readUser(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return user
        case Constructor.authorizationSignUpRequired:
            throw TelegramAPIError.signUpRequired
        case Constructor.sentCodeSuccess:
            throw TelegramAPIError.invalidResponse
        default:
            throw TelegramAPIError.invalidResponse
        }
    }


    private func skipPasswordKDFAlgorithm(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0xd45ab096: // passwordKdfAlgoUnknown
            break
        case 0x3a912d4a: // passwordKdfAlgoSHA256SHA256PBKDF2HMACSHA512iter100000SHA256ModPow
            _ = try reader.readBytes()
            _ = try reader.readBytes()
            _ = try reader.readInt32()
            _ = try reader.readBytes()
        default:
            throw TelegramAPIError.unsupportedPasswordAlgorithm
        }
    }

    private func skipSecurePasswordKDFAlgorithm(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x004a8537: // securePasswordKdfAlgoUnknown
            break
        case 0xbbf2dda0, 0x86471d92:
            _ = try reader.readBytes()
        default:
            throw TelegramAPIError.unsupportedPasswordAlgorithm
        }
    }

    private func readSentCodeType(_ reader: inout TLReader) throws -> String {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x3dbb5986: // auth.sentCodeTypeApp
            _ = try reader.readInt32()
            return "Telegram App"
        case 0xc000bba2: // auth.sentCodeTypeSms
            _ = try reader.readInt32()
            return "SMS"
        case 0x5353e5a7: // auth.sentCodeTypeCall
            _ = try reader.readInt32()
            return "電話"
        case 0xab03c6d9: // auth.sentCodeTypeFlashCall
            _ = try reader.readString()
            return "Flash Call"
        case 0x82006484: // auth.sentCodeTypeMissedCall
            _ = try reader.readString()
            _ = try reader.readInt32()
            return "Missed Call"
        case 0xf450f59b: // auth.sentCodeTypeEmailCode
            let flags = try reader.readInt32()
            _ = try reader.readString()
            _ = try reader.readInt32()
            if flags & 8 != 0 { _ = try reader.readInt32() }
            if flags & 16 != 0 { _ = try reader.readInt32() }
            return "Email"
        case 0xa5491dea: // auth.sentCodeTypeSetUpEmailRequired
            _ = try reader.readInt32()
            return "Email"
        case 0xd9565c39: // auth.sentCodeTypeFragmentSms
            _ = try reader.readString()
            _ = try reader.readInt32()
            return "Fragment SMS"
        case 0x009fd736: // auth.sentCodeTypeFirebaseSms
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readBytes() }
            if flags & 4 != 0 {
                _ = try reader.readInt64()
                _ = try reader.readBytes()
            }
            if flags & 2 != 0 {
                _ = try reader.readString()
                _ = try reader.readInt32()
            }
            _ = try reader.readInt32()
            return "SMS"
        case 0xa416ac81: // auth.sentCodeTypeSmsWord
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            return "SMS"
        case 0xb37794af: // auth.sentCodeTypeSmsPhrase
            let flags = try reader.readInt32()
            if flags & 1 != 0 { _ = try reader.readString() }
            return "SMS"
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    private func skipCodeType(_ reader: inout TLReader) throws {
        switch UInt32(bitPattern: try reader.readInt32()) {
        case 0x72a3158c, 0x741cd3e3, 0x226ccefb, 0xd61ad6ee, 0x06ed998c:
            break
        default:
            throw TelegramAPIError.invalidResponse
        }
    }
}

private func xor(_ lhs: Data, _ rhs: Data) -> Data {
    Data(zip(lhs, rhs).map { $0 ^ $1 })
}

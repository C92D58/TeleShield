sed: --: No such file or directory
import Foundation
import CryptoKit
import BigInt
import Security
#if canImport(CommonCrypto)
import CommonCrypto
#endif

enum MTProtoCryptoError: LocalizedError {
    case invalidAuthKey
    case invalidMessage
    case unknownServerKey
    case rsaEncryptionFailed
    case aesFailure
    case randomFailure
    case invalidKDFParameters

    var errorDescription: String? {
        switch self {
        case .invalidAuthKey: return "Telegram auth key 長度無效"
        case .invalidMessage: return "Telegram 加密訊息格式無效"
        case .unknownServerKey: return "Telegram 回傳了不支援的 RSA key fingerprint"
        case .rsaEncryptionFailed: return "Telegram RSA 加密失敗"
        case .aesFailure: return "Telegram AES-IGE 加解密失敗"
        case .randomFailure: return "無法取得安全隨機資料"
        case .invalidKDFParameters: return "Telegram KDF 參數無效"
        }
    }
}

enum MTProtoMessageDirection: Sendable {
    case clientToServer
    case serverToClient

    var keyDerivationOffset: Int {
        switch self {
        case .clientToServer: return 0
        case .serverToClient: return 8
        }
    }
}

/// A Telegram MTProto RSA key. The raw modulus/exponent are kept as bytes so
/// a CDN key learned at runtime can cross actor boundaries safely; the
/// expensive BigUInt conversion is performed only during RSA encryption.
struct MTProtoRSAPublicKey: Sendable, Equatable {
    let modulus: Data
    let exponent: Data
    let fingerprint: UInt64
}

enum MTProtoCrypto {
    static func randomData(count: Int) throws -> Data {
        guard count >= 0 else { throw MTProtoCryptoError.randomFailure }
        guard count > 0 else { return Data() }
        var data = Data(repeating: 0, count: count)
        let status = data.withUnsafeMutableBytes { buffer in
            guard let baseAddress = buffer.baseAddress else { return errSecParam }
            return SecRandomCopyBytes(kSecRandomDefault, count, baseAddress)
        }
        guard status == errSecSuccess else { throw MTProtoCryptoError.randomFailure }
        return data
    }

    static func sha1(_ data: Data) -> Data {
        Data(Insecure.SHA1.hash(data: data))
    }

    static func sha256(_ data: Data) -> Data {
        Data(SHA256.hash(data: data))
    }

    static func hmacSHA256(key: Data, message: Data) -> Data {
        let code = HMAC<SHA256>.authenticationCode(for: message, using: SymmetricKey(data: key))
        return Data(code)
    }

    static func pbkdf2SHA512(password: Data, salt: Data, iterations: Int, count: Int = 64) throws -> Data {
        guard iterations > 0, iterations <= Int(UInt32.max), count > 0 else {
            throw MTProtoCryptoError.invalidKDFParameters
        }
        #if canImport(CommonCrypto)
        var output = Data(repeating: 0, count: count)
        let status = output.withUnsafeMutableBytes { outputBuffer in
            password.withUnsafeBytes { passwordBuffer in
                salt.withUnsafeBytes { saltBuffer in
                    CCKeyDerivationPBKDF(
                        CCPBKDFAlgorithm(kCCPBKDF2),
                        passwordBuffer.bindMemory(to: Int8.self).baseAddress,
                        password.count,
                        saltBuffer.bindMemory(to: UInt8.self).baseAddress,
                        salt.count,
                        CCPseudoRandomAlgorithm(kCCPRFHmacAlgSHA512),
                        UInt32(iterations),
                        outputBuffer.bindMemory(to: UInt8.self).baseAddress,
                        count
                    )
                }
            }
        }
        guard status == kCCSuccess else { throw MTProtoCryptoError.aesFailure }
        return output
        #else
        throw MTProtoCryptoError.aesFailure
        #endif
    }

    static func authKeyID(_ authKey: Data) throws -> UInt64 {
        guard authKey.count == 256 else { throw MTProtoCryptoError.invalidAuthKey }
        let digest = sha1(authKey)
        var result: UInt64 = 0
        for index in 0..<8 { result |= UInt64(digest[digest.count - 8 + index]) << UInt64(index * 8) }
        return result
    }

    static func authKeyIDData(_ authKey: Data) throws -> Data {
        var writer = TLWriter()
        writer.writeUInt64(try authKeyID(authKey))
        return writer.data
    }

    static func encryptedMessage(
        authKey: Data,
        salt: Int64,
        sessionID: Int64,
        messageID: Int64,
        sequence: Int32,
        body: Data,
        direction: MTProtoMessageDirection = .clientToServer
    ) throws -> (messageKey: Data, packet: Data) {
        guard authKey.count == 256, body.count <= Int(Int32.max) else {
            throw MTProtoCryptoError.invalidAuthKey
        }
        var writer = TLWriter()
        writer.writeInt64(salt)
        writer.writeInt64(sessionID)
        writer.writeInt64(messageID)
        writer.writeInt32(sequence)
        writer.writeInt32(Int32(body.count))
        writer.append(body)
        var plain = writer.data
        let minimumPadding = 12
        var padding = (16 - (plain.count % 16)) % 16
        if padding < minimumPadding { padding += 16 }
        plain.append(try randomData(count: padding))
        let keyDerivationOffset = direction.keyDerivationOffset
        let msgKeyLarge = sha256(
            authKey.subdata(in: (88 + keyDerivationOffset)..<(120 + keyDerivationOffset)) + plain
        )
        let messageKey = msgKeyLarge.subdata(in: 8..<24)
        let (key, iv) = aesKeyAndIV(
            authKey: authKey,
            messageKey: messageKey,
            x: direction.keyDerivationOffset
        )
        let encrypted = try aesIGE(plain, key: key, iv: iv, encrypt: true)
        return (messageKey, try authKeyIDData(authKey) + messageKey + encrypted)
    }

    static func decryptMessage(
        authKey: Data,
        sessionID expectedSessionID: Int64,
        packet: Data,
        direction: MTProtoMessageDirection = .serverToClient
    ) throws -> (salt: Int64, messageID: Int64, sequence: Int32, body: Data) {
        guard authKey.count == 256, packet.count >= 24 else { throw MTProtoCryptoError.invalidMessage }
        let keyID = packet.prefix(8)
        guard keyID.elementsEqual(try authKeyIDData(authKey)) else { throw MTProtoCryptoError.invalidMessage }
        let messageKey = packet.subdata(in: 8..<24)
        let encrypted = packet.subdata(in: 24..<packet.count)
        guard encrypted.count >= 16, encrypted.count % 16 == 0 else { throw MTProtoCryptoError.invalidMessage }
        let (key, iv) = aesKeyAndIV(
            authKey: authKey,
            messageKey: messageKey,
            x: direction.keyDerivationOffset
        )
        let plain = try aesIGE(encrypted, key: key, iv: iv, encrypt: false)
        let keyDerivationOffset = direction.keyDerivationOffset
        let expected = sha256(
            authKey.subdata(in: (88 + keyDerivationOffset)..<(120 + keyDerivationOffset)) + plain
        ).subdata(in: 8..<24)
        guard expected == messageKey else { throw MTProtoCryptoError.invalidMessage }
        var reader = TLReader(plain)
        let salt = try reader.readInt64()
        let sessionID = try reader.readInt64()
        guard sessionID == expectedSessionID else { throw MTProtoCryptoError.invalidMessage }
        let messageID = try reader.readInt64()
        let sequence = try reader.readInt32()
        let length = Int(try reader.readInt32())
        guard length >= 0, length.isMultiple(of: 4), length <= reader.remaining else {
            throw MTProtoCryptoError.invalidMessage
        }
        let body = try reader.readFixed(count: length)
        guard reader.remaining >= 12, reader.remaining <= 1024 else {
            throw MTProtoCryptoError.invalidMessage
        }
        return (salt, messageID, sequence, body)
    }

    static func rsaEncrypt(_ data: Data, fingerprint: UInt64) throws -> Data {
        guard let key = rsaKeys.first(where: { $0.fingerprint == fingerprint }) else {
            throw MTProtoCryptoError.unknownServerKey
        }
        return try rsaEncrypt(data, publicKey: key)
    }

    static func rsaEncrypt(_ data: Data, publicKey: MTProtoRSAPublicKey) throws -> Data {
        guard data.count <= 144 else { throw MTProtoCryptoError.rsaEncryptionFailed }
        let dataWithPadding = data + (try randomData(count: 192 - data.count))
        let reversed = Data(dataWithPadding.reversed())
        let modulus = BigUInt(publicKey.modulus)
        let exponent = BigUInt(publicKey.exponent)

        for _ in 0..<64 {
            let temporaryKey = try randomData(count: 32)
            let payload = reversed + sha256(temporaryKey + dataWithPadding)
            let encryptedPayload = try aesIGE(
                payload,
                key: temporaryKey,
                iv: Data(repeating: 0, count: 32),
                encrypt: true
            )
            let adjustedKey = temporaryKey ^ sha256(encryptedPayload)
            let padded = adjustedKey + encryptedPayload
            let value = BigUInt(padded)
            guard value < modulus else { continue }
            let encrypted = value.power(exponent, modulus: modulus)
            let raw = encrypted.serialize()
            guard raw.count <= 256 else { throw MTProtoCryptoError.rsaEncryptionFailed }
            return Data(repeating: 0, count: 256 - raw.count) + raw
        }
        throw MTProtoCryptoError.rsaEncryptionFailed
    }

    static func isTrustedDHPrime(_ data: Data, generator: Int32) -> Bool {
        guard generator >= 2, generator <= 7 else { return false }
        guard data == trustedDHPrime else { return false }
        switch generator {
        case 2: return data.last.map { ($0 & 0x07) == 0x07 } ?? false
        case 3: return BigUInt(data) % 3 == 2
        case 4: return true
        case 5: return [1, 4].contains(Int(BigUInt(data) % 5))
        case 6: return [19, 23].contains(Int(BigUInt(data) % 24))
        case 7: return [3, 5, 6].contains(Int(BigUInt(data) % 7))
        default: return false
        }
    }

    static func hasRSAKey(fingerprint: UInt64) -> Bool {
        rsaKeys.contains { $0.fingerprint == fingerprint }
    }

    static var masterRSAPublicKeys: [MTProtoRSAPublicKey] { rsaKeys }

    /// Parses the PEM form returned by Telegram's `help.getCdnConfig`.
    /// Telegram has used both PKCS#1 (`RSA PUBLIC KEY`) and SubjectPublicKeyInfo
    /// (`PUBLIC KEY`) wrappers over time, so accept either representation.
    static func rsaPublicKey(fromPEM pem: String) -> MTProtoRSAPublicKey? {
        let payload = pem
            .components(separatedBy: "-----")
            .filter { !$0.contains("BEGIN") && !$0.contains("END") }
            .joined()
            .filter { !$0.isWhitespace }
        guard let der = Data(base64Encoded: String(payload)),
              let components = parseRSAPublicKeyDER(der) else { return nil }
        return makeRSAPublicKey(modulus: components.modulus, exponent: components.exponent)
    }

    /// AES-256-CTR with the big-endian counter convention required by
    /// Telegram's encrypted CDN file protocol.
    static func aesCTR(_ data: Data, key: Data, iv: Data) throws -> Data {
        guard key.count == 32, iv.count == 16 else { throw MTProtoCryptoError.aesFailure }
        guard !data.isEmpty else { return Data() }
        #if canImport(CommonCrypto)
        var output = Data(repeating: 0, count: data.count)
        var cryptor: CCCryptorRef?
        var moved = 0
        let status = output.withUnsafeMutableBytes { outputBuffer in
            data.withUnsafeBytes { inputBuffer in
                key.withUnsafeBytes { keyBuffer in
                    iv.withUnsafeBytes { ivBuffer in
                        CCCryptorCreateWithMode(
                            CCOperation(kCCEncrypt),
                            CCMode(kCCModeCTR),
                            CCAlgorithm(kCCAlgorithmAES),
                            CCPadding(ccNoPadding),
                            ivBuffer.baseAddress,
                            keyBuffer.baseAddress,
                            key.count,
                            nil,
                            0,
                            0,
                            CCModeOptions(kCCModeOptionCTR_BE),
                            &cryptor
                        )
                    }
                }
            }
        }
        guard status == kCCSuccess, let cryptor else { throw MTProtoCryptoError.aesFailure }
        defer { CCCryptorRelease(cryptor) }
        let outputCapacity = output.count
        let updateStatus = output.withUnsafeMutableBytes { outputBuffer in
            data.withUnsafeBytes { inputBuffer in
                CCCryptorUpdate(
                    cryptor,
                    inputBuffer.baseAddress,
                    data.count,
                    outputBuffer.baseAddress,
                    outputCapacity,
                    &moved
                )
            }
        }
        guard updateStatus == kCCSuccess, moved == data.count else { throw MTProtoCryptoError.aesFailure }
        return output
        #else
        throw MTProtoCryptoError.aesFailure
        #endif
    }

    static func bigUInt(_ data: Data) -> BigUInt {
        BigUInt(data)
    }

    static func fixedBigUInt(_ value: BigUInt, count: Int) -> Data {
        let data = value.serialize()
        if data.count >= count { return data.suffix(count) }
        return Data(repeating: 0, count: count - data.count) + data
    }

    static func reverse(_ data: Data) -> Data { Data(data.reversed()) }

    private static func aesKeyAndIV(authKey: Data, messageKey: Data, x: Int) -> (Data, Data) {
        let a = sha256(messageKey + authKey.subdata(in: x..<(x + 36)))
        let b = sha256(authKey.subdata(in: (x + 40)..<(x + 76)) + messageKey)
        let key = a.prefix(8) + b.subdata(in: 8..<24) + a.suffix(8)
        let iv = b.prefix(8) + a.subdata(in: 8..<24) + b.suffix(8)
        return (Data(key), Data(iv))
    }

    static func aesIGE(_ data: Data, key: Data, iv: Data, encrypt: Bool) throws -> Data {
        guard data.count % 16 == 0, key.count == 32, iv.count == 32 else { throw MTProtoCryptoError.aesFailure }
        var output = Data(capacity: data.count)
        var previousCipher = Data(iv.prefix(16))
        var previousPlain = Data(iv.suffix(16))
        for offset in stride(from: 0, to: data.count, by: 16) {
            let block = data.subdata(in: offset..<(offset + 16))
            let transformed: Data
            if encrypt {
                transformed = try aesECB(block ^ previousCipher, key: key, encrypt: true) ^ previousPlain
                previousPlain = block
                previousCipher = transformed
            } else {
                transformed = try aesECB(block ^ previousPlain, key: key, encrypt: false) ^ previousCipher
                previousCipher = block
                previousPlain = transformed
            }
            output.append(transformed)
        }
        return output
    }

    private static func aesECB(_ block: Data, key: Data, encrypt: Bool) throws -> Data {
        #if canImport(CommonCrypto)
        var output = Data(repeating: 0, count: block.count + kCCBlockSizeAES128)
        let outputSize = output.count
        var moved = 0
        let status = output.withUnsafeMutableBytes { outputBuffer in
            block.withUnsafeBytes { inputBuffer in
                key.withUnsafeBytes { keyBuffer in
                    CCCrypt(
                        encrypt ? CCOperation(kCCEncrypt) : CCOperation(kCCDecrypt),
                        CCAlgorithm(kCCAlgorithmAES),
                        CCOptions(kCCOptionECBMode),
                        keyBuffer.baseAddress,
                        key.count,
                        nil,
                        inputBuffer.baseAddress,
                        block.count,
                        outputBuffer.baseAddress,
                        outputSize,
                        &moved
                    )
                }
            }
        }
        guard status == kCCSuccess else { throw MTProtoCryptoError.aesFailure }
        output.removeSubrange(moved..<output.count)
        return output
        #else
        throw MTProtoCryptoError.aesFailure
        #endif
    }

    // Telegram's currently documented 2048-bit DH safe prime. Keeping the
    // accepted group explicit prevents a malicious server from downgrading
    // the handshake to an attacker-controlled group.
    private static let trustedDHPrime = decodeHex("""
        C71CAEB9C6B1C9048E6C522F70F13F73980D40238E3E21C14934D037563D930F
        48198A0AA7C14058229493D22530F4DBFA336F6E0AC925139543AED44CCE7C37
        20FD51F69458705AC68CD4FE6B6B13ABDC9746512969328454F18FAF8C595F64
        2477FE96BB2A941D5BCD1D4AC8CC49880708FA9B378E3C4F3A9060BEE67CF9A4
        A4A695811051907E162753B56B0F6B410DBA74D8A84B2A14B3144E0EF1284754
        FD17ED950D5965B4B9DD46582DB1178D169C6BC465B0D6FF9CA3928FEF5B9AE4
        E418FC15E83EBEA0F87FA9FF5EED70050DED2849F47BF959D956850CE92985
        1F0D8115F635B105EE2E4E15D04B2454BF6F4FADF034B10403119CD8E3B92FCC
        5B
    """)

    private static let rsaKeys: [MTProtoRSAPublicKey] = {
        let pemValues = [
            """
            MIIBCgKCAQEA6LszBcC1LGzyr992NzE0ieY+BSaOW622Aa9Bd4ZHLl+TuFQ4lo4g5nKaMBwK/BIb9xUfg0Q29/2mgIR6Zr9krM7HjuIcCzFvDtr+L0GQjae9H0pRB2OO62cECs5HKhT5DZ98K33vmWiLowc621dQuwKWSQKjWf50XYFw42h21P2KXUGyp2y/+aEyZ+uVgLLQbRA1dEjSDZ2iGRy12Mk5gpYc397aYp438fsJoHIgJ2lgMv5h7WY9t6N/byY9Nw9p21Og3AoXSL2q/2IJ1WRUhebgAdGVMlV1fkuOQoEzR7EdpqtQD9Cs5+bfo3Nhmcyvk5ftB0WkJ9z6bNZ7yxrP8wIDAQAB
            """,
            """
            MIIBCgKCAQEAruw2yP/BCcsJliRoW5eBVBVle9dtjJw+OYED160Wybum9SXtBBLXriwt4rROd9csv0t0OHCaTmRqBcQ0J8fxhN6/cpR1GWgOZRUAiQxoMnlt0R93LCX/j1dnVa/gVbCjdSxpbrfY2g2L4frzjJvdl84Kd9ORYjDEAyFnEA7dD556OptgLQQ2e2iVNq8NZLYTzLp5YpOdO1doK+ttrltggTCy5SrKeLoCPPbOgGsdxJxyz5KKcZnSLj16yE5HvJQn0CNpRdENvRUXe6tBP78O39oJ8BTHp9oIjd6XWXAsp2CvK45Ol8wFXGF710w9lwCGNbmNxNYhtIkdqfsEcwR5JwIDAQAB
            """,
            """
            MIIBCgKCAQEAvfLHfYH2r9R70w8prHblWt/nDkh+XkgpflqQVcnAfSuTtO05lNPspQmL8Y2XjVT4t8cT6xAkdgfmmvnvRPOOKPi0OfJXoRVylFzAQG/j83u5K3kRLbae7fLccVhKZhY46lvsueI1hQdLgNV9n1cQ3TDS2pQOCtovG4eDl9wacrXOJTG2990VjgnIKNA0UMoP+KF03qzryqIt3oTvZq03DyWdGK+AZjgBLaDKSnC6qD2cFY81UryRWOab8zKkWAnhw2kFpcqhI0jdV5QaSCExvnsjVaX0Y1N0870931/5Jb9ICe4nweZ9kSDF/gip3kWLG0o8XQpChDfyvsqB9OLV/wIDAQAB
            """,
            """
            MIIBCgKCAQEAs/ditzm+mPND6xkhzwFIz6J/968CtkcSE/7Z2qAJiXbmZ3UDJPGrzqTDHkO30R8VeRM/Kz2f4nR05GIFiITl4bEjvpy7xqRDspJcCFIOcyXm8abVDhF+th6knSU0yLtNKuQVP6voMrnt9MV1X92LGZQLgdHZbPQz0Z5qIpaKhdyA8DEvWWvSUwwc+yi1/gGaybwlzZwqXYoPOhwMebzKUk0xW14htcJrRrq+PXXQbRzTMynseCoPIoke0dtCodbA3qQxQovE16q9zz4Otv2k4j63cz53J+mhkVWAeWxVGI0lltJmWtEY K6er8VqqWot3nqmWMXogrgRLggv/NbbooQIDAQAB
            """,
            """
            MIIBCgKCAQEAvmpxVY7ld/8DAjz6F6q05shjg8/4p6047bn6/m8yPy1RBsvIyvuDuGnP/RzPEhzXQ9UJ5Ynmh2XJZgHoE9xbnfxL5BXHplJhMtADXKM9bWB11PU1Eioc3+AXBB8QiNFBn2XI5UkO5hPhbb9mJpjA9Uhw8EdfqJP8QetVsI/xrCEbwEXe0xvifRLJbY08/Gp66KpQvy7g8w7VB8wlgePexW3pT13Ap6vuC+mQuJPyiHvSxjEKHgqePji9NP3tJUFQjcECqcm0yV7/2d0t/pbCm+ZH1sadZspQCEPPrtbkQBlvHb4OLiIWPGHKSMeRFvp3IWcmdJqXahxLCUS1Eh6MAQIDAQAB
            """,
            """
            MIIBCgKCAQEAwVACPi9w23mF3tBkdZz+zwrzKOaaQdr01vAbU4E1pvkfj4sqDsm6lyDONS789sVoD/xCS9Y0hkkC3gtL1tSfTlgCMOOul9lcixlEKzwKENj1Yz/s7daSan9tqw3bfUV/nqgbhGX81v/+7RFAEd+RwFnK7a+XYl9sluzHRyVVaTTveB2GazTwEfzk2DWgkBluml8OREmvfraX3bkHZJTKX4EQSjBbbdJ2ZXIsRrYOXfaA+xayEGB+8hdlLmAjbCVfaigxX0CDqWeR1yFL9kwd9P0NsZRPsmoqVwMbMu7mStFai6aIhc3nSlv8kg9qv1m6XHVQY3PnEw+QQtqSIXklHwIDAQAB
            """,
            """
            MIIBCgKCAQEAxq7aeLAqJR20tkQQMfRn+ocfrtMlJsQ2Uksfs7Xcoo77jAid0bRtksiVmT2HEIJUlRxfABoPBV8wY9zRTUMaMA654pUX41mhyVN+XoerGxFvrs9dF1RuvCHbI02dM2ppPvyytvvMoefRoL5BTcpAihFgm5xCaakgsJ/tH5oVl74CdhQw8J5LxI/K++KJBUyZ26Uba1632cOiq05JBUW0Z2vWIOk4BLysk7+U9z+SxynKiZR3/xdiXvFKk01R3BHV+GUKM2RYazpS/P8v7eyKhAbKxOdRcFpHLlVwfjyM1VlDQrEZxsMpNTLYXb6Sce1Uov0YtNx5wEowlREH1WOTlwIDAQAB
            """,
            """
            MIIBCgKCAQEAsQZnSWVZNfClk29RcDTJQ76n8zZaiTGuUsi8sUhW8AS4PSbPKDm+DyJgdHDWdIF3HBzl7DHeFrILuqTs0vfS7Pa2NW8nUBwiaYQmPtwEa4n7bTmBVGsB1700/tz8wQWOLUlL2nMv+BPlDhxq4kmJCyJfgrIrHlX8sGPcPA4Y6Rwo0MSqYn3sg1Pu5gOKlaT9HKmE6wn5Sut6IiBjWozrRQ6n5h2RXNtO7O2qCDqjgB2vBxhV7B+z hRbLbCmW0tYMDsvPpX5M8fsO05svN+lKtCAuz1leFns8piZpptpSCFn7bWxiA9/fx5x17D7pfah3Sy2pA+NDXyzSlGcKdaUmwQIDAQAB
            """,
            """
            MIIBCgKCAQEAwqjFW0pi4reKGbkc9pK83Eunwj/k0G8ZTioMMPbZmW99GivMibwaxDM9RDWabEMyUtGoQC2ZcDeLWRK3W8jMP6dnEKAlvLkDLfC4fXYHzFO5KHEqF06iqAqBdmI1iBGdQv/OQCBcbXIWCGDY2AsiqLhlGQfPOI7/vvKc188rTriocgUtoTUc/n/sIUzkgwTqRyvWYynWARWzQg0I9olLBBC2q5RQJJlnYXZwyTL3y9tdb7zOHkksWV9IMQmZmyZh/N7sMbGWQpt4NMchGpPGeJ2e5gHBjDnlIf2p1yZOYeUYrdbwcS0tUiggS4UeE8TzIuXFQxw7fzEIlmhIaq3FnwIDAQAB
            """
        ]
        return pemValues.compactMap { pem in
            let clean = pem.replacingOccurrences(of: " ", with: "").replacingOccurrences(of: "\n", with: "")
            guard let der = Data(base64Encoded: clean), let components = parseRSAPublicKeyDER(der) else { return nil }
            return makeRSAPublicKey(modulus: components.modulus, exponent: components.exponent)
        }
    }()

    private static func makeRSAPublicKey(modulus: Data, exponent: Data) -> MTProtoRSAPublicKey {
        var fingerprintWriter = TLWriter()
        try? fingerprintWriter.writeBytes(modulus)
        try? fingerprintWriter.writeBytes(exponent)
        let digest = sha1(fingerprintWriter.data).suffix(8)
        var fingerprint: UInt64 = 0
        for (index, byte) in digest.enumerated() {
            fingerprint |= UInt64(byte) << UInt64(index * 8)
        }
        return MTProtoRSAPublicKey(modulus: modulus, exponent: exponent, fingerprint: fingerprint)
    }
}

private func parseRSADER(_ der: Data) -> (modulus: Data, exponent: Data)? {
    var reader = DERReader(data: der)
    guard reader.readTag() == 0x30, let _ = reader.readLength(), reader.readTag() == 0x02,
          let modulus = reader.readInteger(), reader.readTag() == 0x02, let exponent = reader.readInteger() else { return nil }
    return (modulus, exponent)
}

private func parseRSAPublicKeyDER(_ der: Data) -> (modulus: Data, exponent: Data)? {
    if let direct = parseRSADER(der) { return direct }

    // SubjectPublicKeyInfo ::= SEQUENCE {
    //   algorithm       SEQUENCE {...},
    //   subjectPublicKey BIT STRING  -- contains PKCS#1 DER
    // }
    var reader = DERReader(data: der)
    guard reader.readTag() == 0x30,
          let _ = reader.readLength(),
          reader.readTag() == 0x30,
          let algorithmLength = reader.readLength(),
          reader.skip(algorithmLength),
          reader.readTag() == 0x03,
          let bitStringLength = reader.readLength(),
          let bitString = reader.readBytes(bitStringLength),
          bitString.first == 0,
          bitString.count > 1 else { return nil }
    return parseRSADER(Data(bitString.dropFirst()))
}

private struct DERReader {
    let data: Data
    var offset = 0

    mutating func readTag() -> UInt8? {
        guard offset < data.count else { return nil }
        defer { offset += 1 }
        return data[offset]
    }

    mutating func readLength() -> Int? {
        guard offset < data.count else { return nil }
        let first = Int(data[offset]); offset += 1
        if first < 0x80 { return first }
        let count = first & 0x7f
        guard count > 0, count <= 4, offset + count <= data.count else { return nil }
        var value = 0
        for _ in 0..<count { value = (value << 8) | Int(data[offset]); offset += 1 }
        return value
    }

    mutating func readInteger() -> Data? {
        guard let length = readLength(), offset + length <= data.count else { return nil }
        var value = data.subdata(in: offset..<(offset + length)); offset += length
        while value.first == 0 { value.removeFirst() }
        return value
    }

    mutating func readBytes(_ count: Int) -> Data? {
        guard count >= 0, offset + count <= data.count else { return nil }
        let value = data.subdata(in: offset..<(offset + count))
        offset += count
        return value
    }

    mutating func skip(_ count: Int) -> Bool {
        guard count >= 0, offset + count <= data.count else { return false }
        offset += count
        return true
    }
}

private func ^ (lhs: Data, rhs: Data) -> Data {
    Data(zip(lhs, rhs).map { $0 ^ $1 })
}

private func decodeHex(_ value: String) -> Data {
    let characters = Array(value.filter { $0.isHexDigit })
    guard characters.count.isMultiple(of: 2) else { return Data() }
    var data = Data(capacity: characters.count / 2)
    for index in stride(from: 0, to: characters.count, by: 2) {
        let byte = UInt8(String(characters[index...index + 1]), radix: 16) ?? 0
        data.append(byte)
    }
    return data
}

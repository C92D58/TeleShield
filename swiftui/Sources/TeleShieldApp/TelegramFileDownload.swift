import Foundation

struct TelegramFileHash: Sendable, Equatable {
    let offset: Int64
    let limit: Int32
    let hash: Data
}

struct TelegramFileCDNRedirect: Sendable, Equatable {
    let dcID: Int
    let fileToken: Data
    let encryptionKey: Data
    let encryptionIV: Data
    let fileHashes: [TelegramFileHash]
}

enum TelegramFileResponse: Sendable, Equatable {
    case bytes(Data)
    case cdnRedirect(TelegramFileCDNRedirect)
}

enum TelegramCDNFileResponse: Sendable, Equatable {
    case bytes(Data)
    case reuploadNeeded(Data)
}

struct TelegramCDNEndpoint: Sendable, Equatable {
    let dcID: Int
    let host: String
    let port: UInt16
    let isIPv6: Bool
    let isCDN: Bool
}

struct TelegramCDNPublicKey: Sendable, Equatable {
    let dcID: Int
    let publicKey: String
}

enum TelegramFileDownloadError: LocalizedError, Equatable {
    case invalidResponse
    case endpointUnavailable(Int)
    case cdnKeyUnavailable(Int)
    case invalidCDNParameters
    case cdnIntegrityFailure(offset: Int64)
    case cdnTokenExpired
    case reuploadLimitExceeded

    var canFallbackToMaster: Bool {
        switch self {
        case .endpointUnavailable, .cdnKeyUnavailable, .cdnTokenExpired, .reuploadLimitExceeded:
            return true
        case .invalidResponse, .invalidCDNParameters, .cdnIntegrityFailure:
            return false
        }
    }

    var errorDescription: String? {
        switch self {
        case .invalidResponse:
            return "Telegram 檔案回應格式無效"
        case .endpointUnavailable(let dcID):
            return "找不到 Telegram CDN DC \(dcID) 的連線端點"
        case .cdnKeyUnavailable(let dcID):
            return "找不到 Telegram CDN DC \(dcID) 的受信任 RSA key"
        case .invalidCDNParameters:
            return "Telegram CDN 檔案加解密參數無效"
        case .cdnIntegrityFailure(let offset):
            return "Telegram CDN 檔案完整性驗證失敗（offset \(offset)）"
        case .cdnTokenExpired:
            return "Telegram CDN file token 已失效"
        case .reuploadLimitExceeded:
            return "Telegram CDN reupload 次數超過安全上限"
        }
    }
}

private extension TelegramAPI {
    enum FileConstructor {
        static let uploadGetFile: Int32 = Int32(bitPattern: 0xbe5335be)
        static let inputPhotoFileLocation: Int32 = Int32(bitPattern: 0x40181ffe)
        static let uploadFile: Int32 = Int32(bitPattern: 0x096a18d5)
        static let uploadFileCDNRedirect: Int32 = Int32(bitPattern: 0xf18cda44)
        static let uploadGetCDNFile: Int32 = Int32(bitPattern: 0x395f69da)
        static let uploadReuploadCDNFile: Int32 = Int32(bitPattern: 0x9b2754a8)
        static let uploadGetCDNFileHashes: Int32 = Int32(bitPattern: 0x91dc3f31)
        static let uploadCDNFile: Int32 = Int32(bitPattern: 0xa99fca4f)
        static let uploadCDNFileReuploadNeeded: Int32 = Int32(bitPattern: 0xeea8e46)
        static let fileHash: Int32 = Int32(bitPattern: 0xf39b035c)
        static let helpGetConfig: Int32 = Int32(bitPattern: 0xc4f9186b)
        static let config: Int32 = Int32(bitPattern: 0xcc1a241e)
        static let dcOption: Int32 = Int32(bitPattern: 0x18b7a10d)
        static let helpGetCDNConfig: Int32 = Int32(bitPattern: 0x52029342)
        static let cdnConfig: Int32 = Int32(bitPattern: 0x5725e40a)
        static let cdnPublicKey: Int32 = Int32(bitPattern: 0xc982eaba)
    }

    struct DownloadAccumulator {
        var data = Data()
        var offset: Int64 = 0
    }
}

extension TelegramAPI {
    /// Downloads a photo thumbnail through the master DC or Telegram's
    /// encrypted CDN path. The account client is restored to its original DC
    /// before this method returns, including when a file DC was selected by a
    /// FILE_MIGRATE response.
    func downloadPhoto(_ photo: NativePhotoReference, maximumBytes: Int = 4 * 1024 * 1024) async throws -> Data {
        guard maximumBytes > 0 else { return Data() }
        let originalDataCenter = await currentDataCenter()
        do {
            let result: Data
            do {
                result = try await downloadFromMaster(photo, maximumBytes: maximumBytes, cdnSupported: true)
            } catch let error as TelegramFileDownloadError where error.canFallbackToMaster {
                // A CDN endpoint/key can rotate, and CDN file tokens are
                // intentionally short-lived. The master path remains the
                // authoritative fallback and is requested without the CDN
                // capability bit so it returns upload.file directly.
                result = try await downloadFromMaster(photo, maximumBytes: maximumBytes, cdnSupported: false)
            }
            try await restoreDataCenter(originalDataCenter)
            return result
        } catch {
            // Preserve the operation error, but make a best-effort restore so
            // the next account request does not inherit a migrated file DC.
            if await currentDataCenter() != originalDataCenter {
                try? await restoreDataCenter(originalDataCenter)
            }
            throw error
        }
    }

    /// Offline-testable decoder for upload.getFile results.
    func parseUploadFileResponse(_ data: Data) throws -> TelegramFileResponse {
        var reader = TLReader(data)
        switch try reader.readInt32() {
        case FileConstructor.uploadFile:
            _ = try reader.readInt32() // upload.File.type
            _ = try reader.readInt32() // mtime
            let bytes = try reader.readBytes()
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .bytes(bytes)

        case FileConstructor.uploadFileCDNRedirect:
            let redirect = try readCDNRedirect(&reader)
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .cdnRedirect(redirect)

        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    /// Offline-testable decoder for upload.getCdnFile results.
    func parseCDNFileResponse(_ data: Data) throws -> TelegramCDNFileResponse {
        var reader = TLReader(data)
        let constructor = try reader.readInt32()
        switch constructor {
        case FileConstructor.uploadCDNFile:
            let bytes = try reader.readBytes()
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .bytes(bytes)
        case FileConstructor.uploadCDNFileReuploadNeeded:
            let requestToken = try reader.readBytes()
            guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
            return .reuploadNeeded(requestToken)
        default:
            throw TelegramAPIError.invalidResponse
        }
    }

    /// Offline-testable decoder for help.getCdnConfig results.
    func parseCDNConfigResponse(_ data: Data) throws -> [TelegramCDNPublicKey] {
        var reader = TLReader(data)
        guard try reader.readInt32() == FileConstructor.cdnConfig else {
            throw TelegramAPIError.invalidResponse
        }
        let keys = try reader.readVector { reader -> TelegramCDNPublicKey in
            guard try reader.readInt32() == FileConstructor.cdnPublicKey else {
                throw TelegramAPIError.invalidResponse
            }
            return TelegramCDNPublicKey(
                dcID: Int(try reader.readInt32()),
                publicKey: try reader.readString()
            )
        }
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return keys
    }

    /// Offline-testable decoder for help.getConfig's DC options. Parsing the
    /// complete current Layer 223 tail is intentional: accepting a shifted
    /// prefix would risk connecting to a malformed endpoint with stale bytes.
    func parseConfigCDNOptions(_ data: Data) throws -> [TelegramCDNEndpoint] {
        var reader = TLReader(data)
        guard try reader.readInt32() == FileConstructor.config else {
            throw TelegramAPIError.invalidResponse
        }
        let flags = try reader.readInt32()
        _ = try reader.readInt32() // date
        _ = try reader.readInt32() // expires
        _ = try reader.readBool() // test_mode
        _ = try reader.readInt32() // this_dc
        let options = try readDCOptions(&reader)

        _ = try reader.readString() // dc_txt_domain_name
        for _ in 0..<17 { _ = try reader.readInt32() }
        if flags & 1 != 0 { _ = try reader.readInt32() } // tmp_sessions
        _ = try reader.readInt32() // call_receive_timeout_ms
        _ = try reader.readInt32() // call_ring_timeout_ms
        _ = try reader.readInt32() // call_connect_timeout_ms
        _ = try reader.readInt32() // call_packet_timeout_ms
        _ = try reader.readString() // me_url_prefix
        if flags & (1 << 7) != 0 { _ = try reader.readString() }
        if flags & (1 << 9) != 0 { _ = try reader.readString() }
        if flags & (1 << 10) != 0 { _ = try reader.readString() }
        if flags & (1 << 11) != 0 { _ = try reader.readString() }
        if flags & (1 << 12) != 0 { _ = try reader.readString() }
        _ = try reader.readInt32() // caption_length_max
        _ = try reader.readInt32() // message_length_max
        _ = try reader.readInt32() // webfile_dc_id
        if flags & (1 << 2) != 0 {
            _ = try reader.readString()
            _ = try reader.readInt32()
            _ = try reader.readInt32()
        }
        if flags & (1 << 15) != 0 { try skipReactionForFiles(&reader) }
        if flags & (1 << 16) != 0 { _ = try reader.readString() }
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return options.filter { $0.dcID > 0 && !$0.host.isEmpty && $0.port > 0 }
    }

    private func downloadFromMaster(
        _ photo: NativePhotoReference,
        maximumBytes: Int,
        cdnSupported: Bool
    ) async throws -> Data {
        let chunkSize = 512 * 1024
        var accumulator = DownloadAccumulator()

        while accumulator.data.count < maximumBytes {
            let request = try makeUploadGetFileRequest(
                photo,
                flags: cdnSupported ? 2 : 0,
                offset: accumulator.offset,
                limit: Int32(chunkSize)
            )
            let response = try await call(request)
            switch try parseUploadFileResponse(response) {
            case .bytes(let bytes):
                guard bytes.count <= chunkSize else { throw TelegramFileDownloadError.invalidResponse }
                guard !bytes.isEmpty else { return accumulator.data }
                let remaining = maximumBytes - accumulator.data.count
                accumulator.data.append(bytes.prefix(remaining))
                accumulator.offset += Int64(bytes.count)
                if bytes.count < chunkSize || accumulator.data.count >= maximumBytes {
                    return accumulator.data
                }

            case .cdnRedirect(let redirect):
                guard cdnSupported else { throw TelegramFileDownloadError.invalidResponse }
                return try await downloadFromCDN(redirect, maximumBytes: maximumBytes)
            }
        }
        return accumulator.data
    }

    private func makeUploadGetFileRequest(
        _ photo: NativePhotoReference,
        flags: Int32,
        offset: Int64,
        limit: Int32
    ) throws -> Data {
        var request = TLWriter()
        request.writeInt32(FileConstructor.uploadGetFile)
        request.writeInt32(flags)
        request.writeInt32(FileConstructor.inputPhotoFileLocation)
        request.writeInt64(photo.id)
        request.writeInt64(photo.accessHash)
        try request.writeBytes(photo.fileReference)
        try request.writeString(photo.thumbSize)
        request.writeInt64(offset)
        request.writeInt32(limit)
        return request.data
    }

    private func readCDNRedirect(_ reader: inout TLReader) throws -> TelegramFileCDNRedirect {
        let dcID = Int(try reader.readInt32())
        let fileToken = try reader.readBytes()
        let encryptionKey = try reader.readBytes()
        let encryptionIV = try reader.readBytes()
        let fileHashes = try readFileHashVector(&reader)
        guard dcID > 0 else { throw TelegramFileDownloadError.invalidResponse }
        return TelegramFileCDNRedirect(
            dcID: dcID,
            fileToken: fileToken,
            encryptionKey: encryptionKey,
            encryptionIV: encryptionIV,
            fileHashes: fileHashes
        )
    }

    private func downloadFromCDN(
        _ redirect: TelegramFileCDNRedirect,
        maximumBytes: Int
    ) async throws -> Data {
        guard redirect.encryptionKey.count == 32,
              redirect.encryptionIV.count == 16,
              !redirect.fileToken.isEmpty else {
            throw TelegramFileDownloadError.invalidCDNParameters
        }

        let endpoint = try await fetchCDNEndpoint(dcID: redirect.dcID)
        let publicKeys = try await fetchCDNPublicKeys()
        guard let publicKey = publicKeys.first(where: { $0.dcID == redirect.dcID }),
              let rsaKey = MTProtoCrypto.rsaPublicKey(fromPEM: publicKey.publicKey) else {
            throw TelegramFileDownloadError.cdnKeyUnavailable(redirect.dcID)
        }

        let cdnClient = makeCDNClient(
            host: endpoint.host,
            port: endpoint.port,
            dcID: redirect.dcID,
            rsaKeys: [rsaKey]
        )
        var hashes = redirect.fileHashes
        var offset: Int64 = 0
        var result = Data()
        var reuploadCount = 0
        let chunkSize: Int32 = 1024 * 1024

        do {
            while result.count < maximumBytes {
                let request = try makeGetCDNFileRequest(
                    fileToken: redirect.fileToken,
                    offset: offset,
                    limit: chunkSize
                )
                let response: Data
                do {
                    response = try await cdnClient.call(request)
                } catch {
                    if isCDNTokenError(error) { throw TelegramFileDownloadError.cdnTokenExpired }
                    throw error
                }

                switch try parseCDNFileResponse(response) {
                case .reuploadNeeded(let requestToken):
                    guard reuploadCount < 2 else {
                        throw TelegramFileDownloadError.reuploadLimitExceeded
                    }
                    do {
                        hashes = try await reuploadCDNFile(
                            fileToken: redirect.fileToken,
                            requestToken: requestToken
                        )
                    } catch {
                        if isCDNTokenError(error) { throw TelegramFileDownloadError.cdnTokenExpired }
                        throw error
                    }
                    reuploadCount += 1

                case .bytes(let encryptedBytes):
                    guard encryptedBytes.count <= Int(chunkSize) else {
                        throw TelegramFileDownloadError.invalidResponse
                    }
                    guard !encryptedBytes.isEmpty else {
                        await cdnClient.disconnect()
                        return result
                    }
                    guard offset.isMultiple(of: 4096) else {
                        throw TelegramFileDownloadError.invalidCDNParameters
                    }
                    let fileHash = try await verifiedHash(
                        encryptedBytes,
                        offset: offset,
                        hashes: hashes,
                        fileToken: redirect.fileToken
                    )
                    hashes = mergeFileHash(fileHash, into: hashes)
                    let iv = try makeCDNIV(base: redirect.encryptionIV, offset: offset)
                    let decrypted = try MTProtoCrypto.aesCTR(
                        encryptedBytes,
                        key: redirect.encryptionKey,
                        iv: iv
                    )
                    let remaining = maximumBytes - result.count
                    result.append(decrypted.prefix(remaining))
                    offset += Int64(encryptedBytes.count)
                    if encryptedBytes.count < Int(chunkSize) || result.count >= maximumBytes {
                        await cdnClient.disconnect()
                        return result
                    }
                }
            }
            await cdnClient.disconnect()
            return result
        } catch {
            await cdnClient.disconnect()
            throw error
        }
    }

    private func makeGetCDNFileRequest(fileToken: Data, offset: Int64, limit: Int32) throws -> Data {
        var request = TLWriter()
        request.writeInt32(FileConstructor.uploadGetCDNFile)
        try request.writeBytes(fileToken)
        request.writeInt64(offset)
        request.writeInt32(limit)
        return request.data
    }

    private func reuploadCDNFile(fileToken: Data, requestToken: Data) async throws -> [TelegramFileHash] {
        var request = TLWriter()
        request.writeInt32(FileConstructor.uploadReuploadCDNFile)
        try request.writeBytes(fileToken)
        try request.writeBytes(requestToken)
        let response = try await call(request.data)
        var reader = TLReader(response)
        let hashes = try readFileHashVector(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return hashes
    }

    private func requestCDNFileHashes(fileToken: Data, offset: Int64) async throws -> [TelegramFileHash] {
        var request = TLWriter()
        request.writeInt32(FileConstructor.uploadGetCDNFileHashes)
        try request.writeBytes(fileToken)
        request.writeInt64(offset)
        let response = try await call(request.data)
        var reader = TLReader(response)
        let hashes = try readFileHashVector(&reader)
        guard reader.remaining == 0 else { throw TelegramAPIError.invalidResponse }
        return hashes
    }

    private func verifiedHash(
        _ encryptedBytes: Data,
        offset: Int64,
        hashes: [TelegramFileHash],
        fileToken: Data
    ) async throws -> TelegramFileHash {
        if let exact = hashes.first(where: { $0.offset == offset && $0.limit == encryptedBytes.count }),
           exact.hash.count == 32,
           MTProtoCrypto.sha256(encryptedBytes) == exact.hash {
            return exact
        }

        let refreshed: [TelegramFileHash]
        do {
            refreshed = try await requestCDNFileHashes(fileToken: fileToken, offset: offset)
        } catch {
            if isCDNTokenError(error) { throw TelegramFileDownloadError.cdnTokenExpired }
            throw error
        }
        guard let exact = refreshed.first(where: { $0.offset == offset && $0.limit == encryptedBytes.count }),
              exact.hash.count == 32,
              MTProtoCrypto.sha256(encryptedBytes) == exact.hash else {
            throw TelegramFileDownloadError.cdnIntegrityFailure(offset: offset)
        }
        return exact
    }

    private func mergeFileHash(_ hash: TelegramFileHash, into hashes: [TelegramFileHash]) -> [TelegramFileHash] {
        hashes.filter { !($0.offset == hash.offset && $0.limit == hash.limit) } + [hash]
    }

    private func makeCDNIV(base: Data, offset: Int64) throws -> Data {
        guard base.count == 16,
              offset >= 0,
              offset.isMultiple(of: 16),
              offset / 16 <= Int64(UInt32.max) else {
            throw TelegramFileDownloadError.invalidCDNParameters
        }
        var iv = base
        let counter = UInt32(offset / 16)
        iv.withUnsafeMutableBytes { buffer in
            let bytes = buffer.bindMemory(to: UInt8.self)
            bytes[12] = UInt8(truncatingIfNeeded: counter >> 24)
            bytes[13] = UInt8(truncatingIfNeeded: counter >> 16)
            bytes[14] = UInt8(truncatingIfNeeded: counter >> 8)
            bytes[15] = UInt8(truncatingIfNeeded: counter)
        }
        return iv
    }

    private func fetchCDNEndpoint(dcID: Int) async throws -> TelegramCDNEndpoint {
        var request = TLWriter()
        request.writeInt32(FileConstructor.helpGetConfig)
        let response = try await call(request.data)
        let endpoints = try parseConfigCDNOptions(response)
        guard let endpoint = endpoints.first(where: { $0.dcID == dcID && !$0.isIPv6 })
                ?? endpoints.first(where: { $0.dcID == dcID }) else {
            throw TelegramFileDownloadError.endpointUnavailable(dcID)
        }
        return endpoint
    }

    private func fetchCDNPublicKeys() async throws -> [TelegramCDNPublicKey] {
        var request = TLWriter()
        request.writeInt32(FileConstructor.helpGetCDNConfig)
        let response = try await call(request.data)
        return try parseCDNConfigResponse(response)
    }

    private func readFileHashVector(_ reader: inout TLReader) throws -> [TelegramFileHash] {
        try reader.readVector { reader in
            guard try reader.readInt32() == FileConstructor.fileHash else {
                throw TelegramAPIError.invalidResponse
            }
            let offset = try reader.readInt64()
            let limit = try reader.readInt32()
            let hash = try reader.readBytes()
            guard offset >= 0, limit > 0, hash.count == 32 else {
                throw TelegramFileDownloadError.invalidResponse
            }
            return TelegramFileHash(offset: offset, limit: limit, hash: hash)
        }
    }

    private func readDCOptions(_ reader: inout TLReader) throws -> [TelegramCDNEndpoint] {
        try reader.readVector { reader in
            guard try reader.readInt32() == FileConstructor.dcOption else {
                throw TelegramAPIError.invalidResponse
            }
            let flags = try reader.readInt32()
            let dcID = Int(try reader.readInt32())
            let host = try reader.readString()
            let rawPort = try reader.readInt32()
            if flags & (1 << 10) != 0 { _ = try reader.readBytes() }
            guard rawPort > 0, rawPort <= Int32(UInt16.max) else {
                throw TelegramFileDownloadError.invalidResponse
            }
            return TelegramCDNEndpoint(
                dcID: dcID,
                host: host,
                port: UInt16(rawPort),
                isIPv6: flags & 1 != 0,
                isCDN: flags & (1 << 3) != 0
            )
        }.filter(\.isCDN)
    }

    private func isCDNTokenError(_ error: Error) -> Bool {
        guard let protoError = error as? TelegramMTProtoError,
              case .rpc(_, let message) = protoError else { return false }
        let normalized = message.uppercased()
        return normalized.contains("FILE_TOKEN_INVALID") || normalized.contains("REQUEST_TOKEN_INVALID")
    }
}

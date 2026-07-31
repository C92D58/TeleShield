import Foundation
import zlib

enum GZipDecoderError: LocalizedError {
    case invalidStream
    case outputLimitExceeded

    var errorDescription: String? {
        switch self {
        case .invalidStream:
            return "Telegram gzip 壓縮資料無效"
        case .outputLimitExceeded:
            return "Telegram gzip 回應超過允許大小"
        }
    }
}

enum GZipDecoder {
    private static let chunkSize = 64 * 1024
    private static let maximumOutputSize = 16 * 1024 * 1024

    static func decompress(_ data: Data, maximumOutputSize: Int = maximumOutputSize) throws -> Data {
        guard !data.isEmpty, maximumOutputSize > 0 else {
            throw GZipDecoderError.invalidStream
        }

        var stream = z_stream()
        let initialization = inflateInit2_(&stream, 31, ZLIB_VERSION, Int32(MemoryLayout<z_stream>.size))
        guard initialization == Z_OK else { throw GZipDecoderError.invalidStream }
        defer { inflateEnd(&stream) }

        var output = Data()
        output.reserveCapacity(min(data.count * 2, maximumOutputSize))

        return try data.withUnsafeBytes { inputBuffer in
            guard let inputAddress = inputBuffer.bindMemory(to: Bytef.self).baseAddress else {
                throw GZipDecoderError.invalidStream
            }
            stream.next_in = UnsafeMutablePointer(mutating: inputAddress)
            stream.avail_in = uInt(data.count)

            while true {
                var chunk = [UInt8](repeating: 0, count: chunkSize)
                let capacity = chunk.count
                let status: Int32 = chunk.withUnsafeMutableBytes { outputBuffer in
                    guard let outputAddress = outputBuffer.bindMemory(to: Bytef.self).baseAddress else {
                        return Int32(Z_STREAM_ERROR)
                    }
                    stream.next_out = outputAddress
                    stream.avail_out = uInt(capacity)
                    return inflate(&stream, Z_NO_FLUSH)
                }

                let produced = capacity - Int(stream.avail_out)
                if produced > 0 {
                    guard produced <= maximumOutputSize,
                          output.count <= maximumOutputSize - produced else {
                        throw GZipDecoderError.outputLimitExceeded
                    }
                    output.append(contentsOf: chunk.prefix(produced))
                }

                if status == Z_STREAM_END { return output }
                guard status == Z_OK, stream.avail_in > 0 || produced > 0 else {
                    throw GZipDecoderError.invalidStream
                }
            }
        }
    }
}

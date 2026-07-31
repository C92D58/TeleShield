import Foundation

enum TLCodecError: LocalizedError, Equatable {
    case truncated
    case invalidLength
    case invalidConstructor(Int32)
    case invalidString
    case invalidVector

    var errorDescription: String? {
        switch self {
        case .truncated: return "Telegram TL 資料不完整"
        case .invalidLength: return "Telegram TL 長度欄位無效"
        case .invalidConstructor(let id): return "不支援的 Telegram TL constructor：\(id)"
        case .invalidString: return "Telegram TL 字串不是有效 UTF-8"
        case .invalidVector: return "Telegram TL vector 格式無效"
        }
    }
}

struct TLWriter: Sendable {
    private(set) var data = Data()

    mutating func writeInt32(_ value: Int32) {
        writeUInt32(UInt32(bitPattern: value))
    }

    mutating func writeUInt32(_ value: UInt32) {
        data.append(UInt8(truncatingIfNeeded: value))
        data.append(UInt8(truncatingIfNeeded: value >> 8))
        data.append(UInt8(truncatingIfNeeded: value >> 16))
        data.append(UInt8(truncatingIfNeeded: value >> 24))
    }

    mutating func writeInt64(_ value: Int64) {
        writeUInt64(UInt64(bitPattern: value))
    }

    mutating func writeUInt64(_ value: UInt64) {
        for offset in stride(from: 0, through: 56, by: 8) {
            data.append(UInt8(truncatingIfNeeded: value >> UInt64(offset)))
        }
    }

    mutating func writeInt128(_ value: Data) {
        data.append(contentsOf: fixed(value, count: 16))
    }

    mutating func writeInt256(_ value: Data) {
        data.append(contentsOf: fixed(value, count: 32))
    }

    mutating func writeBytes(_ value: Data) throws {
        guard value.count <= 0x00ff_ffff else { throw TLCodecError.invalidLength }
        writeTLLength(value.count)
        data.append(value)
        padToWord()
    }

    mutating func writeString(_ value: String) throws {
        try writeBytes(Data(value.utf8))
    }

    mutating func writeBool(_ value: Bool) {
        writeInt32(value ? TLConstructor.boolTrue : TLConstructor.boolFalse)
    }

    mutating func writeVector<T>(_ values: [T], _ writeElement: (inout TLWriter, T) -> Void) {
        writeInt32(TLConstructor.vector)
        writeInt32(Int32(values.count))
        for value in values {
            writeElement(&self, value)
        }
    }

    mutating func append(_ value: Data) {
        data.append(value)
    }

    mutating func padToWord() {
        while data.count % 4 != 0 { data.append(0) }
    }

    private mutating func writeTLLength(_ count: Int) {
        guard count >= 0 else { return }
        if count < 254 {
            data.append(UInt8(count))
        } else {
            data.append(254)
            data.append(UInt8(truncatingIfNeeded: count))
            data.append(UInt8(truncatingIfNeeded: count >> 8))
            data.append(UInt8(truncatingIfNeeded: count >> 16))
        }
    }
}

struct TLReader: Sendable {
    let data: Data
    private(set) var offset: Int = 0

    init(_ data: Data) { self.data = data }

    var remaining: Int { data.count - offset }

    mutating func readInt32() throws -> Int32 {
        Int32(bitPattern: try readUInt32())
    }

    mutating func readUInt32() throws -> UInt32 {
        guard remaining >= 4 else { throw TLCodecError.truncated }
        let value = UInt32(data[offset])
            | UInt32(data[offset + 1]) << 8
            | UInt32(data[offset + 2]) << 16
            | UInt32(data[offset + 3]) << 24
        offset += 4
        return value
    }

    mutating func readInt64() throws -> Int64 {
        Int64(bitPattern: try readUInt64())
    }

    mutating func readUInt64() throws -> UInt64 {
        guard remaining >= 8 else { throw TLCodecError.truncated }
        var value: UInt64 = 0
        for shift in stride(from: 0, through: 56, by: 8) {
            value |= UInt64(data[offset + shift / 8]) << UInt64(shift)
        }
        offset += 8
        return value
    }

    mutating func readInt128() throws -> Data {
        try readFixed(count: 16)
    }

    mutating func readInt256() throws -> Data {
        try readFixed(count: 32)
    }

    mutating func readBytes() throws -> Data {
        guard remaining >= 1 else { throw TLCodecError.truncated }
        let first = Int(data[offset])
        offset += 1
        let length: Int
        if first < 254 {
            length = first
        } else {
            guard remaining >= 3 else { throw TLCodecError.truncated }
            length = Int(data[offset]) | Int(data[offset + 1]) << 8 | Int(data[offset + 2]) << 16
            offset += 3
        }
        guard length >= 0, remaining >= length else { throw TLCodecError.truncated }
        let result = data.subdata(in: offset..<(offset + length))
        offset += length
        while offset % 4 != 0 {
            guard remaining >= 1 else { throw TLCodecError.truncated }
            offset += 1
        }
        return result
    }

    mutating func readString() throws -> String {
        guard let value = String(data: try readBytes(), encoding: .utf8) else {
            throw TLCodecError.invalidString
        }
        return value
    }

    mutating func readBool() throws -> Bool {
        let value = try readInt32()
        switch value {
        case TLConstructor.boolTrue: return true
        case TLConstructor.boolFalse: return false
        default: throw TLCodecError.invalidConstructor(value)
        }
    }

    mutating func readVector<T>(_ readElement: (inout TLReader) throws -> T) throws -> [T] {
        guard try readInt32() == TLConstructor.vector else { throw TLCodecError.invalidVector }
        let count = try readInt32()
        guard count >= 0, count <= 100_000 else { throw TLCodecError.invalidVector }
        return try (0..<Int(count)).map { _ in try readElement(&self) }
    }

    mutating func readFixed(count: Int) throws -> Data {
        guard count >= 0, remaining >= count else { throw TLCodecError.truncated }
        let value = data.subdata(in: offset..<(offset + count))
        offset += count
        return value
    }

    mutating func skip(_ count: Int) throws {
        _ = try readFixed(count: count)
    }
}

enum TLConstructor {
    static let vector: Int32 = 0x1cb5c415
    static let boolTrue: Int32 = -1720552011
    static let boolFalse: Int32 = -1132882121
    static let rpcResult: Int32 = -212046591
    static let rpcError: Int32 = 0x2144ca19
    static let msgContainer: Int32 = 0x73f1f8dc
    static let gzipPacked: Int32 = 0x3072cfa1
    static let updatesTooLong: Int32 = -484987010
    static let invokeWithLayer: Int32 = -627372787
    static let initConnection: Int32 = -1043505495
    static let invokeWithoutUpdates: Int32 = -1080796745
}

private func fixed(_ data: Data, count: Int) -> Data {
    if data.count == count { return data }
    if data.count > count { return data.suffix(count) }
    return Data(repeating: 0, count: count - data.count) + data
}

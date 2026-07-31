import Foundation
import Network

enum MTProtoTransportError: LocalizedError {
    case notConnected
    case connectionFailed(String)
    case connectionClosed
    case timeout
    case invalidFrame

    var errorDescription: String? {
        switch self {
        case .notConnected: return "Telegram 網路連線尚未建立"
        case .connectionFailed(let message): return "Telegram 網路連線失敗：\(message)"
        case .connectionClosed: return "Telegram 網路連線已關閉"
        case .timeout: return "Telegram 網路連線逾時"
        case .invalidFrame: return "Telegram abridged transport frame 無效"
        }
    }
}

/// The MTProto client only depends on this small transport boundary. Keeping
/// the protocol async makes the production Network.framework actor and a
/// deterministic test transport interchangeable without leaking sockets into
/// protocol tests.
protocol MTProtoTransporting: Sendable {
    func isReady() async -> Bool
    func connect() async throws
    func close() async
    func send(_ packet: Data) async throws
    func receive() async throws -> Data
}

private final class ContinuationOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var resumed = false

    func claim() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard !resumed else { return false }
        resumed = true
        return true
    }
}

private final class AtomicFlag: @unchecked Sendable {
    private let lock = NSLock()
    private var value = false

    func set() {
        lock.lock()
        value = true
        lock.unlock()
    }

    var isSet: Bool {
        lock.lock()
        defer { lock.unlock() }
        return value
    }
}

actor MTProtoTransport: MTProtoTransporting {
    private static let maximumPacketBytes = 16 * 1024 * 1024
    private static let receiveTimeout: Duration = .seconds(45)
    private let host: NWEndpoint.Host
    private let port: NWEndpoint.Port
    private let queue = DispatchQueue(label: "com.caryyu0306.TeleShield.mtproto")
    private var connection: NWConnection?
    private var receiveBuffer = Data()
    private var sentTransportHeader = false

    init(host: String, port: UInt16 = 443) {
        self.host = NWEndpoint.Host(host)
        self.port = NWEndpoint.Port(rawValue: port) ?? .https
    }

    func isReady() -> Bool {
        connection?.state == .ready
    }

    func connect() async throws {
        if let connection, connection.state == .ready { return }
        if let connection {
            connection.cancel()
            self.connection = nil
            receiveBuffer.removeAll(keepingCapacity: false)
            sentTransportHeader = false
        }
        let connection = NWConnection(host: host, port: port, using: .tcp)
        self.connection = connection
        let timeoutFlag = AtomicFlag()
        let timeoutTask = Task { [connection, timeoutFlag] in
            do {
                try await Task.sleep(for: .seconds(20))
            } catch {
                return
            }
            timeoutFlag.set()
            connection.cancel()
        }
        defer { timeoutTask.cancel() }
        do {
            try await withTaskCancellationHandler(operation: {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
                    let once = ContinuationOnce()
                    connection.stateUpdateHandler = { state in
                        switch state {
                        case .ready:
                            if once.claim() { continuation.resume() }
                        case .failed(let error):
                            if once.claim() {
                                continuation.resume(throwing: MTProtoTransportError.connectionFailed(error.localizedDescription))
                            }
                        case .cancelled:
                            if once.claim() { continuation.resume(throwing: MTProtoTransportError.connectionClosed) }
                        default:
                            break
                        }
                    }
                    connection.start(queue: queue)
                }
            }, onCancel: {
                connection.cancel()
            })
        } catch {
            connection.cancel()
            if Task.isCancelled { throw CancellationError() }
            if timeoutFlag.isSet { throw MTProtoTransportError.timeout }
            throw error
        }
    }

    func close() {
        connection?.cancel()
        connection = nil
        receiveBuffer.removeAll(keepingCapacity: false)
        sentTransportHeader = false
    }

    func send(_ packet: Data) async throws {
        let units = packet.count / 4
        guard packet.count % 4 == 0, units > 0, units <= 0x00ff_ffff else {
            throw MTProtoTransportError.invalidFrame
        }
        guard packet.count <= Self.maximumPacketBytes else { throw MTProtoTransportError.invalidFrame }
        guard let connection, connection.state == .ready else { throw MTProtoTransportError.notConnected }
        if !sentTransportHeader {
            try await sendRaw(Data([0xef]), connection: connection)
            sentTransportHeader = true
        }
        var frame = Data()
        if units < 0x7f {
            frame.append(UInt8(units))
        } else {
            frame.append(0x7f)
            frame.append(UInt8(truncatingIfNeeded: units))
            frame.append(UInt8(truncatingIfNeeded: units >> 8))
            frame.append(UInt8(truncatingIfNeeded: units >> 16))
        }
        try await sendRaw(frame + packet, connection: connection)
    }

    func receive() async throws -> Data {
        guard let connection, connection.state == .ready else { throw MTProtoTransportError.notConnected }
        return try await Self.withTimeout(Self.receiveTimeout) { [weak self] in
            guard let self else { throw MTProtoTransportError.connectionClosed }
            return try await self.readFrame()
        }
    }

    private nonisolated static func withTimeout<T: Sendable>(
        _ timeout: Duration,
        operation: @escaping @Sendable () async throws -> T
    ) async throws -> T {
        try await withThrowingTaskGroup(of: T.self) { group in
            group.addTask(operation: operation)
            group.addTask {
                try await Task.sleep(for: timeout)
                throw MTProtoTransportError.timeout
            }
            defer { group.cancelAll() }
            guard let result = try await group.next() else {
                throw MTProtoTransportError.connectionClosed
            }
            return result
        }
    }

    private func readFrame() async throws -> Data {
        let first = try await readExact(1)
        let firstValue = Int(first[first.startIndex])
        let units: Int
        if firstValue == 0x7f {
            let extended = try await readExact(3)
            units = Int(extended[0]) | Int(extended[1]) << 8 | Int(extended[2]) << 16
        } else {
            units = firstValue
        }
        guard units > 0, units <= Self.maximumPacketBytes / 4 else {
            throw MTProtoTransportError.invalidFrame
        }
        return try await readExact(units * 4)
    }

    private func sendRaw(_ data: Data, connection: NWConnection) async throws {
        try await withTaskCancellationHandler(operation: {
            do {
                try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
                    let once = ContinuationOnce()
                    guard !Task.isCancelled else {
                        continuation.resume(throwing: CancellationError())
                        return
                    }
                    connection.send(content: data, completion: .contentProcessed { error in
                        if let error {
                            if once.claim() {
                                continuation.resume(throwing: MTProtoTransportError.connectionFailed(error.localizedDescription))
                            }
                        } else {
                            if once.claim() { continuation.resume() }
                        }
                    })
                }
            } catch {
                if Task.isCancelled { throw CancellationError() }
                throw error
            }
        }, onCancel: {
            connection.cancel()
        })
    }

    private func readExact(_ count: Int) async throws -> Data {
        guard count >= 0 else { throw MTProtoTransportError.invalidFrame }
        while receiveBuffer.count < count {
            guard let connection else { throw MTProtoTransportError.notConnected }
            let chunk = try await withTaskCancellationHandler(operation: {
                do {
                    return try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Data, any Error>) in
                        let once = ContinuationOnce()
                        guard !Task.isCancelled else {
                            continuation.resume(throwing: CancellationError())
                            return
                        }
                        connection.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { data, _, isComplete, error in
                            if let error {
                                if once.claim() {
                                    continuation.resume(throwing: MTProtoTransportError.connectionFailed(error.localizedDescription))
                                }
                            } else if let data, !data.isEmpty {
                                if once.claim() { continuation.resume(returning: data) }
                            } else if isComplete {
                                if once.claim() { continuation.resume(throwing: MTProtoTransportError.connectionClosed) }
                            } else {
                                if once.claim() { continuation.resume(throwing: MTProtoTransportError.connectionClosed) }
                            }
                        }
                    }
                } catch {
                    if Task.isCancelled { throw CancellationError() }
                    throw error
                }
            }, onCancel: {
                connection.cancel()
            })
            receiveBuffer.append(chunk)
        }
        let result = receiveBuffer.prefix(count)
        receiveBuffer.removeFirst(count)
        return Data(result)
    }
}

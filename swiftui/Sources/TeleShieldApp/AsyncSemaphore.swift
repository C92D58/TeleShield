import Foundation

private final class WaiterState: @unchecked Sendable {
    private let lock = NSLock()
    private var status = Status.pending

    private enum Status: Equatable {
        case pending
        case completed
        case cancelled
    }

    let id = UUID()

    func cancel() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard status == .pending else { return false }
        status = .cancelled
        return true
    }

    func complete() -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard status == .pending else { return false }
        status = .completed
        return true
    }

    var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return status == .cancelled
    }
}

/// A cancellation-aware single-permit gate used to keep one MTProto request
/// and its response reader in flight at a time for each auth key.
actor AsyncSemaphore {
    private struct Waiter {
        let state: WaiterState
        let continuation: CheckedContinuation<Void, any Error>
    }

    private var permits: Int
    private var waiters: [Waiter] = []

    init(value: Int = 1) {
        precondition(value > 0)
        permits = value
    }

    func acquire() async throws {
        try Task.checkCancellation()
        if permits > 0 {
            permits -= 1
            return
        }

        let state = WaiterState()
        try await withTaskCancellationHandler(operation: {
            try await withCheckedThrowingContinuation { (continuation: CheckedContinuation<Void, any Error>) in
                guard !Task.isCancelled, !state.isCancelled else {
                    continuation.resume(throwing: CancellationError())
                    return
                }
                waiters.append(Waiter(state: state, continuation: continuation))
            }
        }, onCancel: {
            guard state.cancel() else { return }
            Task { await self.cancelWaiter(state.id) }
        })
    }

    func release() {
        while !waiters.isEmpty {
            let waiter = waiters.removeFirst()
            if waiter.state.complete() {
                waiter.continuation.resume()
                return
            }
            if waiter.state.isCancelled {
                waiter.continuation.resume(throwing: CancellationError())
            }
        }
        permits += 1
    }

    private func cancelWaiter(_ id: UUID) {
        guard let index = waiters.firstIndex(where: { $0.state.id == id }) else { return }
        let waiter = waiters.remove(at: index)
        if waiter.state.isCancelled {
            waiter.continuation.resume(throwing: CancellationError())
        } else if waiter.state.complete() {
            waiter.continuation.resume()
        }
    }
}

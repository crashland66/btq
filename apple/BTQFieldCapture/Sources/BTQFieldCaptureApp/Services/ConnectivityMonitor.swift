import Foundation
@preconcurrency import Network

public enum ConnectivityStatus: String, Equatable, Sendable {
    case satisfied
    case unsatisfied
    case requiresConnection
}

public protocol ConnectivityMonitoring: Sendable {
    func statusStream() -> AsyncStream<ConnectivityStatus>
}

public struct NWPathConnectivityMonitor: ConnectivityMonitoring, @unchecked Sendable {
    public init() {}

    public func statusStream() -> AsyncStream<ConnectivityStatus> {
        AsyncStream { continuation in
            let box = NWPathMonitorBox()
            box.monitor.pathUpdateHandler = { path in
                continuation.yield(ConnectivityStatus(path.status))
            }
            continuation.onTermination = { _ in
                box.cancel()
            }
            box.monitor.start(queue: DispatchQueue(label: "com.btq.fieldcapture.connectivity"))
        }
    }
}

private final class NWPathMonitorBox: @unchecked Sendable {
    let monitor = NWPathMonitor()

    func cancel() {
        monitor.cancel()
    }
}

private extension ConnectivityStatus {
    init(_ status: NWPath.Status) {
        switch status {
        case .satisfied:
            self = .satisfied
        case .requiresConnection:
            self = .requiresConnection
        case .unsatisfied:
            self = .unsatisfied
        @unknown default:
            self = .unsatisfied
        }
    }
}

import Foundation

public protocol BackgroundSyncScheduling: Sendable {
    func scheduleSyncIfNeeded(pendingCount: Int)
    @MainActor func beginExpiringSyncIfNeeded(
        pendingCount: Int,
        operation: @escaping @MainActor @Sendable () async -> Void
    )
}

public struct NoopBackgroundSyncScheduler: BackgroundSyncScheduling {
    public init() {}

    public func scheduleSyncIfNeeded(pendingCount: Int) {}

    @MainActor
    public func beginExpiringSyncIfNeeded(
        pendingCount: Int,
        operation: @escaping @MainActor @Sendable () async -> Void
    ) {}
}

#if os(iOS)
import BackgroundTasks
import UIKit

private final class BackgroundTaskCompletionGate: @unchecked Sendable {
    private let lock = NSLock()
    private let task: BGProcessingTask
    private var didComplete = false

    init(task: BGProcessingTask) {
        self.task = task
    }

    func complete(success: Bool) {
        let shouldComplete = lock.withLock {
            guard !didComplete else { return false }
            didComplete = true
            return true
        }
        if shouldComplete {
            task.setTaskCompleted(success: success)
        }
    }
}

public enum IOSBackgroundSyncTaskHandler {
    public static func register(operation: @escaping @MainActor @Sendable () async -> Void) {
        BGTaskScheduler.shared.register(
            forTaskWithIdentifier: BackgroundUploadSupport.sessionIdentifier,
            using: nil
        ) { task in
            guard let task = task as? BGProcessingTask else {
                task.setTaskCompleted(success: false)
                return
            }
            handle(task: task, operation: operation)
        }
    }

    private static func handle(
        task: BGProcessingTask,
        operation: @escaping @MainActor @Sendable () async -> Void
    ) {
        let completionGate = BackgroundTaskCompletionGate(task: task)
        Task { @MainActor in
            await operation()
            completionGate.complete(success: true)
        }
        task.expirationHandler = {
            // The serial URLSession transfer is deliberately independent of this finite BGTask.
            // Report expiration promptly without claiming that cancellation stops that transfer.
            completionGate.complete(success: false)
        }
    }
}

public struct IOSBackgroundSyncScheduler: BackgroundSyncScheduling {
    public init() {}

    public func scheduleSyncIfNeeded(pendingCount: Int) {
        guard pendingCount > 0 else { return }
        let request = BGProcessingTaskRequest(identifier: BackgroundUploadSupport.sessionIdentifier)
        request.requiresNetworkConnectivity = true
        request.requiresExternalPower = false
        request.earliestBeginDate = Date(timeIntervalSinceNow: 60)
        do {
            try BGTaskScheduler.shared.submit(request)
        } catch {
            // Background scheduling is opportunistic; foreground and reachability sync remain authoritative.
        }
    }

    @MainActor
    public func beginExpiringSyncIfNeeded(
        pendingCount: Int,
        operation: @escaping @MainActor @Sendable () async -> Void
    ) {
        guard pendingCount > 0 else { return }

        var backgroundTask: UIBackgroundTaskIdentifier = .invalid
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "BTQ Capture Sync") {
            // Expiration ends the finite UIKit assertion synchronously. It intentionally does
            // not pretend to cancel the independently owned background URLSession transfer.
            MainActor.assumeIsolated {
                if backgroundTask != .invalid {
                    UIApplication.shared.endBackgroundTask(backgroundTask)
                    backgroundTask = .invalid
                }
            }
        }

        Task { @MainActor in
            await operation()
            if backgroundTask != .invalid {
                UIApplication.shared.endBackgroundTask(backgroundTask)
                backgroundTask = .invalid
            }
        }
    }
}
#endif

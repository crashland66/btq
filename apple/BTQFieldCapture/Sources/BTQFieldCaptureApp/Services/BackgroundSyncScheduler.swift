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

@MainActor
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
        let syncTask = Task {
            await operation()
            task.setTaskCompleted(success: true)
        }
        task.expirationHandler = {
            syncTask.cancel()
            task.setTaskCompleted(success: false)
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
        var syncTask: Task<Void, Never>?
        backgroundTask = UIApplication.shared.beginBackgroundTask(withName: "BTQ Capture Sync") {
            syncTask?.cancel()
            Task { @MainActor in
                if backgroundTask != .invalid {
                    UIApplication.shared.endBackgroundTask(backgroundTask)
                    backgroundTask = .invalid
                }
            }
        }

        syncTask = Task { @MainActor in
            await operation()
            if backgroundTask != .invalid {
                UIApplication.shared.endBackgroundTask(backgroundTask)
                backgroundTask = .invalid
            }
        }
    }
}
#endif

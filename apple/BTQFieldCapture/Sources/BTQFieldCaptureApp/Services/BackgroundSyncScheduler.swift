import Foundation

public protocol BackgroundSyncScheduling: Sendable {
    func scheduleSyncIfNeeded(pendingCount: Int)
}

public struct NoopBackgroundSyncScheduler: BackgroundSyncScheduling {
    public init() {}

    public func scheduleSyncIfNeeded(pendingCount: Int) {}
}

#if os(iOS)
import BackgroundTasks

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
}
#endif

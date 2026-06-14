import Foundation
import UserNotifications

public enum NotificationPermissionStatus: String, Sendable, Equatable {
    case notDetermined
    case authorized
    case denied
    case provisional
    case ephemeral
    case unknown

    public var displayName: String {
        switch self {
        case .notDetermined:
            "Not requested"
        case .authorized:
            "Allowed"
        case .denied:
            "Denied"
        case .provisional:
            "Allowed quietly"
        case .ephemeral:
            "Allowed for now"
        case .unknown:
            "Unknown"
        }
    }

    var allowsScheduling: Bool {
        switch self {
        case .authorized, .provisional, .ephemeral:
            true
        case .notDetermined, .denied, .unknown:
            false
        }
    }
}

public protocol UploadNotificationScheduling: Sendable {
    func authorizationStatus() async -> NotificationPermissionStatus
    func requestAuthorization() async -> NotificationPermissionStatus
    func notifyTestAlert() async
    func notifyUploadFailed(capture: LocalCapture, reason: String) async
    func notifySyncRecovered(pendingCount: Int) async
}

public actor UploadNotificationScheduler: UploadNotificationScheduling {
    public init() {}

    public func authorizationStatus() async -> NotificationPermissionStatus {
        let settings = await UNUserNotificationCenter.current().notificationSettings()
        return NotificationPermissionStatus(settings.authorizationStatus)
    }

    public func requestAuthorization() async -> NotificationPermissionStatus {
        do {
            _ = try await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound])
            return await authorizationStatus()
        } catch {
            return await authorizationStatus()
        }
    }

    public func notifyTestAlert() async {
        let content = UNMutableNotificationContent()
        content.title = "BTQ test alert"
        content.body = "Sync alerts are ready on this device."
        content.sound = .default
        await schedule(content: content, identifier: "btq-test-alert-\(UUID().uuidString)")
    }

    public func notifyUploadFailed(capture: LocalCapture, reason: String) async {
        let content = UNMutableNotificationContent()
        content.title = "BTQ upload failed"
        content.body = "\(capture.siteLabel): \(reason)"
        content.sound = .default
        await schedule(content: content, identifier: "btq-upload-failed-\(capture.captureID)")
    }

    public func notifySyncRecovered(pendingCount: Int) async {
        guard pendingCount == 0 else { return }
        let content = UNMutableNotificationContent()
        content.title = "BTQ sync complete"
        content.body = "All pending captures have uploaded."
        content.sound = nil
        await schedule(content: content, identifier: "btq-sync-complete")
    }

    private func schedule(content: UNNotificationContent, identifier: String) async {
        let center = UNUserNotificationCenter.current()
        do {
            guard await authorizationStatus().allowsScheduling else { return }
            let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
            try await center.add(request)
        } catch {
            // Notification failures must never block capture or sync.
        }
    }
}

public actor NoopUploadNotificationScheduler: UploadNotificationScheduling {
    private var status: NotificationPermissionStatus

    public init(status: NotificationPermissionStatus = .notDetermined) {
        self.status = status
    }

    public func authorizationStatus() async -> NotificationPermissionStatus {
        status
    }

    public func requestAuthorization() async -> NotificationPermissionStatus {
        status = .authorized
        return status
    }

    public func notifyTestAlert() async {}
    public func notifyUploadFailed(capture: LocalCapture, reason: String) async {}
    public func notifySyncRecovered(pendingCount: Int) async {}
}

private extension NotificationPermissionStatus {
    init(_ status: UNAuthorizationStatus) {
        switch status {
        case .notDetermined:
            self = .notDetermined
        case .denied:
            self = .denied
        case .authorized:
            self = .authorized
        case .provisional:
            self = .provisional
        case .ephemeral:
            self = .ephemeral
        @unknown default:
            self = .unknown
        }
    }
}

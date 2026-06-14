import BTQFieldCaptureApp
import SwiftUI
import UserNotifications

private final class BTQForegroundNotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}

@main
struct BTQFieldCaptureiOSApp: App {
    @State private var model: FieldCaptureModel
    private let notificationPresenter = BTQForegroundNotificationPresenter()

    init() {
        let model = FieldCaptureModel()
        _model = State(initialValue: model)
        UNUserNotificationCenter.current().delegate = notificationPresenter
        IOSBackgroundSyncTaskHandler.register {
            await model.resumeOnlineWork()
        }
    }

    var body: some Scene {
        WindowGroup {
            BTQFieldCaptureRootView(
                model: model,
                backgroundSyncScheduler: IOSBackgroundSyncScheduler()
            )
        }
    }
}

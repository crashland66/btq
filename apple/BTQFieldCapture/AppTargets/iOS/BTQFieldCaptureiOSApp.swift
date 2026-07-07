import BTQFieldCaptureApp
import SwiftUI
import UIKit
import UserNotifications

private final class BTQForegroundNotificationPresenter: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .list, .sound]
    }
}

/// Carries the system's non-Sendable background completion handler across the delegate queue.
private final class UncheckedBox<T>: @unchecked Sendable {
    let value: T
    init(_ value: T) { self.value = value }
}

/// Receives the background-URLSession relaunch event so a capture upload can finish while the
/// app was suspended/terminated: the system hands us a completion handler to call once the
/// upload session has delivered all its pending events.
private final class BTQAppDelegate: NSObject, UIApplicationDelegate {
    func application(
        _ application: UIApplication,
        handleEventsForBackgroundURLSession identifier: String,
        completionHandler: @escaping () -> Void
    ) {
        guard identifier == BackgroundUploadSupport.sessionIdentifier else {
            completionHandler()
            return
        }
        let box = UncheckedBox(completionHandler)
        BackgroundUploader.shared.setBackgroundCompletionHandler {
            DispatchQueue.main.async { box.value() }
        }
    }
}

@main
struct BTQFieldCaptureiOSApp: App {
    @UIApplicationDelegateAdaptor(BTQAppDelegate.self) private var appDelegate
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

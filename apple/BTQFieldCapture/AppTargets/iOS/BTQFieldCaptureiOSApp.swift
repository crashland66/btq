import BTQFieldCaptureApp
import SwiftUI

@main
struct BTQFieldCaptureiOSApp: App {
    @State private var model: FieldCaptureModel

    init() {
        let model = FieldCaptureModel()
        _model = State(initialValue: model)
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

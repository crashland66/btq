import BTQFieldCaptureApp
import SwiftUI

@main
struct BTQFieldCaptureMacApp: App {
    var body: some Scene {
        WindowGroup {
            BTQFieldCaptureRootView()
        }
        .windowResizability(.contentSize)
    }
}

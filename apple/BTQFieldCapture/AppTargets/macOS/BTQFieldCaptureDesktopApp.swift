import BTQFieldCaptureApp
import SwiftUI

@main
struct BTQFieldCaptureDesktopApp: App {
    var body: some Scene {
        WindowGroup {
            BTQFieldCaptureRootView()
        }
        .windowResizability(.contentSize)
    }
}

import SwiftUI

public enum AppSection: String, CaseIterable, Identifiable {
    case capture
    case sites
    case queue
    case inbox
    case settings

    public var id: String { rawValue }

    var title: String {
        switch self {
        case .capture: "Capture"
        case .sites: "Sites"
        case .queue: "Queue"
        case .inbox: "Inbox"
        case .settings: "Settings"
        }
    }

    var systemImage: String {
        switch self {
        case .capture: "square.and.pencil"
        case .sites: "building.2"
        case .queue: "arrow.trianglehead.2.clockwise"
        case .inbox: "envelope"
        case .settings: "gearshape"
        }
    }

    static func visible(canReview: Bool) -> [AppSection] {
        allCases.filter { section in
            section != .inbox || canReview
        }
    }
}

public struct BTQFieldCaptureRootView: View {
    @State private var model: FieldCaptureModel
    @State private var connectivityMonitor: any ConnectivityMonitoring
    @AppStorage("btq.screenMode") private var screenModeRaw = ScreenMode.system.rawValue
    private let backgroundSyncScheduler: any BackgroundSyncScheduling
    @Environment(\.scenePhase) private var scenePhase

    public init(
        model: FieldCaptureModel = FieldCaptureModel(),
        connectivityMonitor: any ConnectivityMonitoring = NWPathConnectivityMonitor(),
        backgroundSyncScheduler: any BackgroundSyncScheduling = NoopBackgroundSyncScheduler()
    ) {
        _model = State(initialValue: model)
        _connectivityMonitor = State(initialValue: connectivityMonitor)
        self.backgroundSyncScheduler = backgroundSyncScheduler
    }

    public var body: some View {
        BTQFieldCaptureShell(model: model, screenMode: screenMode)
            .preferredColorScheme(ScreenMode.normalized(screenModeRaw).preferredColorScheme)
            .tint(.btqAccent)
            .task {
                await model.load()
                await model.resumeOnlineWork()
            }
            .task {
                for await status in connectivityMonitor.statusStream() {
                    await model.handleConnectivityChange(status)
                }
            }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .active:
                    Task { await model.resumeOnlineWork() }
                case .background:
                    let activeUploadCount = model.backgroundSyncWorkCount
                    backgroundSyncScheduler.scheduleSyncIfNeeded(pendingCount: activeUploadCount)
                    backgroundSyncScheduler.beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {
                        await model.syncPending()
                        await model.waitForPendingSyncQuiescence()
                    }
                case .inactive:
                    break
                @unknown default:
                    break
                }
            }
    }

    private var screenMode: Binding<ScreenMode> {
        Binding {
            ScreenMode.normalized(screenModeRaw)
        } set: { mode in
            screenModeRaw = mode.rawValue
        }
    }
}

struct BTQFieldCaptureShell: View {
    @Bindable var model: FieldCaptureModel
    @Binding var screenMode: ScreenMode
    @State private var section: AppSection = .capture

    var body: some View {
        shellContent
            .onOpenURL { url in
                Task { await handleOnboardingURL(url) }
            }
            .onChange(of: model.requiresReconnect) { _, requiresReconnect in
                if requiresReconnect {
                    section = .settings
                }
            }
            .onChange(of: model.canReviewInbox) { _, canReview in
                if !canReview && section == .inbox {
                    section = .capture
                }
            }
    }

    @ViewBuilder
    private var shellContent: some View {
        if model.needsInitialSetup {
            NavigationStack {
                InitialSetupView(model: model, onConnected: routeToCapture)
            }
        } else {
            mainShellContent
        }
    }

    @ViewBuilder
    private var mainShellContent: some View {
        #if os(macOS)
        NavigationSplitView {
            List(AppSection.visible(canReview: model.canReviewInbox), selection: $section) { item in
                Label(item.title, systemImage: item.systemImage)
                    .tag(item)
            }
            .navigationTitle("BTQ")
        } detail: {
            detail
        }
        #else
        TabView(selection: $section) {
            ForEach(AppSection.visible(canReview: model.canReviewInbox)) { item in
                NavigationStack {
                    detail(for: item)
                }
                .tabItem { Label(item.title, systemImage: item.systemImage) }
                .badge(item == .inbox ? model.inboxBadgeCount : 0)
                .tag(item)
            }
        }
        #endif
    }

    @ViewBuilder
    private var detail: some View {
        detail(for: section)
    }

    @ViewBuilder
    private func detail(for section: AppSection) -> some View {
        switch section {
        case .capture:
            CaptureNotebookView(model: model)
        case .sites:
            SitesView(model: model, onSiteSelected: routeToCapture)
        case .queue:
            QueueView(model: model)
        case .inbox:
            InboxView(model: model)
        case .settings:
            SettingsView(model: model, screenMode: $screenMode, onConnected: routeToCapture)
        }
    }

    private func handleOnboardingURL(_ url: URL) async {
        if await model.connectWithOnboardingURL(url) {
            routeToCapture()
        }
    }

    private func routeToCapture() {
        section = .capture
    }
}

import SwiftUI

public enum AppSection: String, CaseIterable, Identifiable {
    case capture
    case sites
    case queue
    case settings

    public var id: String { rawValue }

    var title: String {
        switch self {
        case .capture: "Capture"
        case .sites: "Sites"
        case .queue: "Queue"
        case .settings: "Settings"
        }
    }

    var systemImage: String {
        switch self {
        case .capture: "square.and.pencil"
        case .sites: "building.2"
        case .queue: "arrow.trianglehead.2.clockwise"
        case .settings: "gearshape"
        }
    }
}

public struct BTQFieldCaptureRootView: View {
    @State private var model: FieldCaptureModel
    @State private var connectivityMonitor: any ConnectivityMonitoring
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
        BTQFieldCaptureShell(model: model)
            .task {
                await model.load()
            }
            .task {
                for await status in connectivityMonitor.statusStream() {
                    await model.handleConnectivityChange(status)
                }
            }
            .onOpenURL { url in
                Task { await model.connectWithOnboardingURL(url) }
            }
            .onChange(of: scenePhase) { _, phase in
                switch phase {
                case .active:
                    Task { await model.resumeOnlineWork() }
                case .background:
                    backgroundSyncScheduler.scheduleSyncIfNeeded(pendingCount: model.queueSummary.pending)
                case .inactive:
                    break
                @unknown default:
                    break
                }
            }
    }
}

struct BTQFieldCaptureShell: View {
    @Bindable var model: FieldCaptureModel
    @State private var section: AppSection = .capture

    var body: some View {
        #if os(macOS)
        NavigationSplitView {
            List(AppSection.allCases, selection: $section) { item in
                Label(item.title, systemImage: item.systemImage)
                    .tag(item)
            }
            .navigationTitle("BTQ")
        } detail: {
            detail
        }
        #else
        TabView(selection: $section) {
            ForEach(AppSection.allCases) { item in
                NavigationStack {
                    detail(for: item)
                }
                .tabItem { Label(item.title, systemImage: item.systemImage) }
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
            SitesView(model: model)
        case .queue:
            QueueView(model: model)
        case .settings:
            SettingsView(model: model)
        }
    }
}

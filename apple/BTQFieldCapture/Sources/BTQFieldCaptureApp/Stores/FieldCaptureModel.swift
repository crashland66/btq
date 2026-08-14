import Foundation
import Observation

@MainActor
@Observable
public final class FieldCaptureModel {
    public nonisolated static let defaultMaxImagesPerCapture = 20

    public private(set) var account: BTQAccount
    public private(set) var session: BTQSession?
    public private(set) var sites: [BTQSite]
    public private(set) var visits: [Visit]
    public private(set) var captures: [LocalCapture]
    public private(set) var accountWorkspaces: [BTQAccountWorkspace]
    public var selectedSiteID: String?
    public var selectedCategoryValue: String?
    public var observationText: String = ""
    public var statusMessage: String = "Ready"
    public var isLoading: Bool = false
    public private(set) var hasLoaded: Bool = false
    public var isConnecting: Bool = false
    public var isSyncing: Bool = false
    public var isOfflineMode: Bool = true
    public private(set) var submittedCaptures: [SubmittedCapture] = []
    public private(set) var submissionQualitySummary: SubmissionQualitySummary?
    public private(set) var isRefreshingSubmittedHistory: Bool = false
    public private(set) var inboxItems: [InboxItem] = []
    public private(set) var isRefreshingInbox: Bool = false
    public private(set) var isReviewingInboxItem: Bool = false
    public private(set) var notificationPermissionStatus: NotificationPermissionStatus = .unknown
    public private(set) var requiresReconnect: Bool = false
    public private(set) var reconciliationFieldDiagnostics: [ReconciliationFieldDiagnosticRecord]
    public private(set) var isWritingPhoto = false

    private let store: any FieldCaptureStore
    private let apiClient: any CaptureAPIClient
    private let tokenStore: any SecureTokenStore
    private let notificationScheduler: any UploadNotificationScheduling
    private let mediaStore: LocalMediaStore
    private let audioUploadPreparer: any AudioMemoUploadPreparing
    private let reconciliationFieldDiagnosticRecorder: ReconciliationFieldDiagnosticRecorder
    private var pendingSyncTask: Task<Void, Never>?
    private var syncPendingRequested = false
    private var liveBackgroundUploadCaptureIDs: Set<String> = []
    private var activePhotoWriteCount = 0

    public init(
        store: any FieldCaptureStore = SQLiteFieldCaptureStore(),
        apiClient: any CaptureAPIClient = HTTPCaptureAPIClient(),
        tokenStore: any SecureTokenStore = KeychainTokenStore(),
        notificationScheduler: any UploadNotificationScheduling = UploadNotificationScheduler(),
        mediaStore: LocalMediaStore = LocalMediaStore(),
        audioUploadPreparer: (any AudioMemoUploadPreparing)? = nil
    ) {
        self.store = store
        self.apiClient = apiClient
        self.tokenStore = tokenStore
        self.notificationScheduler = notificationScheduler
        self.mediaStore = mediaStore
        self.audioUploadPreparer = audioUploadPreparer ?? AudioMemoUploadPreparer(mediaStore: mediaStore)
        let diagnosticRecorder = ReconciliationFieldDiagnosticRecorder()
        reconciliationFieldDiagnosticRecorder = diagnosticRecorder
        reconciliationFieldDiagnostics = diagnosticRecorder.load()
        account = .defaultProduction
        session = nil
        sites = []
        visits = []
        captures = []
        accountWorkspaces = [
            BTQAccountWorkspace(account: .defaultProduction)
        ]
    }

    public static func preview() -> FieldCaptureModel {
        FieldCaptureModel(
            store: MemoryFieldCaptureStore(),
            apiClient: MockCaptureAPIClient(),
            tokenStore: MemoryTokenStore(),
            notificationScheduler: NoopUploadNotificationScheduler()
        )
    }

    public var selectedSite: BTQSite? {
        get {
            guard let selectedSiteID else { return prioritizedSites.first }
            return sites.first { $0.siteID == selectedSiteID } ?? prioritizedSites.first
        }
        set {
            selectedSiteID = newValue?.siteID
            selectedCategoryValue = defaultCategoryValue(for: newValue)
        }
    }

    public var activeVisit: Visit? {
        visits.first { $0.endedAt == nil }
    }

    public func activeVisit(forSiteID siteID: String?) -> Visit? {
        guard let siteID else { return nil }
        return visits.first { $0.endedAt == nil && $0.siteID == siteID }
    }

    public var timeline: [VisitTimelineEntry] {
        var entries = captures.map { capture in
            VisitTimelineEntry(
                id: capture.captureID,
                title: capture.note.isEmpty ? capture.qcCategory : capture.note,
                subtitle: capture.siteLabel,
                date: capture.capturedAt,
                status: capture.status
            )
        }
        for visit in visits {
            entries.append(
                VisitTimelineEntry(
                    id: "\(visit.id.uuidString)-started",
                    title: "Visit started",
                    subtitle: visit.siteLabel,
                    date: visit.startedAt,
                    status: .draft
                )
            )
            if let endedAt = visit.endedAt {
                entries.append(
                    VisitTimelineEntry(
                        id: "\(visit.id.uuidString)-ended",
                        title: "Visit ended",
                        subtitle: visit.siteLabel,
                        date: endedAt,
                        status: .done
                    )
                )
            }
        }
        return entries.sorted { $0.date > $1.date }
    }

    public var queueSummary: QueueSummary {
        QueueSummary(captures: captures)
    }

    /// Includes the drain's final persistence window after its last capture leaves `.uploading`.
    /// Scene-phase handling uses this signal to keep its UIKit assertion through quiescence.
    public var backgroundSyncWorkCount: Int {
        max(queueSummary.pending + queueSummary.uploading, pendingSyncTask == nil ? 0 : 1)
    }

    public var activeAccountQueuedCaptureCount: Int {
        queuedCaptureCount(for: account.id)
    }

    public var maxImagesPerCapture: Int {
        Self.defaultMaxImagesPerCapture
    }

    public var photoLimitDescription: String {
        maxImagesPerCapture == 1 ? "1 photo" : "\(maxImagesPerCapture) photos"
    }

    public var canSubmitCaptures: Bool {
        guard !requiresReconnect else { return false }
        if session == nil && (account.personName != nil || !sites.isEmpty) {
            return false
        }
        return session?.canSubmit != false
    }

    public var canReviewInbox: Bool {
        guard !requiresReconnect else { return false }
        return session?.canReview == true
    }

    public var inboxBadgeCount: Int {
        canReviewInbox ? max(0, session?.inboxCount ?? inboxItems.count) : 0
    }

    public var inboxGroups: [InboxGroup] {
        var groups: [InboxGroup] = []
        var indexes: [String: Int] = [:]
        for (index, item) in inboxItems.enumerated() {
            let key = item.groupID.isEmpty ? item.draftID : item.groupID
            if let groupIndex = indexes[key] {
                groups[groupIndex].items.append(item)
            } else {
                indexes[key] = groups.count
                groups.append(InboxGroup(id: key.isEmpty ? "draft-\(index)" : key, items: [item]))
            }
        }
        return groups
    }

    public var needsInitialSetup: Bool {
        account.tokenID == nil || requiresReconnect
    }

    public var activeDraftCapture: LocalCapture? {
        let drafts = captures
            .filter { $0.status == .draft }
            .sorted { $0.exportedAt > $1.exportedAt }
        if let siteID = selectedSite?.siteID,
           let siteDraft = drafts.first(where: { $0.siteID == siteID }) {
            return siteDraft
        }
        return drafts.first
    }

    public var persistedCapturesOwningMedia: [LocalCapture] {
        captures + accountWorkspaces
            .filter { $0.account.id != account.id }
            .flatMap(\.captures)
    }

    public var accounts: [BTQAccount] {
        accountWorkspaces.map(\.account)
    }

    public func queuedCaptureCount(for accountID: UUID) -> Int {
        let workspace = accountID == account.id
            ? currentWorkspace()
            : accountWorkspaces.first { $0.account.id == accountID }
        return workspace?.captures.filter { $0.status != .done }.count ?? 0
    }

    public var prioritizedSites: [BTQSite] {
        sites.sorted {
            if $0.isFavorite != $1.isFavorite { return $0.isFavorite && !$1.isFavorite }
            switch ($0.lastUsedAt, $1.lastUsedAt) {
            case let (left?, right?) where left != right:
                return left > right
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            default:
                return $0.label.localizedCaseInsensitiveCompare($1.label) == .orderedAscending
            }
        }
    }

    public func load() async {
        guard !hasLoaded else { return }
        guard !isLoading else { return }
        isLoading = true
        defer {
            isLoading = false
            hasLoaded = true
        }
        await refreshNotificationPermissionStatus()
        do {
            let snapshot = try await store.load()
            apply(snapshot)
            await reconcileBackgroundUploads()
            let recoveredUploads = recoverInterruptedUploads()
            _ = await refreshSessionIfPossible()
            if sites.isEmpty {
                applyDemoSession()
                statusMessage = "Demo data loaded. Paste a token to connect BTQ."
            }
            if recoveredUploads {
                do {
                    try await persist()
                } catch {
                    statusMessage = "Could not save recovered uploads. Try again."
                }
            } else {
                try? await persist()
            }
        } catch {
            applyDemoSession()
            statusMessage = "Local store unavailable. Demo data loaded."
        }
    }

    public func refreshNotificationPermissionStatus() async {
        notificationPermissionStatus = await notificationScheduler.authorizationStatus()
    }

    public func requestNotificationPermission() async {
        notificationPermissionStatus = await notificationScheduler.requestAuthorization()
        switch notificationPermissionStatus {
        case .authorized, .provisional, .ephemeral:
            statusMessage = "Sync alerts enabled."
        case .denied:
            statusMessage = "Sync alerts are disabled in system Settings."
        case .notDetermined, .unknown:
            statusMessage = "Sync alerts are not enabled."
        }
    }

    public func sendTestNotification() async {
        await refreshNotificationPermissionStatus()
        guard notificationPermissionStatus.allowsScheduling else {
            statusMessage = "Enable sync alerts before sending a test."
            return
        }
        await notificationScheduler.notifyTestAlert()
        statusMessage = "Test sync alert sent."
    }

    public func sendTestUploadFailureNotification() async {
        await refreshNotificationPermissionStatus()
        guard notificationPermissionStatus.allowsScheduling else {
            statusMessage = "Enable sync alerts before sending a failure test."
            return
        }
        let site = selectedSite
        let siteID = site?.siteID ?? "test-site"
        let now = Date()
        let capture = LocalCapture(
            captureID: "test-upload-failure-\(UUID().uuidString)",
            jobID: "test-upload-failure",
            visitID: nil,
            siteID: siteID,
            siteLabel: site?.label ?? "Test Site",
            targetID: siteID,
            qcCategory: "notification_test",
            note: "Test upload failure alert",
            capturedAt: now,
            exportedAt: now
        )
        await notificationScheduler.notifyUploadFailed(
            capture: capture,
            reason: "Test upload failure alert from Settings."
        )
        statusMessage = "Test upload failure alert sent."
    }

    @discardableResult
    public func connectWithOnboardingURL(_ url: URL) async -> Bool {
        guard let token = OnboardingLinkParser.token(from: url) else {
            statusMessage = "No token found in link."
            return false
        }
        return await connect(token: token)
    }

    @discardableResult
    public func connect(token: String) async -> Bool {
        guard !isConnecting else { return false }
        guard account.baseURL.btqUsesHTTPS else {
            statusMessage = CaptureAPIError.insecureBaseURL.description
            return false
        }
        isConnecting = true
        defer { isConnecting = false }
        do {
            let liveSession = try await apiClient.session(baseURL: account.baseURL, token: token)
            let accountID = accountIDForConnectedSession(liveSession)
            let targetWorkspace = accountWorkspaces.first { $0.account.id == accountID }
            var updatedAccount = accountID == account.id
                ? account
                : targetWorkspace?.account ?? BTQAccount(id: accountID, label: liveSession.person.name, baseURL: account.baseURL)
            updatedAccount.tokenID = liveSession.token.tokenID
            updatedAccount.personID = liveSession.person.personID
            updatedAccount.personName = liveSession.person.name
            updatedAccount.tokenRole = liveSession.token.role
            try await tokenStore.saveToken(token, accountID: updatedAccount.id)
            upsertCurrentWorkspace()
            if accountID != account.id {
                if let targetWorkspace {
                    apply(targetWorkspace)
                } else {
                    session = nil
                    sites = []
                    visits = []
                    captures = []
                    selectedSiteID = nil
                    selectedCategoryValue = nil
                }
                observationText = ""
            }
            account = updatedAccount
            session = liveSession
            mergeSites(liveSession.sites)
            isOfflineMode = false
            requiresReconnect = false
            do {
                try await persist()
                statusMessage = "Ready for \(liveSession.person.name)"
            } catch {
                statusMessage = "Ready for \(liveSession.person.name). Local cache could not be saved."
            }
            return true
        } catch CaptureAPIError.unauthorized {
            statusMessage = "Token is invalid, expired, or revoked."
            return false
        } catch {
            statusMessage = "Could not verify token. Cached data remains available."
            return false
        }
    }

    @discardableResult
    public func refreshSessionIfPossible() async -> Bool {
        guard account.baseURL.btqUsesHTTPS else {
            statusMessage = CaptureAPIError.insecureBaseURL.description
            return false
        }
        guard let token = try? await tokenStore.loadToken(accountID: account.id), !token.isEmpty else {
            return true
        }
        do {
            let liveSession = try await apiClient.session(baseURL: account.baseURL, token: token)
            apply(liveSession)
            isOfflineMode = false
            requiresReconnect = false
            statusMessage = "Session refreshed"
            try await persist()
            return true
        } catch CaptureAPIError.unauthorized {
            await requireReconnect(message: "Token expired or revoked. Reconnect this account.")
            return false
        } catch {
            statusMessage = sites.isEmpty ? "Could not refresh session." : "Cached session remains available."
            return true
        }
    }

    public func resumeOnlineWork() async {
        await reconcileBackgroundUploads()
        if recoverInterruptedUploads() {
            do {
                try await persist()
            } catch {
                statusMessage = "Could not save recovered uploads. Try again."
                await waitForPendingSyncQuiescence()
                return
            }
        }
        guard await refreshSessionIfPossible() else {
            await waitForPendingSyncQuiescence()
            return
        }
        await syncPending()
        await waitForPendingSyncQuiescence()
    }

    public func refreshSubmittedHistory() async {
        guard !isRefreshingSubmittedHistory else { return }
        let requestedAccountID = account.id
        let requestedBaseURL = account.baseURL
        guard requestedBaseURL.btqUsesHTTPS else {
            statusMessage = CaptureAPIError.insecureBaseURL.description
            return
        }
        guard let token = try? await tokenStore.loadToken(accountID: requestedAccountID), !token.isEmpty else {
            statusMessage = "Connect a token to refresh submitted history."
            return
        }
        isRefreshingSubmittedHistory = true
        defer { isRefreshingSubmittedHistory = false }

        do {
            let response = try await apiClient.mySubmissions(baseURL: requestedBaseURL, token: token)
            guard account.id == requestedAccountID else { return }
            submittedCaptures = response.submissions
            submissionQualitySummary = response.qualitySummary
            statusMessage = response.submissions.isEmpty ? "No submitted captures yet." : "Submitted history refreshed."
        } catch CaptureAPIError.unauthorized {
            guard account.id == requestedAccountID else { return }
            await requireReconnect(message: "Token expired or revoked. Reconnect this account.")
        } catch {
            guard account.id == requestedAccountID else { return }
            statusMessage = "Could not refresh submitted history."
        }
    }

    public func mediaAuthorizationToken() async -> String? {
        guard let token = try? await tokenStore.loadToken(accountID: account.id), !token.isEmpty else {
            return nil
        }
        return token
    }

    public func remotePhotoPath(for capture: LocalCapture, filename: String) -> String {
        remoteMediaPath(for: capture, filename: filename)
    }

    public func refreshInbox() async {
        guard !isRefreshingInbox else { return }
        guard canReviewInbox else {
            inboxItems = []
            setInboxCount(0)
            statusMessage = "This account does not have approval access."
            return
        }
        let requestedAccountID = account.id
        let requestedBaseURL = account.baseURL
        guard requestedBaseURL.btqUsesHTTPS else {
            statusMessage = CaptureAPIError.insecureBaseURL.description
            return
        }
        guard let token = try? await tokenStore.loadToken(accountID: requestedAccountID), !token.isEmpty else {
            statusMessage = "Connect a token to review approvals."
            return
        }
        isRefreshingInbox = true
        defer { isRefreshingInbox = false }

        do {
            let response = try await apiClient.inbox(baseURL: requestedBaseURL, token: token)
            guard account.id == requestedAccountID else { return }
            inboxItems = response.items
            setInboxCount(response.count)
            statusMessage = response.items.isEmpty ? "Nothing waiting for approval." : "\(response.count) approval item\(response.count == 1 ? "" : "s") waiting."
        } catch CaptureAPIError.unauthorized {
            guard account.id == requestedAccountID else { return }
            await requireReconnect(message: "Token expired or revoked. Reconnect this account.")
        } catch CaptureAPIError.serverStatus(let status, _, _) where status == 403 {
            guard account.id == requestedAccountID else { return }
            inboxItems = []
            setInboxCount(0)
            statusMessage = "This account does not have approval access."
        } catch {
            guard account.id == requestedAccountID else { return }
            statusMessage = "Could not load approval inbox."
        }
    }

    public func reviewInboxItem(_ item: InboxItem, action: InboxDecisionAction) async {
        guard !isReviewingInboxItem else { return }
        guard canReviewInbox else {
            statusMessage = "This account does not have approval access."
            return
        }
        guard !isOfflineMode else {
            statusMessage = "Approval review requires a connection."
            return
        }
        let requestedAccountID = account.id
        let requestedBaseURL = account.baseURL
        guard let token = try? await tokenStore.loadToken(accountID: requestedAccountID), !token.isEmpty else {
            statusMessage = "Connect a token to review approvals."
            return
        }
        isReviewingInboxItem = true
        defer { isReviewingInboxItem = false }

        do {
            let response = try await apiClient.decideInboxItem(
                action: action,
                item: item,
                reason: action == .reject ? "" : nil,
                baseURL: requestedBaseURL,
                token: token
            )
            guard account.id == requestedAccountID else { return }
            if response.alreadyDecided {
                removeInboxItems(draftIDs: [item.draftID])
                statusMessage = "Already handled on another device."
                return
            }
            if let error = response.error, error != "already_decided" {
                statusMessage = response.message ?? "Could not review approval item."
                return
            }
            removeInboxItems(draftIDs: [item.draftID])
            statusMessage = action == .approve ? "Approved." : "Rejected."
        } catch CaptureAPIError.unauthorized {
            guard account.id == requestedAccountID else { return }
            await requireReconnect(message: "Token expired or revoked. Reconnect this account.")
        } catch {
            guard account.id == requestedAccountID else { return }
            statusMessage = "Action failed. Refreshing approval inbox."
            await refreshInbox()
        }
    }

    public func reviewInboxSet(_ group: InboxGroup, approvedDraftIDs: Set<String>) async {
        guard !isReviewingInboxItem else { return }
        guard canReviewInbox else {
            statusMessage = "This account does not have approval access."
            return
        }
        guard !isOfflineMode else {
            statusMessage = "Approval review requires a connection."
            return
        }
        let requestedAccountID = account.id
        let requestedBaseURL = account.baseURL
        guard let token = try? await tokenStore.loadToken(accountID: requestedAccountID), !token.isEmpty else {
            statusMessage = "Connect a token to review approvals."
            return
        }
        let decisions = group.items.map {
            InboxSetDecisionEntry(
                draftID: $0.draftID,
                revision: $0.revision,
                checked: approvedDraftIDs.contains($0.draftID)
            )
        }
        guard !decisions.isEmpty else { return }
        isReviewingInboxItem = true
        defer { isReviewingInboxItem = false }

        do {
            let response = try await apiClient.decideInboxSet(decisions, baseURL: requestedBaseURL, token: token)
            guard account.id == requestedAccountID else { return }
            let hasReviewFailure = response.results.contains { result in
                guard let error = result.error else { return false }
                return error != "already_decided"
            }
            if hasReviewFailure {
                statusMessage = "Set partly failed. Refreshing approval inbox."
                await refreshInbox()
                return
            }
            removeInboxItems(draftIDs: Set(group.items.map(\.draftID)))
            if response.alreadyDecided > 0 {
                statusMessage = "Set handled; \(response.alreadyDecided) already decided elsewhere."
            } else {
                statusMessage = "Set reviewed."
            }
        } catch CaptureAPIError.unauthorized {
            guard account.id == requestedAccountID else { return }
            await requireReconnect(message: "Token expired or revoked. Reconnect this account.")
        } catch {
            guard account.id == requestedAccountID else { return }
            statusMessage = "Action failed. Refreshing approval inbox."
            await refreshInbox()
        }
    }

    public func switchAccount(_ accountID: UUID) async {
        guard !isConnecting else {
            statusMessage = "Wait for connect to finish before switching accounts."
            return
        }
        guard !isSyncing else {
            statusMessage = "Wait for sync to finish before switching accounts."
            return
        }
        guard accountID != account.id,
              let workspace = accountWorkspaces.first(where: { $0.account.id == accountID }) else {
            return
        }
        upsertCurrentWorkspace()
        apply(workspace)
        observationText = ""
        statusMessage = "Switched to \(workspace.account.personName ?? workspace.account.label)"
        try? await persist()
    }

    public func removeAccount(_ accountID: UUID) async {
        guard !isConnecting else {
            statusMessage = "Wait for connect to finish before removing accounts."
            return
        }
        guard !isSyncing else {
            statusMessage = "Wait for sync to finish before removing accounts."
            return
        }
        let wasActive = account.id == accountID
        let removedWorkspace = wasActive
            ? currentWorkspace()
            : accountWorkspaces.first { $0.account.id == accountID }
        if !wasActive {
            upsertCurrentWorkspace()
        }
        let queuedCaptureCount = queuedCaptureCount(for: accountID)
        guard queuedCaptureCount == 0 else {
            let captureLabel = queuedCaptureCount == 1 ? "capture" : "captures"
            statusMessage = "Sync or delete \(queuedCaptureCount) queued \(captureLabel) before removing this account."
            return
        }
        if let removedWorkspace {
            // Account removal is blocked while any capture is not done, and done captures have
            // already released managed references. The empty owner set is therefore intentional.
            let noRetainedMediaOwnersAfterGuardedAccountRemoval: [LocalCapture] = []
            mediaStore.deleteMedia(
                for: removedWorkspace.captures,
                preservingMediaOwnedBy: noRetainedMediaOwnersAfterGuardedAccountRemoval
            )
        }
        try? await tokenStore.deleteToken(accountID: accountID)
        accountWorkspaces.removeAll { $0.account.id == accountID }

        if wasActive {
            if let nextWorkspace = accountWorkspaces.first {
                apply(nextWorkspace)
                observationText = ""
            } else {
                resetToEmptyDefaultAccount()
            }
        }

        statusMessage = wasActive ? "Account removed" : "Cached account removed"
        try? await persist()
    }

    public func startVisit(site: BTQSite) async {
        selectedSite = site
        var updatedSite = site
        updatedSite.lastUsedAt = .now
        replaceSite(updatedSite)
        visits.removeAll { $0.endedAt == nil }
        visits.append(Visit(siteID: site.siteID, siteLabel: site.label))
        statusMessage = "Visit started at \(site.label)"
        try? await persist()
    }

    public func endVisit() async {
        guard let index = visits.firstIndex(where: { $0.endedAt == nil }) else { return }
        visits[index].endedAt = .now
        statusMessage = "Visit ended"
        try? await persist()
    }

    public func endVisit(site: BTQSite) async {
        guard let index = visits.firstIndex(where: { $0.endedAt == nil && $0.siteID == site.siteID }) else {
            statusMessage = "No active visit at \(site.label)"
            return
        }
        visits[index].endedAt = .now
        statusMessage = "Visit ended at \(site.label)"
        try? await persist()
    }

    public func toggleFavorite(site: BTQSite) async {
        var updated = site
        updated.isFavorite.toggle()
        replaceSite(updated)
        try? await persist()
    }

    public func selectSite(id siteID: String?) {
        selectedSiteID = siteID
        selectedCategoryValue = defaultCategoryValue(for: selectedSite)
    }

    public func beginPhotoWrite() {
        activePhotoWriteCount += 1
        isWritingPhoto = true
    }

    public func endPhotoWrite() {
        activePhotoWriteCount = max(0, activePhotoWriteCount - 1)
        isWritingPhoto = activePhotoWriteCount > 0
    }

    /// Establishes durable ownership of a reserved photo URL without rewriting the full
    /// snapshot. The media writer may create the file only after this method succeeds.
    @discardableResult
    public func appendPhotoToDraft(
        _ photo: CapturePhoto,
        photos: [CapturePhoto],
        audios: [CaptureAudio] = []
    ) async -> Bool {
        guard photos.last?.id == photo.id,
              photos.filter({ $0.id == photo.id }).count == 1 else {
            statusMessage = "Could not save photo order locally."
            return false
        }
        guard let site = selectedSite else {
            statusMessage = "Choose a site before adding media."
            return false
        }
        guard canSubmitCaptures else {
            statusMessage = "This account cannot submit captures."
            return false
        }
        guard photos.count <= maxImagesPerCapture else {
            statusMessage = "Limit is \(photoLimitDescription) per capture."
            return false
        }

        let previousCaptures = captures
        let previousAccountWorkspaces = accountWorkspaces
        let note = observationText.trimmingCharacters(in: .whitespacesAndNewlines)
        let category = selectedCategoryValue ?? defaultCategoryValue(for: site) ?? "general_note"
        let now = Date()
        let draftIndex: Int
        if let index = captures.firstIndex(where: { $0.status == .draft && $0.siteID == site.siteID }) {
            captures[index].siteLabel = site.label
            captures[index].targetID = site.siteID
            captures[index].qcCategory = category
            captures[index].note = note
            captures[index].exportedAt = now
            captures[index].photos = photos
            captures[index].audios = audios
            captures[index].audio = audios.first
            draftIndex = index
        } else {
            let suffix = String(UUID().uuidString.prefix(8)).lowercased()
            let kind: CaptureAssetKind = audios.isEmpty ? .photo : .photoVoice
            captures.append(
                LocalCapture(
                    captureID: BTQFormatting.makeCaptureID(capturedAt: now, suffix: suffix),
                    jobID: BTQFormatting.makeJobID(exportedAt: now, assetKind: kind, siteLabel: site.label, suffix: suffix),
                    visitID: activeVisit(forSiteID: site.siteID)?.id,
                    siteID: site.siteID,
                    siteLabel: site.label,
                    targetID: site.siteID,
                    qcCategory: category,
                    note: note,
                    capturedAt: now,
                    exportedAt: now,
                    status: .draft,
                    photos: photos,
                    audios: audios
                )
            )
            draftIndex = captures.index(before: captures.endIndex)
        }

        upsertCurrentWorkspace()
        let snapshot = currentSnapshot()
        do {
            try await store.appendDraftPhoto(
                photo,
                to: captures[draftIndex],
                accountID: account.id,
                snapshot: snapshot
            )
            statusMessage = "Draft saved locally."
            return true
        } catch {
            captures = previousCaptures
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not save draft locally."
            return false
        }
    }

    @discardableResult
    public func upsertDraftCapture(photos: [CapturePhoto], audio: CaptureAudio? = nil, audios: [CaptureAudio] = []) async -> Bool {
        let audioAttachments = audios.isEmpty ? audio.map { [$0] } ?? [] : audios
        guard let site = selectedSite else {
            statusMessage = "Choose a site before adding media."
            return false
        }
        guard canSubmitCaptures else {
            statusMessage = "This account cannot submit captures."
            return false
        }
        guard photos.count <= maxImagesPerCapture else {
            statusMessage = "Limit is \(photoLimitDescription) per capture."
            return false
        }
        guard !photos.isEmpty || !audioAttachments.isEmpty else {
            return true
        }

        let previousCaptures = captures
        let note = observationText.trimmingCharacters(in: .whitespacesAndNewlines)
        let category = selectedCategoryValue ?? defaultCategoryValue(for: site) ?? "general_note"
        let now = Date()
        if let index = captures.firstIndex(where: { $0.status == .draft && $0.siteID == site.siteID }) {
            captures[index].siteLabel = site.label
            captures[index].targetID = site.siteID
            captures[index].qcCategory = category
            captures[index].note = note
            captures[index].exportedAt = now
            captures[index].photos = photos
            captures[index].audios = audioAttachments
            captures[index].audio = audioAttachments.first
        } else {
            let suffix = String(UUID().uuidString.prefix(8)).lowercased()
            let kind: CaptureAssetKind = !photos.isEmpty && !audioAttachments.isEmpty ? .photoVoice : !photos.isEmpty ? .photo : .voice
            captures.append(
                LocalCapture(
                    captureID: BTQFormatting.makeCaptureID(capturedAt: now, suffix: suffix),
                    jobID: BTQFormatting.makeJobID(exportedAt: now, assetKind: kind, siteLabel: site.label, suffix: suffix),
                    visitID: activeVisit(forSiteID: site.siteID)?.id,
                    siteID: site.siteID,
                    siteLabel: site.label,
                    targetID: site.siteID,
                    qcCategory: category,
                    note: note,
                    capturedAt: now,
                    exportedAt: now,
                    status: .draft,
                    photos: photos,
                    audios: audioAttachments
                )
            )
        }

        do {
            try await persist()
            statusMessage = "Draft saved locally."
            return true
        } catch {
            captures = previousCaptures
            statusMessage = "Could not save draft locally."
            return false
        }
    }

    /// Rolls back a pre-owned photo reference when its atomic file write fails. This rare
    /// recovery path may use the full snapshot; successful per-photo writes never do.
    @discardableResult
    public func removeUnwrittenDraftPhoto(_ photoID: UUID, siteID: String?) async -> Bool {
        guard let siteID,
              let index = captures.firstIndex(where: { $0.status == .draft && $0.siteID == siteID }),
              captures[index].photos.contains(where: { $0.id == photoID }) else {
            return true
        }
        let previousCaptures = captures
        let previousAccountWorkspaces = accountWorkspaces
        captures[index].photos.removeAll { $0.id == photoID }
        if captures[index].photos.isEmpty && captures[index].audioAttachments.isEmpty {
            captures.remove(at: index)
        }
        do {
            try await persist()
            return true
        } catch {
            captures = previousCaptures
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not remove an unwritten photo reference."
            return false
        }
    }

    @discardableResult
    public func removeDraftCapture(siteID: String? = nil) async -> Bool {
        let draftSiteID = siteID ?? selectedSite?.siteID
        guard let draftSiteID,
              let index = captures.firstIndex(where: { $0.status == .draft && $0.siteID == draftSiteID }) else {
            return true
        }
        let previousCaptures = captures
        let previousAccountWorkspaces = accountWorkspaces
        let draft = captures.remove(at: index)
        do {
            try await persist()
        } catch {
            captures = previousCaptures
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not remove the saved draft. Its media was kept."
            return false
        }
        mediaStore.deleteMedia(
            for: [draft],
            preservingMediaOwnedBy: persistedCapturesOwningMedia
        )
        return true
    }

    @discardableResult
    public func validateQuickObservationDraft(photoCount: Int = 0, hasAudio: Bool = false) -> Bool {
        guard selectedSite != nil else {
            statusMessage = "Choose a site before saving."
            return false
        }
        guard canSubmitCaptures else {
            statusMessage = "This account cannot submit captures."
            return false
        }
        let note = observationText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !note.isEmpty || photoCount > 0 || hasAudio else {
            statusMessage = "Add a note, photo, or voice memo."
            return false
        }
        guard photoCount <= maxImagesPerCapture else {
            let limit = maxImagesPerCapture == 1 ? "1 photo" : "\(maxImagesPerCapture) photos"
            statusMessage = "Limit is \(limit) per capture."
            return false
        }
        return true
    }

    @discardableResult
    public func saveQuickObservation(photos: [CapturePhoto] = [], audio: CaptureAudio? = nil, audios: [CaptureAudio] = []) async -> Bool {
        guard !isWritingPhoto else {
            statusMessage = "Wait for the photo to finish saving."
            return false
        }
        let audioAttachments = audios.isEmpty ? audio.map { [$0] } ?? [] : audios
        guard validateQuickObservationDraft(photoCount: photos.count, hasAudio: !audioAttachments.isEmpty) else {
            return false
        }
        guard let site = selectedSite else {
            statusMessage = "Choose a site before saving."
            return false
        }
        let category = selectedCategoryValue ?? defaultCategoryValue(for: site) ?? "general_note"
        let note = observationText.trimmingCharacters(in: .whitespacesAndNewlines)
        let kind: CaptureAssetKind = !photos.isEmpty && !audioAttachments.isEmpty ? .photoVoice : !photos.isEmpty ? .photo : !audioAttachments.isEmpty ? .voice : .text
        let visitID = activeVisit(forSiteID: site.siteID)?.id
        let previousCaptures = captures
        let previousSites = sites
        let previousAccountWorkspaces = accountWorkspaces
        let now = Date()
        if let index = captures.firstIndex(where: { $0.status == .draft && $0.siteID == site.siteID }) {
            let suffix = String(captures[index].captureID.suffix(8)).lowercased()
            captures[index].jobID = BTQFormatting.makeJobID(exportedAt: now, assetKind: kind, siteLabel: site.label, suffix: suffix)
            captures[index].visitID = visitID
            captures[index].siteLabel = site.label
            captures[index].targetID = site.siteID
            captures[index].qcCategory = category
            captures[index].note = note
            captures[index].exportedAt = now
            captures[index].status = .pending
            captures[index].attempts = 0
            captures[index].lastError = nil
            captures[index].lastTriedAt = nil
            captures[index].retryAfter = nil
            captures[index].photos = photos
            captures[index].audios = audioAttachments
            captures[index].audio = audioAttachments.first
        } else {
            let suffix = String(UUID().uuidString.prefix(8)).lowercased()
            captures.append(
                LocalCapture(
                    captureID: BTQFormatting.makeCaptureID(capturedAt: now, suffix: suffix),
                    jobID: BTQFormatting.makeJobID(exportedAt: now, assetKind: kind, siteLabel: site.label, suffix: suffix),
                    visitID: visitID,
                    siteID: site.siteID,
                    siteLabel: site.label,
                    targetID: site.siteID,
                    qcCategory: category,
                    note: note,
                    capturedAt: now,
                    exportedAt: now,
                    photos: photos,
                    audios: audioAttachments
                )
            )
        }
        var updatedSite = site
        updatedSite.lastUsedAt = now
        replaceSite(updatedSite)
        do {
            try await persist()
        } catch {
            captures = previousCaptures
            sites = previousSites
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not save locally. Try again."
            return false
        }
        observationText = ""
        statusMessage = "Saved locally"
        guard !isOfflineMode else {
            statusMessage = "Saved offline. Captures will sync when connection returns."
            return true
        }
        requestPendingSync()
        return true
    }

    public func syncPending() async {
        guard pendingSyncTask == nil else {
            syncPendingRequested = true
            return
        }
        let task = startPendingSyncDrain()
        await task.value
    }

    /// Observes the pending-sync queue until its active drain and any coalesced request finish.
    /// This method never starts a drain, and `saveQuickObservation` must never await it.
    public func waitForPendingSyncQuiescence() async {
        while let task = pendingSyncTask {
            await task.value
        }
    }

    private func drainPendingSync() async {
        isSyncing = true
        defer {
            syncPendingRequested = false
            isSyncing = false
            pendingSyncTask = nil
        }
        guard canSubmitCaptures else {
            statusMessage = requiresReconnect || (session == nil && (account.personName != nil || !sites.isEmpty))
                ? "Reconnect this account to sync captures."
                : "This account cannot submit captures."
            return
        }
        guard account.baseURL.btqUsesHTTPS else {
            statusMessage = CaptureAPIError.insecureBaseURL.description
            return
        }
        guard let token = try? await tokenStore.loadToken(accountID: account.id), !token.isEmpty else {
            statusMessage = "Saved offline. Connect a token to sync."
            return
        }

        var successfulUploadCount = 0
        var sentRecoveryNotification = false
        drainLoop: while true {
            syncPendingRequested = false
            var shouldStopDrain = false
            let readyCaptures = captures.filter { $0.status == .pending && isReadyToRetry($0) }

            for capture in readyCaptures {
                guard let index = captures.firstIndex(where: { $0.captureID == capture.captureID }) else { continue }
                let repairedCapture = mediaStore.repairManagedMediaURLs(for: captures[index])
                if repairedCapture != captures[index] {
                    captures[index] = repairedCapture
                }
                if let missingMedia = missingMediaDescription(for: captures[index]) {
                    captures[index].status = .failed
                    captures[index].lastError = missingMedia
                    captures[index].retryAfter = nil
                    statusMessage = "Capture failed: \(missingMedia)"
                    await notificationScheduler.notifyUploadFailed(capture: captures[index], reason: missingMedia)
                    continue
                }
                captures[index].status = .uploading
                captures[index].lastTriedAt = .now
                captures[index].retryAfter = nil
                let uploadingCapture = captures[index]
                do {
                    // Persist correlation state before creating the background task. If the
                    // process dies after this write, launch reconciliation can safely decide
                    // whether the task exists instead of treating a disk-level `.pending` row
                    // as new work while its first transfer is still live.
                    try await persist()
                } catch {
                    captures[index].status = .pending
                    captures[index].lastError = "Could not save upload state. It will retry automatically."
                    captures[index].retryAfter = nextRetryDate(attempts: captures[index].attempts)
                    statusMessage = "Could not save queue state. Try syncing again."
                    shouldStopDrain = true
                    break
                }
                var preparedCapture: LocalCapture?
                defer {
                    if let preparedCapture {
                        cleanupPreparedUploadMedia(preparedCapture, source: uploadingCapture)
                    }
                }
                do {
                    let captureForUpload = try await audioUploadPreparer.capturePreparedForUpload(uploadingCapture)
                    preparedCapture = captureForUpload
                    let submitResponse = try await apiClient.submit(capture: captureForUpload, baseURL: account.baseURL, token: token)
                    try validateSubmitResponse(submitResponse, for: captureForUpload)
                    guard let completedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    let mediaCapture = captures[completedIndex]
                    var confirmedCapture = captureWithRemotePhotoURLs(mediaCapture)
                    confirmedCapture = mediaStore.captureWithReleasedManagedMediaReferences(confirmedCapture)
                    confirmedCapture.status = .done
                    confirmedCapture.lastError = nil
                    confirmedCapture.retryAfter = nil
                    captures[completedIndex] = confirmedCapture
                    do {
                        // Persist confirmation before releasing the only managed evidence copy.
                        // The uploader's durable completion stays available if this write fails.
                        try await persist()
                    } catch {
                        captures[completedIndex] = mediaCapture
                        statusMessage = "Server confirmed the capture, but its queue state could not be saved."
                        shouldStopDrain = true
                        break
                    }
                    mediaStore.deleteMedia(
                        for: [mediaCapture],
                        preservingMediaOwnedBy: persistedCapturesOwningMedia
                    )
                    await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                    successfulUploadCount += 1
                    statusMessage = "Synced \(confirmedCapture.siteLabel)"
                } catch let error as AudioMemoUploadPreparationError {
                    guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                        statusMessage = "Could not prepare voice memos for upload."
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    let errorDescription = error.localizedDescription
                    captures[failedIndex].attempts += 1
                    captures[failedIndex].status = .failed
                    captures[failedIndex].lastError = errorDescription
                    captures[failedIndex].retryAfter = nil
                    statusMessage = "Capture failed: \(errorDescription)"
                    await notificationScheduler.notifyUploadFailed(capture: captures[failedIndex], reason: errorDescription)
                    await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                    continue
                } catch let error as CaptureAPIError {
                    guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                        statusMessage = "Sync paused. Will retry."
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    let errorDescription = userFacingCaptureError(error.description)
                    captures[failedIndex].attempts += 1
                    captures[failedIndex].lastError = errorDescription
                    if case .unauthorized = error {
                        captures[failedIndex].status = .pending
                        captures[failedIndex].retryAfter = nil
                        await requireReconnect(message: "Token expired or revoked. Reconnect this account to sync.")
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        shouldStopDrain = true
                        break
                    }
                    if error.isPermanent {
                        captures[failedIndex].status = .failed
                        captures[failedIndex].retryAfter = nil
                        statusMessage = "Capture failed: \(errorDescription)"
                        await notificationScheduler.notifyUploadFailed(capture: captures[failedIndex], reason: errorDescription)
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    captures[failedIndex].status = .pending
                    captures[failedIndex].retryAfter = nextRetryDate(attempts: captures[failedIndex].attempts)
                    statusMessage = "Sync paused. Will retry."
                    await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                    continue
                } catch let error as URLError where error.code == .cancelled {
                    guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                        statusMessage = "Upload interrupted before confirmation."
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    let errorDescription = "Upload interrupted before server confirmation. Local media is still saved; check for duplicates before retrying."
                    captures[failedIndex].attempts += 1
                    captures[failedIndex].status = .failed
                    captures[failedIndex].lastError = errorDescription
                    captures[failedIndex].retryAfter = nil
                    statusMessage = "Capture failed: \(errorDescription)"
                    await notificationScheduler.notifyUploadFailed(capture: captures[failedIndex], reason: errorDescription)
                    await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                    continue
                } catch {
                    guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                        statusMessage = "Offline. Capture will sync later."
                        await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                        continue
                    }
                    captures[failedIndex].attempts += 1
                    captures[failedIndex].status = .pending
                    captures[failedIndex].lastError = error.localizedDescription
                    captures[failedIndex].retryAfter = nextRetryDate(attempts: captures[failedIndex].attempts)
                    statusMessage = "Offline. Capture will sync later."
                    await apiClient.finishBackgroundUpload(captureID: uploadingCapture.captureID)
                    continue
                }
            }

            do {
                try await persist()
            } catch {
                statusMessage = "Could not save queue state. Try syncing again."
            }
            if !sentRecoveryNotification,
               successfulUploadCount > 0,
               queueSummary.pending == 0,
               queueSummary.failed == 0 {
                await notificationScheduler.notifySyncRecovered(pendingCount: 0)
                sentRecoveryNotification = true
            }
            if shouldStopDrain {
                break drainLoop
            }
            guard syncPendingRequested,
                  captures.contains(where: { $0.status == .pending && isReadyToRetry($0) }) else {
                break drainLoop
            }
        }
    }

    public func retryCapture(_ captureID: String) async {
        guard !isSyncing else {
            statusMessage = "Wait for sync to finish before retrying."
            return
        }
        guard let index = captures.firstIndex(where: { $0.captureID == captureID }),
              captures[index].status == .failed else {
            return
        }
        captures[index].status = .pending
        captures[index].lastError = nil
        captures[index].retryAfter = nil
        statusMessage = "Retry queued"
        try? await persist()
        await syncPending()
    }

    public func missingMediaRecoveryDescription(for capture: LocalCapture) -> String? {
        guard capture.status == .failed else { return nil }
        let recovery = repairedMissingMedia(for: capture)
        // Keep the existing recovery affordance for stale persisted URLs even when every file
        // is rediscovered. Confirming that case persists the repaired URLs without a loss note.
        guard !missingMedia(for: capture).isEmpty else { return nil }
        let missing = recovery.missing
        guard !missing.isEmpty else {
            return "All saved media is available. Confirming will repair this capture so it can be retried. No media-loss note will be added."
        }
        return "\(missing.summary) \(survivingMediaSummary(for: recovery.capture, excluding: missing)) will be uploaded. A permanent media-loss note will be added to this capture."
    }

    public func uploadSurvivingMedia(for captureID: String) async {
        guard !isSyncing else {
            statusMessage = "Wait for sync to finish before recovering this capture."
            return
        }
        guard let index = captures.firstIndex(where: { $0.captureID == captureID }),
              captures[index].status == .failed else {
            return
        }

        let previousCapture = captures[index]
        let previousAccountWorkspaces = accountWorkspaces
        let recovery = repairedMissingMedia(for: previousCapture)
        let repairedCapture = recovery.capture
        let missing = recovery.missing
        guard !missing.isEmpty else {
            captures[index] = repairedCapture
            statusMessage = "All saved media is available. Retry the capture instead."
            try? await persist()
            return
        }

        var recoveredCapture = repairedCapture
        recoveredCapture.photos.removeAll { missing.photoIDs.contains($0.id) }
        recoveredCapture.audios.removeAll { missing.audioIDs.contains($0.id) }
        recoveredCapture.audio = recoveredCapture.audios.first
        let survivingSummary = survivingMediaSummary(for: repairedCapture, excluding: missing)
        let lossRecord = "Media loss before upload: \(missing.inlineSummary). Operator confirmed upload of \(survivingSummary.lowercased())."
        recoveredCapture.note = recoveredCapture.note.trimmingCharacters(in: .whitespacesAndNewlines)
        if recoveredCapture.note.isEmpty {
            recoveredCapture.note = lossRecord
        } else {
            recoveredCapture.note += "\n\n\(lossRecord)"
        }
        recoveredCapture.status = .pending
        recoveredCapture.lastError = nil
        recoveredCapture.retryAfter = nil
        captures[index] = recoveredCapture

        do {
            try await persist()
        } catch {
            captures[index] = previousCapture
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not save media recovery. The surviving evidence was kept."
            return
        }

        statusMessage = "Recovery saved. Uploading surviving media with the loss recorded."
        await syncPending()
    }

    public func deleteCapture(_ captureID: String) async {
        guard let index = captures.firstIndex(where: { $0.captureID == captureID }) else {
            return
        }
        guard captures[index].status != .uploading else {
            statusMessage = "Capture is uploading and cannot be removed yet."
            return
        }
        let previousCaptures = captures
        let previousAccountWorkspaces = accountWorkspaces
        let capture = captures.remove(at: index)
        do {
            try await persist()
        } catch {
            captures = previousCaptures
            accountWorkspaces = previousAccountWorkspaces
            statusMessage = "Could not remove the capture. Its media was kept."
            return
        }
        mediaStore.deleteMedia(
            for: [capture],
            preservingMediaOwnedBy: persistedCapturesOwningMedia
        )
        statusMessage = "Capture removed"
    }

    public func displayError(for capture: LocalCapture) -> String? {
        guard let lastError = capture.lastError, !lastError.isEmpty else { return nil }
        return userFacingCaptureError(lastError)
    }

    public func handleConnectivityChange(_ status: ConnectivityStatus) async {
        switch status {
        case .satisfied:
            let wasOffline = isOfflineMode
            isOfflineMode = false
            if wasOffline {
                guard await refreshSessionIfPossible() else { return }
            }
            if queueSummary.pending > 0 {
                statusMessage = wasOffline ? "Back online. Syncing pending captures." : statusMessage
                await syncPending()
            }
        case .unsatisfied, .requiresConnection:
            isOfflineMode = true
            if queueSummary.pending > 0 {
                statusMessage = "Offline. Captures will sync when connection returns."
            }
        }
    }

    private func apply(_ snapshot: FieldCaptureSnapshot) {
        accountWorkspaces = snapshot.accountWorkspaces
        let activeID = snapshot.activeAccountID ?? snapshot.account.id
        if let workspace = accountWorkspaces.first(where: { $0.account.id == activeID }) {
            apply(workspace)
        } else if let first = accountWorkspaces.first {
            apply(first)
        } else {
            apply(
                BTQAccountWorkspace(
                    account: snapshot.account,
                    session: snapshot.session,
                    sites: snapshot.sites,
                    visits: snapshot.visits,
                    captures: snapshot.captures
                )
            )
        }
    }

    private func apply(_ workspace: BTQAccountWorkspace) {
        account = workspace.account
        session = workspace.session
        sites = workspace.sites
        visits = workspace.visits
        captures = workspace.captures
        submittedCaptures = []
        submissionQualitySummary = nil
        inboxItems = []
        requiresReconnect = false
        selectedSiteID = prioritizedSites.first?.siteID
        selectedCategoryValue = defaultCategoryValue(for: selectedSite)
    }

    private func applyDemoSession() {
        session = .demo
        sites = BTQSession.demo.sites
        selectedSiteID = sites.first?.siteID
        selectedCategoryValue = defaultCategoryValue(for: selectedSite)
        upsertCurrentWorkspace()
    }

    private func resetToEmptyDefaultAccount() {
        account = .defaultProduction
        session = nil
        sites = []
        visits = []
        captures = []
        submittedCaptures = []
        submissionQualitySummary = nil
        inboxItems = []
        requiresReconnect = false
        selectedSiteID = nil
        selectedCategoryValue = nil
        observationText = ""
        accountWorkspaces = [BTQAccountWorkspace(account: account)]
    }

    private func apply(_ liveSession: BTQSession) {
        var updatedAccount = account
        updatedAccount.tokenID = liveSession.token.tokenID
        updatedAccount.personID = liveSession.person.personID
        updatedAccount.personName = liveSession.person.name
        updatedAccount.tokenRole = liveSession.token.role
        account = updatedAccount
        session = liveSession
        requiresReconnect = false
        mergeSites(liveSession.sites)
    }

    private func accountIDForConnectedSession(_ liveSession: BTQSession) -> UUID {
        if account.tokenID == nil {
            return account.id
        }
        if account.tokenID == liveSession.token.tokenID {
            return account.id
        }
        if let existing = accountWorkspaces.first(where: { $0.account.tokenID == liveSession.token.tokenID }) {
            return existing.account.id
        }
        return UUID()
    }

    private func currentWorkspace() -> BTQAccountWorkspace {
        BTQAccountWorkspace(
            account: account,
            session: session,
            sites: sites,
            visits: visits,
            captures: captures
        )
    }

    private func upsertCurrentWorkspace() {
        let workspace = currentWorkspace()
        if let index = accountWorkspaces.firstIndex(where: { $0.account.id == workspace.account.id }) {
            accountWorkspaces[index] = workspace
        } else {
            accountWorkspaces.append(workspace)
        }
    }

    private func mergeSites(_ incoming: [BTQSite]) {
        let previousCategoryValue = selectedCategoryValue
        let existing = Dictionary(uniqueKeysWithValues: sites.map { ($0.siteID, $0) })
        sites = incoming.map { site in
            var merged = site
            merged.isFavorite = existing[site.siteID]?.isFavorite ?? site.isFavorite
            merged.lastUsedAt = existing[site.siteID]?.lastUsedAt
            return merged
        }
        if selectedSiteID == nil || !sites.contains(where: { $0.siteID == selectedSiteID }) {
            selectedSiteID = prioritizedSites.first?.siteID
        }
        if let previousCategoryValue,
           selectedSite?.displayCategories.contains(where: { $0.value == previousCategoryValue }) == true {
            selectedCategoryValue = previousCategoryValue
        } else {
            selectedCategoryValue = defaultCategoryValue(for: selectedSite)
        }
    }

    private func replaceSite(_ site: BTQSite) {
        if let index = sites.firstIndex(where: { $0.siteID == site.siteID }) {
            sites[index] = site
        } else {
            sites.append(site)
        }
    }

    /// Reconciles durable URL-session evidence before the age-based fallback can touch an
    /// uploading capture. A task's presence is the authoritative liveness signal; a stored
    /// successful delegate completion takes precedence over both liveness and retry heuristics.
    private func reconcileBackgroundUploads() async {
        // The active workspace is held in the top-level model properties between persistence
        // points. Fold it into the durable workspace set before reconciling every account.
        upsertCurrentWorkspace()
        let uploadingCaptureIDs = Set(
            accountWorkspaces.lazy
                .flatMap(\.captures)
                .filter { $0.status == .uploading }
                .map(\.captureID)
        )
        let reconciliation = await apiClient.reconcileBackgroundUploads(
            captureIDs: uploadingCaptureIDs
        )
        let confirmedCaptureIDs = Set(reconciliation.completedResponses.keys)
        let strandedCaptureIDs = reconciliation.isAuthoritative
            ? uploadingCaptureIDs
                .subtracting(reconciliation.liveCaptureIDs)
                .subtracting(confirmedCaptureIDs)
            : []
        reconciliationFieldDiagnostics = reconciliationFieldDiagnosticRecorder.record(
            ReconciliationFieldDiagnosticRecord(
                isAuthoritative: reconciliation.isAuthoritative,
                taskDescriptions: reconciliation.taskDescriptions,
                liveCaptureIDs: reconciliation.liveCaptureIDs,
                confirmedCaptureIDs: confirmedCaptureIDs,
                strandedCaptureIDs: strandedCaptureIDs
            )
        )
        guard reconciliation.isAuthoritative else {
            liveBackgroundUploadCaptureIDs = []
            return
        }

        // The drain may have changed the active capture array while the client was suspended.
        // Refresh that workspace, then reconcile only the captures included in the request.
        upsertCurrentWorkspace()
        let previousWorkspaces = accountWorkspaces
        let previousCaptures = captures
        var confirmedMedia: [LocalCapture] = []
        var consumedCompletionIDs: Set<String> = []
        var uploadFailures: [(LocalCapture, String)] = []
        var changedCaptureIDs: Set<String> = []
        var changed = false

        liveBackgroundUploadCaptureIDs = reconciliation.liveCaptureIDs
        for workspaceIndex in accountWorkspaces.indices {
            let isActiveWorkspace = accountWorkspaces[workspaceIndex].account.id == account.id
            for captureIndex in accountWorkspaces[workspaceIndex].captures.indices
                where accountWorkspaces[workspaceIndex].captures[captureIndex].status == .uploading
                    && uploadingCaptureIDs.contains(
                        accountWorkspaces[workspaceIndex].captures[captureIndex].captureID
                    ) {
                let captureID = accountWorkspaces[workspaceIndex].captures[captureIndex].captureID
                if let response = reconciliation.completedResponses[captureID] {
                    consumedCompletionIDs.insert(captureID)
                    do {
                        try validateSubmitResponse(
                            response,
                            for: accountWorkspaces[workspaceIndex].captures[captureIndex]
                        )
                        let mediaCapture = accountWorkspaces[workspaceIndex].captures[captureIndex]
                        var confirmed = captureWithRemotePhotoURLs(mediaCapture)
                        confirmed = mediaStore.captureWithReleasedManagedMediaReferences(confirmed)
                        confirmed.status = .done
                        confirmed.lastError = nil
                        confirmed.retryAfter = nil
                        accountWorkspaces[workspaceIndex].captures[captureIndex] = confirmed
                        confirmedMedia.append(mediaCapture)
                        if isActiveWorkspace {
                            statusMessage = "Synced \(confirmed.siteLabel)"
                        }
                    } catch {
                        let description = userFacingCaptureError(error.localizedDescription)
                        accountWorkspaces[workspaceIndex].captures[captureIndex].attempts += 1
                        accountWorkspaces[workspaceIndex].captures[captureIndex].status = .failed
                        accountWorkspaces[workspaceIndex].captures[captureIndex].lastError = description
                        accountWorkspaces[workspaceIndex].captures[captureIndex].retryAfter = nil
                        if isActiveWorkspace {
                            statusMessage = "Capture failed: \(description)"
                        }
                        uploadFailures.append((
                            accountWorkspaces[workspaceIndex].captures[captureIndex],
                            description
                        ))
                    }
                    changedCaptureIDs.insert(captureID)
                    changed = true
                } else if reconciliation.liveCaptureIDs.contains(captureID) {
                    continue
                } else {
                    accountWorkspaces[workspaceIndex].captures[captureIndex].status = .pending
                    accountWorkspaces[workspaceIndex].captures[captureIndex].lastError =
                        "Upload was interrupted. It will retry automatically."
                    accountWorkspaces[workspaceIndex].captures[captureIndex].retryAfter = .now
                    changedCaptureIDs.insert(captureID)
                    changed = true
                }
            }
        }

        if let activeWorkspace = accountWorkspaces.first(where: { $0.account.id == account.id }) {
            let reconciledCaptures = Dictionary(
                uniqueKeysWithValues: activeWorkspace.captures.lazy
                    .filter { changedCaptureIDs.contains($0.captureID) }
                    .map { ($0.captureID, $0) }
            )
            for captureIndex in captures.indices {
                if let reconciledCapture = reconciledCaptures[captures[captureIndex].captureID] {
                    captures[captureIndex] = reconciledCapture
                }
            }
        }

        if changed {
            do {
                try await persist()
            } catch {
                accountWorkspaces = previousWorkspaces
                captures = previousCaptures
                // A durable completion is also a liveness guard until it can be persisted into
                // the capture store; do not let the stale fallback submit it again meanwhile.
                liveBackgroundUploadCaptureIDs.formUnion(consumedCompletionIDs)
                statusMessage = "Could not save reconciled uploads. Try again."
                await apiClient.sweepUploadBodies(
                    preservingCaptureIDs: liveBackgroundUploadCaptureIDs
                )
                return
            }
        }

        for mediaCapture in confirmedMedia {
            mediaStore.deleteMedia(
                for: [mediaCapture],
                preservingMediaOwnedBy: persistedCapturesOwningMedia
            )
        }
        for captureID in consumedCompletionIDs {
            await apiClient.finishBackgroundUpload(captureID: captureID)
        }
        for (capture, description) in uploadFailures {
            await notificationScheduler.notifyUploadFailed(capture: capture, reason: description)
        }
        await apiClient.sweepUploadBodies(
            preservingCaptureIDs: liveBackgroundUploadCaptureIDs
        )
    }

    @discardableResult
    private func recoverInterruptedUploads(now: Date = .now) -> Bool {
        let staleThreshold: TimeInterval = 120
        var recoveredUploads = false
        for index in captures.indices where captures[index].status == .uploading
            && !liveBackgroundUploadCaptureIDs.contains(captures[index].captureID) {
            let lastTriedAt = captures[index].lastTriedAt ?? captures[index].capturedAt
            if now.timeIntervalSince(lastTriedAt) > staleThreshold {
                captures[index].status = .pending
                captures[index].lastError = "Upload was interrupted. It will retry automatically."
                captures[index].retryAfter = now
                recoveredUploads = true
            }
        }
        return recoveredUploads
    }

    private func isReadyToRetry(_ capture: LocalCapture, now: Date = .now) -> Bool {
        guard let retryAfter = capture.retryAfter else { return true }
        return retryAfter <= now
    }

    private func missingMediaDescription(for capture: LocalCapture) -> String? {
        let missing = missingMedia(for: capture)
        if let photo = missing.photos.first {
            return "Missing photo file: \(photo.filename)"
        }
        if let audio = missing.audios.first {
            return "Missing audio file: \(audio.filename)"
        }
        return nil
    }

    private func missingMedia(for capture: LocalCapture) -> MissingCaptureMedia {
        MissingCaptureMedia(
            photos: capture.photos.filter { mediaFileExists($0.fileURL) == false },
            audios: capture.audioAttachments.filter { mediaFileExists($0.fileURL) == false }
        )
    }

    private func repairedMissingMedia(
        for capture: LocalCapture
    ) -> (capture: LocalCapture, missing: MissingCaptureMedia) {
        let repairedCapture = mediaStore.repairManagedMediaURLs(for: capture)
        return (repairedCapture, missingMedia(for: repairedCapture))
    }

    private func mediaFileExists(_ fileURL: URL?) -> Bool {
        guard let fileURL else { return false }
        return FileManager.default.fileExists(atPath: fileURL.path)
    }

    private func survivingMediaSummary(
        for capture: LocalCapture,
        excluding missing: MissingCaptureMedia
    ) -> String {
        let photoCount = capture.photos.count - missing.photos.count
        let audioCount = capture.audioAttachments.count - missing.audios.count
        let photos = "\(photoCount) surviving photo\(photoCount == 1 ? "" : "s")"
        let audios = "\(audioCount) surviving voice memo\(audioCount == 1 ? "" : "s")"
        return "\(photos) and \(audios)"
    }

    private func validateSubmitResponse(_ response: SubmitCaptureResponse, for capture: LocalCapture) throws {
        let expectedPhotos = capture.photos.count
        let expectedAudio = capture.audioAttachments.count
        guard response.photoCount == expectedPhotos else {
            throw CaptureAPIError.serverStatus(
                status: 409,
                code: "photo_count_mismatch",
                message: "Server accepted \(response.photoCount) of \(expectedPhotos) photos. Retry before deleting local media."
            )
        }
        guard response.audioCount == expectedAudio else {
            throw CaptureAPIError.serverStatus(
                status: 409,
                code: "audio_count_mismatch",
                message: "Server accepted \(response.audioCount) of \(expectedAudio) voice memos. Retry before deleting local media."
            )
        }
    }

    private func defaultCategoryValue(for site: BTQSite?) -> String? {
        site?.displayCategories.first { category in
            category.value.compare("qc", options: [.caseInsensitive, .diacriticInsensitive]) == .orderedSame
        }?.value
    }

    private func nextRetryDate(attempts: Int, now: Date = .now) -> Date {
        let schedule: [TimeInterval] = [5, 15, 30, 60, 120]
        let delay = schedule[max(0, min(attempts - 1, schedule.count - 1))]
        return now.addingTimeInterval(delay)
    }

    private func userFacingCaptureError(_ error: String) -> String {
        if isPhotoLimitError(error) {
            return "Limit is \(photoLimitDescription) per capture."
        }
        return error
    }

    private func isPhotoLimitError(_ error: String) -> Bool {
        let normalized = error.localizedLowercase
        return normalized.contains("too many photos")
            || normalized.contains("too many images")
            || (normalized.contains("at most") && (normalized.contains("photos") || normalized.contains("images")))
            || normalized.contains("max images")
            || normalized.contains("maximum image")
            || normalized.contains("maximum photo")
            || normalized.contains("photo limit")
            || normalized.contains("image limit")
    }

    private func cleanupPreparedUploadMedia(_ prepared: LocalCapture, source: LocalCapture) {
        let sourceAudioURLs = Set(source.audioAttachments.compactMap(\.fileURL))
        for audio in prepared.audioAttachments {
            guard let fileURL = audio.fileURL, !sourceAudioURLs.contains(fileURL) else { continue }
            mediaStore.deletePendingMedia(
                photos: [],
                audio: audio,
                preservingMediaOwnedBy: persistedCapturesOwningMedia
            )
        }
    }

    private func captureWithRemotePhotoURLs(_ capture: LocalCapture) -> LocalCapture {
        var updated = capture
        updated.photos = capture.photos.map { photo in
            var updatedPhoto = photo
            if updatedPhoto.remoteURL == nil {
                updatedPhoto.remoteURL = remoteMediaPath(for: capture, filename: photo.filename)
            }
            return updatedPhoto
        }
        return updated
    }

    private func remoteMediaPath(for capture: LocalCapture, filename: String) -> String {
        let date = Self.remoteMediaDateString(from: capture.capturedAt)
        return "/media/\(Self.escapeMediaPathComponent(date))/\(Self.escapeMediaPathComponent(capture.captureID))/\(Self.escapeMediaPathComponent(filename))"
    }

    private nonisolated static func remoteMediaDateString(from date: Date) -> String {
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(secondsFromGMT: 0) ?? .gmt
        let components = calendar.dateComponents([.year, .month, .day], from: date)
        return String(format: "%04d-%02d-%02d", components.year ?? 1970, components.month ?? 1, components.day ?? 1)
    }

    private nonisolated static func escapeMediaPathComponent(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/?#[]@!$&'()*+,;=")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    private func setInboxCount(_ count: Int) {
        guard var currentSession = session else { return }
        currentSession.inboxCount = max(0, count)
        session = currentSession
    }

    private func removeInboxItems(draftIDs: Set<String>) {
        guard !draftIDs.isEmpty else { return }
        let removedCount = inboxItems.filter { draftIDs.contains($0.draftID) }.count
        inboxItems.removeAll { draftIDs.contains($0.draftID) }
        setInboxCount(max(0, inboxBadgeCount - removedCount))
    }

    private func requireReconnect(message: String) async {
        try? await tokenStore.deleteToken(accountID: account.id)
        var updatedAccount = account
        updatedAccount.tokenID = nil
        account = updatedAccount
        session = nil
        submittedCaptures = []
        submissionQualitySummary = nil
        inboxItems = []
        isOfflineMode = true
        requiresReconnect = true
        statusMessage = message
        try? await persist()
    }

    private func persist() async throws {
        upsertCurrentWorkspace()
        try await store.save(currentSnapshot())
    }

    private func currentSnapshot() -> FieldCaptureSnapshot {
        FieldCaptureSnapshot(
            account: account,
            session: session,
            sites: sites,
            visits: visits,
            captures: captures,
            activeAccountID: account.id,
            accountWorkspaces: accountWorkspaces
        )
    }

    private func requestPendingSync() {
        guard pendingSyncTask == nil else {
            syncPendingRequested = true
            return
        }
        startPendingSyncDrain()
    }

    @discardableResult
    private func startPendingSyncDrain() -> Task<Void, Never> {
        let task = Task { [weak self] in
            guard let self else { return }
            await self.drainPendingSync()
        }
        pendingSyncTask = task
        return task
    }
}

private struct MissingCaptureMedia {
    var photos: [CapturePhoto]
    var audios: [CaptureAudio]

    var isEmpty: Bool {
        photos.isEmpty && audios.isEmpty
    }

    var photoIDs: Set<UUID> {
        Set(photos.map(\.id))
    }

    var audioIDs: Set<UUID> {
        Set(audios.map(\.id))
    }

    var summary: String {
        "Missing from this device: \(itemDescriptions.joined(separator: ", "))."
    }

    var inlineSummary: String {
        "missing \(itemDescriptions.joined(separator: ", "))"
    }

    private var itemDescriptions: [String] {
        photos.map { "photo \"\($0.filename)\"" }
            + audios.map { "voice memo \"\($0.filename)\"" }
    }
}

public struct VisitTimelineEntry: Identifiable, Equatable, Sendable {
    public var id: String
    public var title: String
    public var subtitle: String
    public var date: Date
    public var status: CaptureQueueStatus
}

private extension CaptureAPIError {
    var isPermanent: Bool {
        switch self {
        case .insecureBaseURL:
            true
        case .unauthorized:
            true
        case .serverStatus(let status, _, _):
            (400..<500).contains(status) && ![404, 408, 429].contains(status)
        case .invalidResponse:
            false
        }
    }
}

import Foundation
import Observation

@MainActor
@Observable
public final class FieldCaptureModel {
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
    public private(set) var notificationPermissionStatus: NotificationPermissionStatus = .unknown

    private let store: any FieldCaptureStore
    private let apiClient: any CaptureAPIClient
    private let tokenStore: any SecureTokenStore
    private let notificationScheduler: any UploadNotificationScheduling
    private let mediaStore: LocalMediaStore

    public init(
        store: any FieldCaptureStore = SQLiteFieldCaptureStore(),
        apiClient: any CaptureAPIClient = HTTPCaptureAPIClient(),
        tokenStore: any SecureTokenStore = KeychainTokenStore(),
        notificationScheduler: any UploadNotificationScheduling = UploadNotificationScheduler(),
        mediaStore: LocalMediaStore = LocalMediaStore()
    ) {
        self.store = store
        self.apiClient = apiClient
        self.tokenStore = tokenStore
        self.notificationScheduler = notificationScheduler
        self.mediaStore = mediaStore
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
            selectedCategoryValue = newValue?.displayCategories.first?.value
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

    public var maxImagesPerCapture: Int {
        max(0, session?.maxImages ?? 6)
    }

    public var canSubmitCaptures: Bool {
        session?.canSubmit != false
    }

    public var accounts: [BTQAccount] {
        accountWorkspaces.map(\.account)
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
            recoverInterruptedUploads()
            _ = await refreshSessionIfPossible()
            if sites.isEmpty {
                applyDemoSession()
                statusMessage = "Demo data loaded. Paste a token to connect BTQ."
            }
            try? await persist()
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
            statusMessage = "Ready for \(liveSession.person.name)"
            try await persist()
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
        guard let token = try? await tokenStore.loadToken(accountID: account.id), !token.isEmpty else {
            return true
        }
        do {
            let liveSession = try await apiClient.session(baseURL: account.baseURL, token: token)
            apply(liveSession)
            isOfflineMode = false
            statusMessage = "Session refreshed"
            try await persist()
            return true
        } catch CaptureAPIError.unauthorized {
            statusMessage = "Token is invalid, expired, or revoked."
            return false
        } catch {
            statusMessage = sites.isEmpty ? "Could not refresh session." : "Cached session remains available."
            return true
        }
    }

    public func resumeOnlineWork() async {
        guard await refreshSessionIfPossible() else { return }
        await syncPending()
    }

    public func refreshSubmittedHistory() async {
        guard !isRefreshingSubmittedHistory else { return }
        let requestedAccountID = account.id
        let requestedBaseURL = account.baseURL
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
            statusMessage = "Token is invalid, expired, or revoked."
        } catch {
            guard account.id == requestedAccountID else { return }
            statusMessage = "Could not refresh submitted history."
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
        if let removedWorkspace {
            mediaStore.deleteMedia(for: removedWorkspace.captures)
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
    public func saveQuickObservation(photos: [CapturePhoto] = [], audio: CaptureAudio? = nil) async -> Bool {
        guard validateQuickObservationDraft(photoCount: photos.count, hasAudio: audio != nil) else {
            return false
        }
        guard let site = selectedSite else {
            statusMessage = "Choose a site before saving."
            return false
        }
        let category = selectedCategoryValue ?? site.displayCategories.first?.value ?? "general_note"
        let note = observationText.trimmingCharacters(in: .whitespacesAndNewlines)
        let now = Date()
        let suffix = String(UUID().uuidString.prefix(8)).lowercased()
        let kind: CaptureAssetKind = !photos.isEmpty && audio != nil ? .photoVoice : !photos.isEmpty ? .photo : audio != nil ? .voice : .text
        let visitID = activeVisit(forSiteID: site.siteID)?.id
        let capture = LocalCapture(
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
            audio: audio
        )
        captures.append(capture)
        var updatedSite = site
        updatedSite.lastUsedAt = now
        replaceSite(updatedSite)
        observationText = ""
        statusMessage = "Saved locally"
        try? await persist()
        guard !isOfflineMode else {
            statusMessage = "Saved offline. Captures will sync when connection returns."
            return true
        }
        await syncPending()
        return true
    }

    public func syncPending() async {
        guard !isSyncing else { return }
        guard canSubmitCaptures else {
            statusMessage = "This account cannot submit captures."
            return
        }
        guard let token = try? await tokenStore.loadToken(accountID: account.id), !token.isEmpty else {
            statusMessage = "Saved offline. Connect a token to sync."
            return
        }
        isSyncing = true
        defer { isSyncing = false }

        var successfulUploadCount = 0
        for capture in captures where capture.status == .pending && isReadyToRetry(capture) {
            guard let index = captures.firstIndex(where: { $0.captureID == capture.captureID }) else { continue }
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
                _ = try await apiClient.submit(capture: uploadingCapture, baseURL: account.baseURL, token: token)
                guard let completedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                    continue
                }
                var releasedCapture = mediaStore.releaseManagedMedia(for: captures[completedIndex])
                releasedCapture.status = .done
                releasedCapture.lastError = nil
                releasedCapture.retryAfter = nil
                captures[completedIndex] = releasedCapture
                successfulUploadCount += 1
                statusMessage = "Synced \(releasedCapture.siteLabel)"
            } catch let error as CaptureAPIError {
                guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                    if error.isPermanent { continue }
                    statusMessage = "Sync paused. Will retry."
                    break
                }
                captures[failedIndex].attempts += 1
                captures[failedIndex].lastError = error.description
                if error.isPermanent {
                    captures[failedIndex].status = .failed
                    captures[failedIndex].retryAfter = nil
                    statusMessage = "Capture failed: \(error.description)"
                    await notificationScheduler.notifyUploadFailed(capture: captures[failedIndex], reason: error.description)
                    continue
                }
                captures[failedIndex].status = .pending
                captures[failedIndex].retryAfter = nextRetryDate(attempts: captures[failedIndex].attempts)
                statusMessage = "Sync paused. Will retry."
                break
            } catch {
                guard let failedIndex = captures.firstIndex(where: { $0.captureID == uploadingCapture.captureID }) else {
                    statusMessage = "Offline. Capture will sync later."
                    break
                }
                captures[failedIndex].attempts += 1
                captures[failedIndex].status = .pending
                captures[failedIndex].lastError = error.localizedDescription
                captures[failedIndex].retryAfter = nextRetryDate(attempts: captures[failedIndex].attempts)
                statusMessage = "Offline. Capture will sync later."
                break
            }
        }
        try? await persist()
        if successfulUploadCount > 0, queueSummary.pending == 0, queueSummary.failed == 0 {
            await notificationScheduler.notifySyncRecovered(pendingCount: 0)
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

    public func deleteCapture(_ captureID: String) async {
        guard let index = captures.firstIndex(where: { $0.captureID == captureID }) else {
            return
        }
        guard captures[index].status != .uploading else {
            statusMessage = "Capture is uploading and cannot be removed yet."
            return
        }
        let capture = captures.remove(at: index)
        mediaStore.deleteMedia(for: [capture])
        statusMessage = "Capture removed"
        try? await persist()
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
        selectedSiteID = prioritizedSites.first?.siteID
        selectedCategoryValue = selectedSite?.displayCategories.first?.value
    }

    private func applyDemoSession() {
        session = .demo
        sites = BTQSession.demo.sites
        selectedSiteID = sites.first?.siteID
        selectedCategoryValue = sites.first?.displayCategories.first?.value
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
        account = updatedAccount
        session = liveSession
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
            selectedCategoryValue = selectedSite?.displayCategories.first?.value
        }
    }

    private func replaceSite(_ site: BTQSite) {
        if let index = sites.firstIndex(where: { $0.siteID == site.siteID }) {
            sites[index] = site
        } else {
            sites.append(site)
        }
    }

    private func recoverInterruptedUploads(now: Date = .now) {
        let staleThreshold: TimeInterval = 120
        for index in captures.indices where captures[index].status == .uploading {
            let lastTriedAt = captures[index].lastTriedAt ?? captures[index].capturedAt
            if now.timeIntervalSince(lastTriedAt) > staleThreshold {
                captures[index].status = .pending
                captures[index].lastError = "recovered_interrupted_upload"
                captures[index].retryAfter = now
            }
        }
    }

    private func isReadyToRetry(_ capture: LocalCapture, now: Date = .now) -> Bool {
        guard let retryAfter = capture.retryAfter else { return true }
        return retryAfter <= now
    }

    private func missingMediaDescription(for capture: LocalCapture) -> String? {
        for photo in capture.photos {
            guard let fileURL = photo.fileURL,
                  FileManager.default.fileExists(atPath: fileURL.path) else {
                return "Missing photo file: \(photo.filename)"
            }
        }
        if let audio = capture.audio {
            guard let fileURL = audio.fileURL,
                  FileManager.default.fileExists(atPath: fileURL.path) else {
                return "Missing audio file: \(audio.filename)"
            }
        }
        return nil
    }

    private func nextRetryDate(attempts: Int, now: Date = .now) -> Date {
        let schedule: [TimeInterval] = [5, 15, 30, 60, 120]
        let delay = schedule[max(0, min(attempts - 1, schedule.count - 1))]
        return now.addingTimeInterval(delay)
    }

    private func persist() async throws {
        upsertCurrentWorkspace()
        try await store.save(
            FieldCaptureSnapshot(
                account: account,
                session: session,
                sites: sites,
                visits: visits,
                captures: captures,
                activeAccountID: account.id,
                accountWorkspaces: accountWorkspaces
            )
        )
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
        case .unauthorized:
            true
        case .serverStatus(let status, _, _):
            (400..<500).contains(status) && ![404, 408, 429].contains(status)
        case .invalidResponse:
            false
        }
    }
}

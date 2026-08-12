import Foundation
import Testing
@testable import BTQFieldCaptureApp

// Independent verification of the durable background-upload lifecycle change.
// These tests were written without reference to the implementer's notes; they drive the
// production code paths (FieldCaptureModel.load/resumeOnlineWork/syncPending,
// BackgroundUploader's URLSession delegate, HTTPCaptureAPIClient's body store) rather than
// asserting on source text. No sleeps: every wait is a continuation or a task value.

// MARK: - Fixtures

private func verifierSnapshot(
    account: BTQAccount = .defaultProduction,
    captures: [LocalCapture],
    extraWorkspaces: [BTQAccountWorkspace] = []
) -> FieldCaptureSnapshot {
    let site = BTQSite(siteID: "site_1", label: "Site One")
    let session = BTQSession(
        person: BTQPerson(personID: "person_field", name: "Field User"),
        token: BTQToken(tokenID: "token_field", label: "Pilot"),
        sites: [site],
        canSubmit: true,
        canReview: false,
        maxImages: 6
    )
    let active = BTQAccountWorkspace(
        account: account,
        session: session,
        sites: [site],
        visits: [],
        captures: captures
    )
    return FieldCaptureSnapshot(
        account: account,
        session: session,
        sites: [site],
        captures: captures,
        activeAccountID: account.id,
        accountWorkspaces: [active] + extraWorkspaces
    )
}

private func verifierCapture(
    id: String,
    status: CaptureQueueStatus,
    lastTriedAt: Date?,
    photo: CapturePhoto?
) -> LocalCapture {
    LocalCapture(
        captureID: id,
        jobID: "job-\(id)",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "verifier",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        status: status,
        lastTriedAt: lastTriedAt,
        photos: photo.map { [$0] } ?? []
    )
}

private func submitResponse(for captureID: String, photoCount: Int) -> SubmitCaptureResponse {
    SubmitCaptureResponse(
        status: "submitted",
        jobID: "job-\(captureID)",
        captureID: captureID,
        couchdbDocID: "doc-\(captureID)",
        photoCount: photoCount,
        audioCount: 0,
        idempotentReplay: false
    )
}

// MARK: - Stubs

/// A `CaptureAPIClient` whose reconciliation answer is programmable and whose lifecycle calls
/// are recorded, so the model's use of the new seam can be observed exactly.
private actor ProgrammableReconcilingAPIClient: CaptureAPIClient {
    private var reconciliation: CaptureUploadReconciliation
    private(set) var reconcileRequests: [Set<String>] = []
    private(set) var finishedCaptureIDs: [String] = []
    private(set) var sweepPreservedIDs: [Set<String>] = []
    private(set) var submittedCaptureIDs: [String] = []

    init(reconciliation: CaptureUploadReconciliation) {
        self.reconciliation = reconciliation
    }

    func setReconciliation(_ value: CaptureUploadReconciliation) {
        reconciliation = value
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        )
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submittedCaptureIDs.append(capture.captureID)
        return submitResponse(for: capture.captureID, photoCount: capture.photos.count)
    }

    func reconcileBackgroundUploads(captureIDs: Set<String>) async -> CaptureUploadReconciliation {
        reconcileRequests.append(captureIDs)
        return reconciliation
    }

    func finishBackgroundUpload(captureID: String) async {
        finishedCaptureIDs.append(captureID)
    }

    func sweepUploadBodies(preservingCaptureIDs: Set<String>) async {
        sweepPreservedIDs.append(preservingCaptureIDs)
    }
}

/// Lets the test park `reconcileBackgroundUploads` and `submit` independently so the drain and a
/// reconciliation pass can be interleaved deterministically.
private actor InterleavingReconcilingAPIClient: CaptureAPIClient {
    private var reconcileGate: SignalBox?
    private var reconcileGateArmed = false
    private var reconcileParked = SignalBox()
    private var submitGate: SignalBox?
    private var submitGateArmed = false
    private var submitParked = SignalBox()
    private var swept = SignalBox()

    func armReconcileGate() {
        reconcileGate = SignalBox()
        reconcileGateArmed = true
        reconcileParked = SignalBox()
    }

    func armSubmitGate() {
        submitGate = SignalBox()
        submitGateArmed = true
        submitParked = SignalBox()
    }
    func releaseReconcileGate() async { await reconcileGate?.signal() }
    func releaseSubmitGate() async { await submitGate?.signal() }
    func waitUntilReconcileParked() async { await reconcileParked.wait() }
    func waitUntilSubmitParked() async { await submitParked.wait() }
    func waitUntilSwept() async { await swept.wait() }
    func armSweptSignal() { swept = SignalBox() }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        )
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        if submitGateArmed, let gate = submitGate {
            submitGateArmed = false
            await submitParked.signal()
            await gate.wait()
        }
        return submitResponse(for: capture.captureID, photoCount: capture.photos.count)
    }

    func reconcileBackgroundUploads(captureIDs: Set<String>) async -> CaptureUploadReconciliation {
        if reconcileGateArmed, let gate = reconcileGate {
            reconcileGateArmed = false
            await reconcileParked.signal()
            await gate.wait()
        }
        return CaptureUploadReconciliation()
    }

    func finishBackgroundUpload(captureID: String) async {}

    func sweepUploadBodies(preservingCaptureIDs: Set<String>) async {
        await swept.signal()
    }
}

/// Loads a fixed snapshot but refuses every save, so persistence failure can be exercised.
private actor UnwritableFieldCaptureStore: FieldCaptureStore {
    struct SaveRefused: Error {}

    private let snapshot: FieldCaptureSnapshot
    private(set) var saveAttempts = 0

    init(snapshot: FieldCaptureSnapshot) {
        self.snapshot = snapshot
    }

    func load() async throws -> FieldCaptureSnapshot { snapshot }

    func save(_ snapshot: FieldCaptureSnapshot) async throws {
        saveAttempts += 1
        throw SaveRefused()
    }
}

/// Replies 200 with a canned submit payload, and can hold the response open on a gate so an
/// upload can be observed while it is genuinely in flight.
private final class VerifierUploadProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var responseBody: Data = Data()
    nonisolated(unsafe) static var statusCode: Int = 200
    nonisolated(unsafe) static var didStart: (@Sendable () -> Void)?
    nonisolated(unsafe) static var gate: (@Sendable () async -> Void)?

    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }

    override func startLoading() {
        nonisolated(unsafe) let this = self
        nonisolated(unsafe) let sink = self.client
        let request = self.request
        let body = Self.responseBody
        let status = Self.statusCode
        let started = Self.didStart
        let gate = Self.gate
        Task {
            started?()
            await gate?()
            let response = HTTPURLResponse(
                url: request.url!,
                statusCode: status,
                httpVersion: "HTTP/1.1",
                headerFields: ["Content-Type": "application/json"]
            )!
            sink?.urlProtocol(this, didReceive: response, cacheStoragePolicy: .notAllowed)
            sink?.urlProtocol(this, didLoad: body)
            sink?.urlProtocolDidFinishLoading(this)
        }
    }

    override func stopLoading() {}
}

/// Records the multipart body file the API client hands to the uploader, leaves a sibling copy
/// behind (standing in for a body a killed process could not clean up), then fails the transfer.
private actor BodyObservingUploader: CaptureUploader {
    private(set) var bodyFiles: [URL] = []
    private(set) var orphanFiles: [URL] = []
    private(set) var captureIDs: [String?] = []

    func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        try await upload(request, fromFile: file, captureID: nil)
    }

    func upload(_ request: URLRequest, fromFile file: URL, captureID: String?) async throws -> (Data, URLResponse) {
        bodyFiles.append(file)
        captureIDs.append(captureID)
        let orphan = file.deletingLastPathComponent().appendingPathComponent("orphan-body.multipart")
        try? FileManager.default.copyItem(at: file, to: orphan)
        orphanFiles.append(orphan)
        throw URLError(.networkConnectionLost)
    }

    /// Authoritative with no live tasks — mirrors the production uploader after every transfer
    /// has resolved, so the sweep is authorised to run (see `nonAuthoritativeSnapshotSweepsNothing`
    /// for the blind case).
    func reconciliationSnapshot() async -> BackgroundUploadSnapshot {
        BackgroundUploadSnapshot(liveCaptureIDs: [], completions: [], isAuthoritative: true)
    }

    func upload(_ request: URLRequest, fromFile file: URL, captureID: String) async throws -> (Data, URLResponse) {
        try await upload(request, fromFile: file, captureID: Optional(captureID))
    }
}

// MARK: - C2 / C4 / I1 / I2 — reconciliation against live background tasks

@Test @MainActor
func liveBackgroundTaskKeepsCaptureUploadingAndOffTheWire() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-live-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: mediaRoot) }

    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let capture = verifierCapture(
        id: "cap-live",
        status: .uploading,
        // Far older than the 120s stale-recovery threshold.
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )
    let apiClient = ProgrammableReconcilingAPIClient(
        reconciliation: CaptureUploadReconciliation(liveCaptureIDs: ["cap-live"])
    )
    let tokenStore = MemoryTokenStore()
    let account = BTQAccount.defaultProduction
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: LocalMediaStore(rootDirectory: mediaRoot)
    )
    await tokenStore.saveToken("token-live", accountID: account.id)
    await model.load()
    await model.syncPending()

    #expect(model.captures.first?.status == .uploading)
    #expect(await apiClient.submittedCaptureIDs.isEmpty)
    #expect(FileManager.default.fileExists(atPath: photoURL.path))
    #expect(await apiClient.reconcileRequests.first == ["cap-live"])
    // The live capture's multipart body must be preserved by the sweep.
    #expect(await apiClient.sweepPreservedIDs.last == ["cap-live"])
}

@Test @MainActor
func strandedUploadWithNoLiveTaskAndNoCompletionReturnsToPendingAndRetries() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-stranded-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: mediaRoot) }

    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let capture = verifierCapture(
        id: "cap-stranded",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )
    let apiClient = ProgrammableReconcilingAPIClient(reconciliation: CaptureUploadReconciliation())
    let tokenStore = MemoryTokenStore()
    let account = BTQAccount.defaultProduction
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: LocalMediaStore(rootDirectory: mediaRoot)
    )
    await tokenStore.saveToken("token-stranded", accountID: account.id)
    await model.load()

    #expect(model.captures.first?.status == .pending)

    await model.syncPending()

    #expect(await apiClient.submittedCaptureIDs == ["cap-stranded"])
    #expect(model.captures.first?.status == .done)
    #expect(!FileManager.default.fileExists(atPath: photoURL.path))
}

@Test @MainActor
func storedSuccessfulCompletionConfirmsCaptureExactlyOnce() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-confirm-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: mediaRoot) }

    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let capture = verifierCapture(
        id: "cap-confirmed",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )
    let apiClient = ProgrammableReconcilingAPIClient(
        reconciliation: CaptureUploadReconciliation(
            liveCaptureIDs: [],
            completedResponses: ["cap-confirmed": submitResponse(for: "cap-confirmed", photoCount: 1)]
        )
    )
    let tokenStore = MemoryTokenStore()
    let account = BTQAccount.defaultProduction
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: LocalMediaStore(rootDirectory: mediaRoot)
    )
    await tokenStore.saveToken("token-confirm", accountID: account.id)
    await model.load()

    let confirmed = try #require(model.captures.first)
    #expect(confirmed.status == .done)
    #expect(confirmed.photos.first?.remoteURL != nil)          // remote URLs adopted
    #expect(confirmed.photos.first?.fileURL == nil)            // managed media reference released
    #expect(!FileManager.default.fileExists(atPath: photoURL.path))
    #expect(await apiClient.finishedCaptureIDs == ["cap-confirmed"])

    // A second reconciliation pass plus a drain must not resubmit the same capture.
    await model.resumeOnlineWork()
    #expect(await apiClient.submittedCaptureIDs.isEmpty)
    #expect(await apiClient.finishedCaptureIDs == ["cap-confirmed"])
    #expect(model.captures.first?.status == .done)
}

@Test @MainActor
func confirmationThatCannotBePersistedKeepsMediaAndDoesNotResubmit() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-persistfail-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: mediaRoot) }

    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let capture = verifierCapture(
        id: "cap-persist-fail",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )
    let apiClient = ProgrammableReconcilingAPIClient(
        reconciliation: CaptureUploadReconciliation(
            liveCaptureIDs: [],
            completedResponses: ["cap-persist-fail": submitResponse(for: "cap-persist-fail", photoCount: 1)]
        )
    )
    let tokenStore = MemoryTokenStore()
    let account = BTQAccount.defaultProduction
    let model = FieldCaptureModel(
        store: UnwritableFieldCaptureStore(snapshot: verifierSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: LocalMediaStore(rootDirectory: mediaRoot)
    )
    await tokenStore.saveToken("token-persist-fail", accountID: account.id)
    await model.load()

    // I1: evidence must survive a failed confirmation write.
    #expect(FileManager.default.fileExists(atPath: photoURL.path))
    #expect(model.captures.first?.photos.first?.fileURL == photoURL)
    // The durable completion must NOT be discarded — it is the only proof of server success.
    #expect(await apiClient.finishedCaptureIDs.isEmpty)
    // The capture stays out of the retry path while a durable success exists.
    #expect(model.captures.first?.status == .uploading)

    await model.syncPending()
    #expect(await apiClient.submittedCaptureIDs.isEmpty)
}

// MARK: - D3 (was C5 boundary defect) — every workspace is reconciled, not just the active one

/// Flipped from the pre-fix characterization test. It previously asserted that reconciliation
/// and the body sweep saw only the ACTIVE workspace; it now asserts the mechanism of the fix:
/// every workspace's `.uploading` captures are reconciled, outcomes land in the workspace that
/// owns them, non-active workspaces are persisted, and a live upload parked in a non-active
/// workspace keeps its body across an account switch.
@Test @MainActor
func everyWorkspaceIsReconciledAndNonActiveLiveUploadsSurviveAnAccountSwitch() async throws {
    let account = BTQAccount.defaultProduction
    let otherAccount = BTQAccount(label: "Second", baseURL: URL(string: "https://fc.example.com")!)

    // Active workspace holds an untouched pending capture — the cross-contamination canary.
    let activePending = verifierCapture(
        id: "cap-active-pending",
        status: .pending,
        lastTriedAt: nil,
        photo: nil
    )
    // Non-active workspace holds all three reconciliation outcomes.
    let otherLive = verifierCapture(
        id: "cap-other-live",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: nil
    )
    let otherStranded = verifierCapture(
        id: "cap-other-stranded",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: nil
    )
    let otherConfirmed = verifierCapture(
        id: "cap-other-confirmed",
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -3_600),
        photo: nil
    )

    let apiClient = ProgrammableReconcilingAPIClient(
        reconciliation: CaptureUploadReconciliation(
            liveCaptureIDs: ["cap-other-live"],
            completedResponses: [
                "cap-other-confirmed": submitResponse(for: "cap-other-confirmed", photoCount: 0)
            ]
        )
    )
    let store = MemoryFieldCaptureStore(
        snapshot: verifierSnapshot(
            account: account,
            captures: [activePending],
            extraWorkspaces: [
                BTQAccountWorkspace(
                    account: otherAccount,
                    captures: [otherLive, otherStranded, otherConfirmed]
                )
            ]
        )
    )
    let model = FieldCaptureModel(
        store: store,
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    // MECHANISM 1: the reconciliation request folds in EVERY workspace's uploading captures,
    // including ones the active model does not own.
    #expect(await apiClient.reconcileRequests.first == [
        "cap-other-live", "cap-other-stranded", "cap-other-confirmed",
    ])

    // MECHANISM 2: the sweep's preserve set carries the non-active workspace's live capture,
    // so its multipart body is not deleted out from under the running transfer.
    #expect(await apiClient.sweepPreservedIDs.last?.contains("cap-other-live") == true)

    // MECHANISM 3: outcomes are applied per workspace, to the workspace that owns the capture.
    let otherWorkspace = try #require(
        model.accountWorkspaces.first(where: { $0.account.id == otherAccount.id })
    )
    let byID = Dictionary(uniqueKeysWithValues: otherWorkspace.captures.map { ($0.captureID, $0) })
    #expect(byID["cap-other-live"]?.status == .uploading)
    #expect(byID["cap-other-stranded"]?.status == .pending)
    #expect(byID["cap-other-confirmed"]?.status == .done)
    #expect(await apiClient.finishedCaptureIDs == ["cap-other-confirmed"])

    // No cross-contamination: the active workspace's own capture is untouched.
    #expect(model.captures.map(\.captureID) == ["cap-active-pending"])
    #expect(model.captures.first?.status == .pending)
    #expect(model.captures.first?.lastError == nil)

    // N4: a non-active workspace's outcome must not narrate into the active account's UI,
    // while its state change still lands (asserted above and in the persisted snapshot below).
    #expect(!model.statusMessage.contains("Synced"))
    #expect(!model.statusMessage.contains("Capture failed"))

    // MECHANISM 4: persistence covers the non-active workspace it mutated.
    let persisted = try await store.load()
    let persistedOther = try #require(
        persisted.accountWorkspaces.first(where: { $0.account.id == otherAccount.id })
    )
    let persistedByID = Dictionary(
        uniqueKeysWithValues: persistedOther.captures.map { ($0.captureID, $0) }
    )
    #expect(persistedByID["cap-other-live"]?.status == .uploading)
    #expect(persistedByID["cap-other-stranded"]?.status == .pending)
    #expect(persistedByID["cap-other-confirmed"]?.status == .done)

    // MECHANISM 5: after switching accounts the live upload is still live and still preserved,
    // and it is never handed back to `submit`.
    await model.switchAccount(otherAccount.id)
    #expect(model.account.id == otherAccount.id)
    #expect(model.captures.first(where: { $0.captureID == "cap-other-live" })?.status == .uploading)

    await model.resumeOnlineWork()
    #expect(await apiClient.sweepPreservedIDs.last?.contains("cap-other-live") == true)
    #expect(model.captures.first(where: { $0.captureID == "cap-other-live" })?.status == .uploading)
    #expect(await apiClient.submittedCaptureIDs.isEmpty)
}

// MARK: - D4 — the upload-body reservation

@Test
func reservationKeepsAnInFlightSubmitLiveBeforeAnyURLSessionTaskExists() async throws {
    let bodyRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-reserve-\(UUID().uuidString)", isDirectory: true)
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-reserve-media-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: bodyRoot)
        try? FileManager.default.removeItem(at: mediaRoot)
    }
    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let uploader = GatedAuthoritativeUploader()
    let client = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: uploader,
        uploadBodyDirectory: bodyRoot
    )
    let capture = verifierCapture(
        id: "cap-reserved",
        status: .uploading,
        lastTriedAt: .now,
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )

    let submitTask = Task {
        try await client.submit(
            capture: capture,
            baseURL: URL(string: "https://fc.example.com")!,
            token: "token"
        )
    }

    // The uploader has the body file but has NOT created a URLSession task — precisely the
    // window the reservation exists to cover.
    await uploader.waitUntilStarted()
    let bodyFile = try #require(await uploader.bodyFiles.first)
    #expect(FileManager.default.fileExists(atPath: bodyFile.path))

    let duringFlight = await client.reconcileBackgroundUploads(captureIDs: ["cap-reserved"])
    #expect(duringFlight.isAuthoritative)
    #expect(duringFlight.liveCaptureIDs.contains("cap-reserved"))

    await client.sweepUploadBodies(preservingCaptureIDs: [])
    #expect(FileManager.default.fileExists(atPath: bodyFile.path))

    await uploader.release()
    _ = try? await submitTask.value

    // Reservation released once submit unwinds.
    let afterFlight = await client.reconcileBackgroundUploads(captureIDs: ["cap-reserved"])
    #expect(!afterFlight.liveCaptureIDs.contains("cap-reserved"))
}

@Test
func reservationIsReleasedWhenSubmitThrowsBeforeTheBodyIsWritten() async throws {
    let bodyRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-earlythrow-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: bodyRoot) }

    let uploader = GatedAuthoritativeUploader()
    let client = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: uploader,
        uploadBodyDirectory: bodyRoot
    )
    let capture = verifierCapture(id: "cap-early-throw", status: .uploading, lastTriedAt: .now, photo: nil)

    // `secureAPIURL` rejects a non-HTTPS base URL after the reservation is taken and before
    // any body file exists.
    await #expect(throws: CaptureAPIError.insecureBaseURL) {
        _ = try await client.submit(
            capture: capture,
            baseURL: URL(string: "http://fc.example.com")!,
            token: "token"
        )
    }
    #expect(await uploader.bodyFiles.isEmpty)

    let reconciliation = await client.reconcileBackgroundUploads(captureIDs: ["cap-early-throw"])
    #expect(!reconciliation.liveCaptureIDs.contains("cap-early-throw"))
}

/// Flipped from the characterization test. The reservation is now counted, so overlapping
/// submits of the SAME capture ID each hold a reference and only the LAST release frees it.
/// Asserts the counted semantics through the only public door (`submit`), plus the two ways a
/// naive counter breaks: leaking on an early throw, and going negative.
@Test
func overlappingSubmitsHoldCountedReservations() async throws {
    let bodyRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-refcount-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: bodyRoot) }

    let uploader = GatedAuthoritativeUploader()
    let client = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: uploader,
        uploadBodyDirectory: bodyRoot
    )
    let capture = verifierCapture(id: "cap-overlap", status: .uploading, lastTriedAt: .now, photo: nil)
    let baseURL = URL(string: "https://fc.example.com")!

    // reserve #1, reserve #2 — both in flight.
    let first = Task { try await client.submit(capture: capture, baseURL: baseURL, token: "t") }
    await uploader.waitUntilStarted(count: 1)
    let second = Task { try await client.submit(capture: capture, baseURL: baseURL, token: "t") }
    await uploader.waitUntilStarted(count: 2)

    let secondBody = try #require(await uploader.bodyFiles.last)
    #expect(FileManager.default.fileExists(atPath: secondBody.path))
    #expect(await client.reconcileBackgroundUploads(captureIDs: ["cap-overlap"])
        .liveCaptureIDs.contains("cap-overlap"))

    // An unrelated failed submit for the SAME id must not decrement the live reservations.
    // (A counter that released on every unwind regardless of ownership would drop to 1 here
    // and to 0 after the next release, freeing the body while #2 still runs.)
    await #expect(throws: CaptureAPIError.insecureBaseURL) {
        _ = try await client.submit(
            capture: capture,
            baseURL: URL(string: "http://fc.example.com")!,
            token: "t"
        )
    }
    #expect(await client.reconcileBackgroundUploads(captureIDs: ["cap-overlap"])
        .liveCaptureIDs.contains("cap-overlap"))

    // release #1 — count drops to 1, so the ID stays live and #2's body survives a sweep.
    await uploader.release(upTo: 1)
    _ = try? await first.value

    #expect(await client.reconcileBackgroundUploads(captureIDs: ["cap-overlap"])
        .liveCaptureIDs.contains("cap-overlap"))
    await client.sweepUploadBodies(preservingCaptureIDs: [])
    #expect(FileManager.default.fileExists(atPath: secondBody.path))

    // release #2 — the last reference frees the ID and its body directory.
    await uploader.release()
    _ = try? await second.value

    #expect(!(await client.reconcileBackgroundUploads(captureIDs: ["cap-overlap"])
        .liveCaptureIDs.contains("cap-overlap")))
    #expect(!FileManager.default.fileExists(atPath: secondBody.path))

    // Cannot go negative: further resolved submits leave the ID un-live, never "live" again.
    await #expect(throws: CaptureAPIError.insecureBaseURL) {
        _ = try await client.submit(
            capture: capture,
            baseURL: URL(string: "http://fc.example.com")!,
            token: "t"
        )
    }
    #expect(!(await client.reconcileBackgroundUploads(captureIDs: ["cap-overlap"])
        .liveCaptureIDs.contains("cap-overlap")))
}

// MARK: - N1 — reconciliation write-back is scoped, not wholesale

/// Flipped from the characterization test. `reconcileBackgroundUploads` used to write its stale
/// pre-await workspace copy over `captures` wholesale, rolling back anything a concurrent drain
/// had done. It now (a) re-folds the drain's array after the await, (b) only reconciles capture
/// IDs that were in its own request, and (c) merges back only the IDs it actually changed. This
/// drives the same interleaving and asserts the drain's work survives.
@Test @MainActor
func reconciliationWriteBackPreservesConcurrentDrainState() async throws {
    let account = BTQAccount.defaultProduction
    let capture = verifierCapture(id: "cap-interleave", status: .pending, lastTriedAt: nil, photo: nil)
    let apiClient = InterleavingReconcilingAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("token-interleave", accountID: account.id)
    await model.load()
    #expect(model.captures.first?.status == .pending)

    // Park a reconciliation right after it has snapshotted the workspace.
    await apiClient.armSweptSignal()
    await apiClient.armReconcileGate()
    let resume = Task { await model.resumeOnlineWork() }
    await apiClient.waitUntilReconcileParked()

    // Now let a drain move the capture to `.uploading` and park it inside `submit`.
    await apiClient.armSubmitGate()
    let drain = Task { await model.syncPending() }
    await apiClient.waitUntilSubmitParked()
    #expect(model.captures.first?.status == .uploading)

    // Release reconciliation. Its pre-await snapshot still says `.pending`; the merge must not
    // put that back.
    await apiClient.releaseReconcileGate()
    await apiClient.waitUntilSwept()

    // MECHANISM: the capture was not in reconciliation's own `captureIDs` request (it was
    // `.pending` when the request was built), so it is neither reconciled nor merged back.
    let statusAfterWriteBack = model.captures.first?.status
    #expect(statusAfterWriteBack == .uploading,
            "drain state was rolled back to \(String(describing: statusAfterWriteBack))")
    // The drain's own bookkeeping survives too, not just the status.
    #expect(model.captures.first?.lastTriedAt != nil)
    #expect(model.captures.first?.lastError == nil)

    await apiClient.releaseSubmitGate()
    await drain.value
    _ = await resume.value
    #expect(model.captures.first?.status == .done)
}

// MARK: - C1 / C3 — durable identity and completions without a waiter

@Test
func inFlightUploadIsReportedLiveByCaptureID() async throws {
    let completionDirectory = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-live-snapshot-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: completionDirectory) }

    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [VerifierUploadProtocol.self]
    VerifierUploadProtocol.statusCode = 200
    VerifierUploadProtocol.responseBody = try JSONEncoder().encode(
        submitResponse(for: "cap-inflight", photoCount: 0)
    )

    let started = SignalBox()
    let release = SignalBox()
    VerifierUploadProtocol.didStart = { [started] in Task { await started.signal() } }
    VerifierUploadProtocol.gate = { [release] in await release.wait() }
    defer {
        VerifierUploadProtocol.didStart = nil
        VerifierUploadProtocol.gate = nil
    }

    let uploader = BackgroundUploader(
        configuration: configuration,
        completionDirectory: completionDirectory
    )
    let bodyFile = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-body-\(UUID().uuidString).multipart")
    try Data("multipart-body".utf8).write(to: bodyFile)
    defer { try? FileManager.default.removeItem(at: bodyFile) }

    var request = URLRequest(url: URL(string: "https://fc.example.com/api/captures")!)
    request.httpMethod = "POST"

    let uploadTask = Task {
        try await uploader.upload(request, fromFile: bodyFile, captureID: "cap-inflight")
    }

    await started.wait()
    let inFlight = await uploader.reconciliationSnapshot()
    #expect(inFlight.isAuthoritative)
    #expect(inFlight.liveCaptureIDs.contains("cap-inflight"))

    await release.signal()
    _ = try await uploadTask.value

    let afterCompletion = await uploader.reconciliationSnapshot()
    #expect(!afterCompletion.liveCaptureIDs.contains("cap-inflight"))
    let completion = try #require(afterCompletion.completions.first { $0.captureID == "cap-inflight" })
    #expect(completion.statusCode == 200)
    #expect(completion.errorDescription == nil)
    #expect(!completion.responseData.isEmpty)

    await uploader.discardCompletion(for: "cap-inflight")
    let afterDiscard = await uploader.reconciliationSnapshot()
    #expect(afterDiscard.completions.isEmpty)
}

@Test
func completionWithNoWaitingContinuationIsStillRecorded() async throws {
    let completionDirectory = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-nowaiter-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: completionDirectory) }

    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [VerifierUploadProtocol.self]
    let uploader = BackgroundUploader(
        configuration: configuration,
        completionDirectory: completionDirectory
    )

    // A task the uploader never registered a continuation for — exactly the relaunch case
    // where the process that started the transfer is gone.
    let orphanSession = URLSession(configuration: .ephemeral)
    let orphanTask = orphanSession.dataTask(with: URL(string: "https://fc.example.com/api/captures")!)
    orphanTask.taskDescription = "cap-orphan"

    uploader.urlSession(orphanSession, task: orphanTask, didCompleteWithError: nil)

    let snapshot = await uploader.reconciliationSnapshot()
    let completion = try #require(snapshot.completions.first { $0.captureID == "cap-orphan" })
    #expect(completion.errorDescription == nil)
    orphanTask.cancel()
    orphanSession.invalidateAndCancel()
}

// MARK: - C5 — multipart bodies have a durable home and a scoped sweep

@Test
func multipartBodiesLiveInAppStorageAndSweepSparesLiveCaptures() async throws {
    let bodyRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-bodies-\(UUID().uuidString)", isDirectory: true)
    let mediaRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-bodies-media-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: bodyRoot)
        try? FileManager.default.removeItem(at: mediaRoot)
    }

    let photoURL = mediaRoot.appendingPathComponent("evidence.jpg")
    try Data("photo-bytes".utf8).write(to: photoURL)

    let uploader = BodyObservingUploader()
    let client = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: uploader,
        uploadBodyDirectory: bodyRoot
    )

    let liveCapture = verifierCapture(
        id: "cap-body-live",
        status: .uploading,
        lastTriedAt: .now,
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )
    let deadCapture = verifierCapture(
        id: "cap-body-dead",
        status: .uploading,
        lastTriedAt: .now,
        photo: CapturePhoto(filename: "evidence.jpg", fileURL: photoURL)
    )

    for capture in [liveCapture, deadCapture] {
        do {
            _ = try await client.submit(
                capture: capture,
                baseURL: URL(string: "https://fc.example.com")!,
                token: "token"
            )
            Issue.record("submit should have surfaced the uploader failure")
        } catch {
            // expected — the stub uploader fails the transfer after recording the body
        }
    }

    let bodyFiles = await uploader.bodyFiles
    #expect(bodyFiles.count == 2)
    // Durable home: not the process-scoped temporary directory.
    for file in bodyFiles {
        #expect(file.path.hasPrefix(bodyRoot.standardizedFileURL.path + "/"))
        #expect(!file.deletingLastPathComponent().path.hasSuffix("/T"))
    }
    // The capture ID reaches the uploader for durable correlation.
    #expect(await uploader.captureIDs == ["cap-body-live", "cap-body-dead"])

    // Followup 2 deletes a capture's whole body directory as soon as its LAST in-flight submit
    // resolves, so a body can no longer be orphaned while its own submit is unwinding. That is
    // correct (see report); the killed-process state has to be staged the way a killed process
    // actually produces it — a directory left on disk with no live task and no owning submit.
    let orphans = await uploader.orphanFiles
    #expect(orphans.count == 2)
    for orphan in orphans {
        #expect(!FileManager.default.fileExists(atPath: orphan.path),
                "a resolved submit must not leave its body directory behind")
    }
    for orphan in orphans {
        try FileManager.default.createDirectory(
            at: orphan.deletingLastPathComponent(),
            withIntermediateDirectories: true
        )
        try Data("stranded-multipart-body".utf8).write(to: orphan)
    }

    await client.sweepUploadBodies(preservingCaptureIDs: ["cap-body-live"])

    #expect(FileManager.default.fileExists(atPath: orphans[0].path))   // live task's body preserved
    #expect(!FileManager.default.fileExists(atPath: orphans[1].path))  // stranded body swept

    await client.finishBackgroundUpload(captureID: "cap-body-live")
    #expect(!FileManager.default.fileExists(atPath: orphans[0].path))
}

// MARK: - N3 — a non-authoritative snapshot must sweep nothing

/// `sweepUploadBodies` used to run its deletion pass even when the uploader could not report
/// liveness (`.unavailable`), which would wipe bodies backing tasks it simply could not see.
@Test
func nonAuthoritativeSnapshotSweepsNothing() async throws {
    let bodyRoot = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verify-nonauth-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: bodyRoot) }

    // Record a real body directory path through the production path, then restage it.
    let recordingUploader = BodyObservingUploader()
    let recordingClient = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: recordingUploader,
        uploadBodyDirectory: bodyRoot
    )
    let capture = verifierCapture(id: "cap-nonauth", status: .uploading, lastTriedAt: .now, photo: nil)
    _ = try? await recordingClient.submit(
        capture: capture,
        baseURL: URL(string: "https://fc.example.com")!,
        token: "t"
    )
    let strandedBody = try #require(await recordingUploader.orphanFiles.first)
    try FileManager.default.createDirectory(
        at: strandedBody.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try Data("stranded-multipart-body".utf8).write(to: strandedBody)

    // A client whose uploader cannot report liveness (protocol default -> `.unavailable`).
    let blindClient = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: LivenessBlindUploader(),
        uploadBodyDirectory: bodyRoot
    )
    #expect(await blindClient.reconcileBackgroundUploads(captureIDs: ["cap-nonauth"])
        .isAuthoritative == false)

    await blindClient.sweepUploadBodies(preservingCaptureIDs: [])
    #expect(FileManager.default.fileExists(atPath: strandedBody.path),
            "a blind uploader must not authorise a deletion pass")

    // An authoritative client with the same directory still sweeps it.
    let seeingClient = HTTPCaptureAPIClient(
        session: URLSession(configuration: .ephemeral),
        uploader: GatedAuthoritativeUploader(),
        uploadBodyDirectory: bodyRoot
    )
    await seeingClient.sweepUploadBodies(preservingCaptureIDs: [])
    #expect(!FileManager.default.fileExists(atPath: strandedBody.path))
}

// MARK: - Support

/// Uses the protocol's default `reconciliationSnapshot()` (`.unavailable`) — an uploader that
/// cannot report which transfers are live.
private struct LivenessBlindUploader: CaptureUploader {
    func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        throw URLError(.networkConnectionLost)
    }

    func upload(_ request: URLRequest, fromFile file: URL, captureID: String) async throws -> (Data, URLResponse) {
        throw URLError(.networkConnectionLost)
    }
}

/// Holds each `upload` call open on a per-call gate and reports an AUTHORITATIVE snapshot with
/// no live URLSession tasks — so the only thing that can protect an in-flight body is the API
/// client's own reservation. No sleeps: start/release are continuation-based.
private actor GatedAuthoritativeUploader: CaptureUploader {
    private(set) var bodyFiles: [URL] = []
    private var startedCount = 0
    private var startWaiters: [(count: Int, continuation: CheckedContinuation<Void, Never>)] = []
    private var gates: [CheckedContinuation<Void, Never>] = []
    private var releasedCount = 0

    func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        try await upload(request, fromFile: file, captureID: "")
    }

    func upload(_ request: URLRequest, fromFile file: URL, captureID: String) async throws -> (Data, URLResponse) {
        bodyFiles.append(file)
        startedCount += 1
        let reached = startedCount
        startWaiters.removeAll { waiter in
            guard waiter.count <= reached else { return false }
            waiter.continuation.resume()
            return true
        }
        if releasedCount < reached {
            await withCheckedContinuation { continuation in
                gates.append(continuation)
            }
        }
        throw URLError(.networkConnectionLost)
    }

    func reconciliationSnapshot() async -> BackgroundUploadSnapshot {
        BackgroundUploadSnapshot(liveCaptureIDs: [], completions: [], isAuthoritative: true)
    }

    func waitUntilStarted(count: Int = 1) async {
        if startedCount >= count { return }
        await withCheckedContinuation { continuation in
            startWaiters.append((count, continuation))
        }
    }

    /// Releases gates up to `upTo` calls (default: all outstanding).
    func release(upTo limit: Int = .max) {
        while releasedCount < limit, !gates.isEmpty {
            releasedCount += 1
            gates.removeFirst().resume()
        }
        if limit == .max {
            releasedCount = max(releasedCount, startedCount)
        }
    }
}

/// A one-shot async signal: no sleeps, no polling.
private actor SignalBox {
    private var isSignalled = false
    private var waiters: [CheckedContinuation<Void, Never>] = []

    func signal() {
        isSignalled = true
        let pending = waiters
        waiters.removeAll()
        for waiter in pending { waiter.resume() }
    }

    func wait() async {
        if isSignalled { return }
        await withCheckedContinuation { continuation in
            waiters.append(continuation)
        }
    }
}

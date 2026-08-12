import Foundation
import Testing
@testable import BTQFieldCaptureApp

// Independent verification of the "durable local save is not gated on the network"
// change. Nothing here reuses the implementer's assertions; every gate is driven by
// a controllable uploader that suspends on a continuation (no sleeps anywhere).

// MARK: - Controllable uploader

private enum VerifierSubmitOutcome: Sendable {
    case success
    case failure(CaptureAPIError)
}

/// An uploader that parks every `submit` on a continuation until the test opens the
/// gate. Tests can also await the *arrival* of the Nth submit, so "an upload is
/// genuinely in flight" is an observed fact, never a timing guess.
private actor GatedSubmitAPIClient: CaptureAPIClient {
    private(set) var submittedCaptureIDs: [String] = []
    private var gateOpen: Bool
    private var gateWaiters: [CheckedContinuation<Void, Never>] = []
    private var arrivalWaiters: [(threshold: Int, continuation: CheckedContinuation<Void, Never>)] = []
    private let outcome: VerifierSubmitOutcome

    init(outcome: VerifierSubmitOutcome = .success, gateOpen: Bool = false) {
        self.outcome = outcome
        self.gateOpen = gateOpen
    }

    var submitCount: Int { submittedCaptureIDs.count }

    var distinctSubmittedCaptureIDs: Set<String> { Set(submittedCaptureIDs) }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submittedCaptureIDs.append(capture.captureID)
        resumeArrivalWaiters()
        if !gateOpen {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                gateWaiters.append(continuation)
            }
        }
        switch outcome {
        case .success:
            return SubmitCaptureResponse(
                status: "submitted",
                jobID: capture.jobID,
                captureID: capture.captureID,
                couchdbDocID: capture.captureID,
                photoCount: capture.photos.count,
                audioCount: capture.audioAttachments.count,
                idempotentReplay: false
            )
        case .failure(let error):
            throw error
        }
    }

    func openGate() {
        gateOpen = true
        let waiters = gateWaiters
        gateWaiters = []
        for waiter in waiters {
            waiter.resume()
        }
    }

    /// Suspends until at least `count` submits have *started*.
    func waitForSubmitArrival(count: Int) async {
        if submittedCaptureIDs.count >= count { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            arrivalWaiters.append((count, continuation))
        }
    }

    private func resumeArrivalWaiters() {
        let current = submittedCaptureIDs.count
        var remaining: [(threshold: Int, continuation: CheckedContinuation<Void, Never>)] = []
        for waiter in arrivalWaiters {
            if current >= waiter.threshold {
                waiter.continuation.resume()
            } else {
                remaining.append(waiter)
            }
        }
        arrivalWaiters = remaining
    }
}

private enum VerifierStoreError: Error {
    case saveFailed
}

/// Load succeeds, every save fails — used to exercise the local-persistence
/// failure path of `saveQuickObservation`.
private actor VerifierFailingSaveStore: FieldCaptureStore {
    private let snapshot: FieldCaptureSnapshot

    init(snapshot: FieldCaptureSnapshot) {
        self.snapshot = snapshot
    }

    func load() async throws -> FieldCaptureSnapshot { snapshot }

    func save(_ snapshot: FieldCaptureSnapshot) async throws {
        throw VerifierStoreError.saveFailed
    }
}

// MARK: - Fixtures

private func verifierPackageRoot() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
}

private func verifierSnapshot(captures: [LocalCapture] = []) -> FieldCaptureSnapshot {
    FieldCaptureSnapshot(
        account: .defaultProduction,
        session: .demo,
        sites: BTQSession.demo.sites,
        captures: captures
    )
}

private func makeVerifierMediaStore() -> LocalMediaStore {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verifier-media-\(UUID().uuidString)", isDirectory: true)
    return LocalMediaStore(rootDirectory: root)
}

/// Writes a real file inside the media store's managed root so release/delete
/// behaviour is exercised for real.
private func makeManagedPhoto(in mediaStore: LocalMediaStore) throws -> CapturePhoto {
    let directory = mediaStore.mediaDirectory(bucketID: "verifier-bucket")
    try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
    let filename = "photo-\(UUID().uuidString).jpg"
    let url = directory.appendingPathComponent(filename)
    try Data([0xFF, 0xD8, 0xFF, 0xD9]).write(to: url)
    return CapturePhoto(filename: filename, mimeType: "image/jpeg", fileURL: url)
}

@MainActor
private func makeOnlineVerifierModel(
    apiClient: any CaptureAPIClient,
    store: any FieldCaptureStore = MemoryFieldCaptureStore(snapshot: verifierSnapshot()),
    mediaStore: LocalMediaStore = makeVerifierMediaStore()
) async -> FieldCaptureModel {
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: store,
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: mediaStore
    )
    await model.load()
    await tokenStore.saveToken("verifier-token", accountID: model.account.id)
    // Bring the model online WITHOUT going through handleConnectivityChange, which
    // would itself kick a drain and muddy what each test is proving.
    model.isOfflineMode = false
    return model
}

/// Gives the cooperative pool many scheduling opportunities without sleeping.
/// Used only as a secondary discriminator, never as primary evidence.
private func yieldRepeatedly(_ count: Int = 100) async {
    for _ in 0..<count {
        await Task.yield()
    }
}

private func verifierSourceText(_ relativePath: String) throws -> String {
    try String(contentsOf: verifierPackageRoot().appendingPathComponent(relativePath), encoding: .utf8)
}

// MARK: - A1 / I3: save returns at the durable-local boundary

// If `saveQuickObservation` awaited the upload, this test could never finish: the
// gate is never opened before the assertion. Returning at all is the proof.
@Test(.timeLimit(.minutes(1))) @MainActor
func verifierSaveReturnsWhileUploaderIsStalled() async throws {
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "A1 stalled uploader"
    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.captures.count == 1)
    // I3: nothing may be reported as synced before the server confirmed it.
    #expect(model.captures.first?.status != .done)
    #expect(model.queueSummary.done == 0)

    // Prove the upload really is in flight and really is stalled — i.e. the save
    // returned across a live, never-completing network call rather than skipping it.
    await apiClient.waitForSubmitArrival(count: 1)
    #expect(model.captures.first?.status == .uploading)
    #expect(await apiClient.submitCount == 1)

    await apiClient.openGate()
    await model.waitForPendingSyncQuiescence()
    #expect(model.captures.first?.status == .done)
}

// MARK: - A2: the editor gate is not wired to queue state

@Test
func verifierCanEditDraftIgnoresQueueOwnedState() throws {
    let source = try verifierSourceText("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift")
    let declaration = try #require(source.range(of: "private var canEditDraft: Bool {"))
    let bodyEnd = try #require(source.range(of: "}", range: declaration.upperBound..<source.endIndex))
    let body = String(source[declaration.upperBound..<bodyEnd.lowerBound])

    #expect(body.contains("canSubmitCaptures"))
    #expect(body.contains("isSavingDraft"))
    #expect(body.contains("isImportingPhotos"))
    // Queue-owned state must not appear anywhere in the gate.
    #expect(!body.contains("isSyncing"))
    #expect(!body.contains("queueSummary"))
    #expect(!body.contains("captures"))
    #expect(!body.contains("pendingSync"))
}

// Behavioural half of A2: every model-owned input to `canEditDraft` stays permissive
// while a saved batch is still uploading.
@Test(.timeLimit(.minutes(1))) @MainActor
func verifierEditorInputsStayPermissiveWhileBatchIsUploading() async throws {
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "A2 first capture"
    #expect(await model.saveQuickObservation())
    await apiClient.waitForSubmitArrival(count: 1)

    // The batch is genuinely mid-flight...
    #expect(model.isSyncing)
    #expect(model.captures.first?.status == .uploading)
    // ...and the editor's only model-side input is still true.
    #expect(model.canSubmitCaptures)
    // The draft field is writable and a second save is accepted (see A3/A4).
    model.observationText = "A2 typed while uploading"
    #expect(model.observationText == "A2 typed while uploading")

    await apiClient.openGate()
    await model.waitForPendingSyncQuiescence()
}

// MARK: - A3 / A4 / A9 / I2: coalescing, exactly-once, quiescence ordering

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierSecondSaveIsPickedUpByInFlightDrainExactlyOnce() async throws {
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "A3 first"
    #expect(await model.saveQuickObservation())
    await apiClient.waitForSubmitArrival(count: 1)
    let firstID = try #require(model.captures.first?.captureID)

    // Second save arrives while the first upload is provably in flight.
    model.observationText = "A3 second"
    #expect(await model.saveQuickObservation())
    #expect(model.captures.count == 2)
    let secondID = try #require(model.captures.last?.captureID)
    #expect(firstID != secondID)
    #expect(model.captures.last?.status == .pending)

    // Discriminator: if the second save had started its OWN drain instead of being
    // coalesced, that drain would run on the MainActor as soon as we yield, and it
    // records its arrival BEFORE blocking on the gate. Still 1 => coalesced.
    await yieldRepeatedly()
    #expect(await apiClient.submitCount == 1)
    #expect(model.captures.last?.status == .pending)

    await apiClient.openGate()
    // waitForPendingSyncQuiescence must NOT start a drain; the only way the second
    // capture can finish here is if the already-running drain picked it up (A4),
    // and it must be finished before quiescence is reported (A9).
    await model.waitForPendingSyncQuiescence()

    #expect(model.captures.count == 2)
    #expect(model.captures.allSatisfy { $0.status == .done })
    #expect(model.queueSummary.done == 2)
    #expect(model.queueSummary.pending == 0)

    // I2 / A3: exactly one submission per capture, IDs unchanged.
    let submitted = await apiClient.submittedCaptureIDs
    #expect(submitted.count == 2)
    #expect(Set(submitted) == Set([firstID, secondID]))
    #expect(model.captures.map(\.captureID) == [firstID, secondID])
}

// I2: an explicit sync request landing on an in-flight drain must not duplicate work.
@Test(.timeLimit(.minutes(1))) @MainActor
func verifierConcurrentSyncRequestDoesNotResubmit() async throws {
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "I2 single submission"
    #expect(await model.saveQuickObservation())
    await apiClient.waitForSubmitArrival(count: 1)

    await model.syncPending()
    // Documented behaviour: when a drain is already running, `syncPending()` only
    // coalesces a request and returns; it does NOT await the in-flight upload.
    #expect(model.captures.first?.status == .uploading)
    await model.syncPending()

    await apiClient.openGate()
    await model.waitForPendingSyncQuiescence()

    #expect(await apiClient.submitCount == 1)
    #expect(model.captures.first?.status == .done)
}

// MARK: - A5 / I1: handed-off media survives the editor reset

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierSavedMediaSurvivesUntilServerConfirmation() async throws {
    let mediaStore = makeVerifierMediaStore()
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient, mediaStore: mediaStore)
    let photo = try makeManagedPhoto(in: mediaStore)
    let photoPath = try #require(photo.fileURL).path

    model.observationText = "A5 photo capture"
    #expect(await model.saveQuickObservation(photos: [photo]))

    // A5: immediately after save returns (the point at which the editor drops its
    // references) the file is still on disk and still owned by the capture.
    #expect(FileManager.default.fileExists(atPath: photoPath))
    #expect(model.captures.first?.photos.first?.fileURL == photo.fileURL)

    // I1: still present while the upload is unconfirmed.
    await apiClient.waitForSubmitArrival(count: 1)
    #expect(FileManager.default.fileExists(atPath: photoPath))
    #expect(model.captures.first?.status == .uploading)

    await apiClient.openGate()
    await model.waitForPendingSyncQuiescence()

    // Release happens only after the server confirmed.
    #expect(model.captures.first?.status == .done)
    #expect(model.captures.first?.photos.first?.fileURL == nil)
    #expect(!FileManager.default.fileExists(atPath: photoPath))
}

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierMediaSurvivesFailedUpload() async throws {
    let mediaStore = makeVerifierMediaStore()
    let apiClient = GatedSubmitAPIClient(
        outcome: .failure(.serverStatus(status: 422, code: "rejected", message: "Rejected by backend")),
        gateOpen: true
    )
    let model = await makeOnlineVerifierModel(apiClient: apiClient, mediaStore: mediaStore)
    let photo = try makeManagedPhoto(in: mediaStore)
    let photoPath = try #require(photo.fileURL).path

    model.observationText = "I1 failing upload"
    #expect(await model.saveQuickObservation(photos: [photo]))
    await model.waitForPendingSyncQuiescence()

    #expect(model.captures.first?.status == .failed)
    #expect(FileManager.default.fileExists(atPath: photoPath))
    #expect(model.captures.first?.photos.first?.fileURL == photo.fileURL)
}

// A5, view-side: the reset block must not delete media.
@Test
func verifierEditorResetDoesNotDeleteHandedOffMedia() throws {
    let source = try verifierSourceText("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift")
    let saveRange = try #require(source.range(of: "let didSave = await model.saveQuickObservation("))
    let tail = source[saveRange.upperBound...]
    let blockEnd = try #require(tail.range(of: "recorder.clear()"))
    let resetBlock = String(tail[tail.startIndex..<blockEnd.upperBound])

    #expect(resetBlock.contains("pendingPhotos.removeAll"))
    #expect(resetBlock.contains("pendingAudios.removeAll"))
    #expect(!resetBlock.contains("deleteMedia"))
    #expect(!resetBlock.contains("deletePendingMedia"))
    #expect(!resetBlock.contains("removeItem"))
    #expect(!resetBlock.contains("releaseManagedMedia"))
}

// MARK: - A6: local persistence failure rolls back and enqueues nothing

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierLocalPersistenceFailureRollsBackAndEnqueuesNothing() async throws {
    let apiClient = GatedSubmitAPIClient(gateOpen: true)
    let model = await makeOnlineVerifierModel(
        apiClient: apiClient,
        store: VerifierFailingSaveStore(snapshot: verifierSnapshot())
    )
    let sitesBeforeSave = model.sites
    let capturesBeforeSave = model.captures

    model.observationText = "A6 doomed save"
    let didSave = await model.saveQuickObservation()

    #expect(didSave == false)
    #expect(model.captures == capturesBeforeSave)
    #expect(model.captures.isEmpty)
    #expect(model.sites == sitesBeforeSave)
    #expect(model.statusMessage == "Could not save locally. Try again.")

    // Nothing enqueued: no drain, no upload, even after quiescence.
    await model.waitForPendingSyncQuiescence()
    #expect(await apiClient.submitCount == 0)
    #expect(model.isSyncing == false)
    #expect(model.queueSummary.pending == 0)
}

// MARK: - A7: offline short-circuits

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierOfflineSaveAttemptsNoDrain() async throws {
    let apiClient = GatedSubmitAPIClient(gateOpen: true)
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot()),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: makeVerifierMediaStore()
    )
    await model.load()
    await tokenStore.saveToken("verifier-token", accountID: model.account.id)
    #expect(model.isOfflineMode)

    model.observationText = "A7 offline save"
    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.captures.first?.status == .pending)
    #expect(model.statusMessage == "Saved offline. Captures will sync when connection returns.")
    #expect(model.isSyncing == false)

    await model.waitForPendingSyncQuiescence()
    #expect(await apiClient.submitCount == 0)
    #expect(model.captures.first?.status == .pending)
}

// MARK: - A8: quiescence is an observer, never a trigger

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierQuiescenceReturnsPromptlyAndStartsNoUpload() async throws {
    let pending = LocalCapture(
        captureID: "verifier-capture-pending",
        jobID: "verifier-job-pending",
        visitID: nil,
        siteID: "site_sandy_sandbox",
        siteLabel: "Sandy Sandbox",
        targetID: "site_sandy_sandbox",
        qcCategory: "general_note",
        note: "A8 already queued",
        capturedAt: .now,
        exportedAt: .now,
        status: .pending
    )
    let apiClient = GatedSubmitAPIClient(gateOpen: true)
    let model = await makeOnlineVerifierModel(
        apiClient: apiClient,
        store: MemoryFieldCaptureStore(snapshot: verifierSnapshot(captures: [pending]))
    )

    #expect(model.queueSummary.pending == 1)
    #expect(model.isSyncing == false)

    await model.waitForPendingSyncQuiescence()

    // Returned, and did not become a sync trigger.
    #expect(await apiClient.submitCount == 0)
    #expect(model.captures.first?.status == .pending)
    #expect(model.isSyncing == false)

    // Called twice in a row it is still inert.
    await model.waitForPendingSyncQuiescence()
    #expect(await apiClient.submitCount == 0)
}

// MARK: - I3: queue state stays truthful

@Test(.timeLimit(.minutes(1))) @MainActor
func verifierNoCaptureIsLeftUploadingAfterQuiescence() async throws {
    let apiClient = GatedSubmitAPIClient(
        outcome: .failure(.serverStatus(status: 503, code: "unavailable", message: "Backend down")),
        gateOpen: true
    )
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "I3 first"
    #expect(await model.saveQuickObservation())
    model.observationText = "I3 second"
    #expect(await model.saveQuickObservation())

    await model.waitForPendingSyncQuiescence()

    #expect(model.captures.count == 2)
    #expect(model.captures.allSatisfy { $0.status != .uploading })
    #expect(model.captures.allSatisfy { $0.status != .done })
    #expect(model.queueSummary.done == 0)
    #expect(model.isSyncing == false)
}

// Observation (not an acceptance criterion): `isSyncing` is set inside the detached
// drain, so it is still false at the instant `saveQuickObservation` returns even
// though a drain is already scheduled. Guards keyed on `isSyncing` (retryCapture,
// the Sync buttons) are briefly not armed in that window.
@Test(.timeLimit(.minutes(1))) @MainActor
func verifierIsSyncingIsNotYetSetWhenSaveReturns() async throws {
    let apiClient = GatedSubmitAPIClient()
    let model = await makeOnlineVerifierModel(apiClient: apiClient)

    model.observationText = "isSyncing window probe"
    #expect(await model.saveQuickObservation())
    let syncingImmediatelyAfterSave = model.isSyncing

    await apiClient.waitForSubmitArrival(count: 1)
    #expect(model.isSyncing)

    await apiClient.openGate()
    await model.waitForPendingSyncQuiescence()
    #expect(model.isSyncing == false)

    // Recorded, not asserted as required behaviour.
    #expect(syncingImmediatelyAfterSave == false)
}

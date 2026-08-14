import Foundation
import Testing
@testable import BTQFieldCaptureApp

// Independent verification of the scene-phase `.background` change:
//
//     let activeUploadCount = model.queueSummary.pending + model.queueSummary.uploading
//     backgroundSyncScheduler.beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {
//         await model.syncPending()
//         await model.waitForPendingSyncQuiescence()
//     }
//
// Nothing here reuses any other verifier's stubs or assertions. There are no sleeps:
// every wait is a continuation that some other participant explicitly resumes, or an
// `await` on a Task whose completion is the thing under test.
//
// SCOPE NOTE (read this before trusting any assertion below): the real collaborator
// `IOSBackgroundSyncScheduler` is `#if os(iOS)` and takes a UIKit background-task
// assertion, so it cannot be executed from this macOS test target. These tests
// therefore drive a *hand-copied* stand-in (`FakeExpiringScheduler`) that reproduces
// the two behaviours of the real one that the claims depend on: the
// `guard pendingCount > 0` early return, and "the assertion ends as soon as
// `operation()` returns". They prove the model-side semantics of the operation
// closure. They do NOT prove UIKit assertion bookkeeping.

// MARK: - Controllable uploader

private enum ParkedSubmitOutcome: Sendable {
    case success
    case failure(any Error)
}

/// Parks every `submit` on its own continuation until the test releases it, and lets
/// the test await the *arrival* of the Nth submit so "an upload is genuinely in
/// flight" is observed, never guessed.
private actor ParkingSubmitAPIClient: CaptureAPIClient {
    private(set) var submittedCaptureIDs: [String] = []
    private var parked: [CheckedContinuation<Void, Never>] = []
    private var releaseBudget: Int
    private var arrivalWaiters: [(threshold: Int, continuation: CheckedContinuation<Void, Never>)] = []
    private let outcome: ParkedSubmitOutcome

    init(outcome: ParkedSubmitOutcome = .success, releaseBudget: Int = 0) {
        self.outcome = outcome
        self.releaseBudget = releaseBudget
    }

    var submitCount: Int { submittedCaptureIDs.count }
    var hasDuplicateSubmissions: Bool { Set(submittedCaptureIDs).count != submittedCaptureIDs.count }

    func session(baseURL: URL, token: String) async throws -> BTQSession { .demo }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submittedCaptureIDs.append(capture.captureID)
        resumeArrivalWaiters()
        if releaseBudget > 0 {
            releaseBudget -= 1
        } else {
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                parked.append(continuation)
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

    /// Lets `count` more submits through — those already parked first, then future ones.
    func release(_ count: Int = 1) {
        var remaining = count
        while remaining > 0, !parked.isEmpty {
            parked.removeFirst().resume()
            remaining -= 1
        }
        releaseBudget += remaining
    }

    func releaseEverything() {
        let waiters = parked
        parked = []
        releaseBudget += 1_000
        for waiter in waiters { waiter.resume() }
    }

    /// Suspends until at least `count` submits have *started*.
    func waitForSubmitArrival(count: Int) async {
        if submittedCaptureIDs.count >= count { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            arrivalWaiters.append((count, continuation))
        }
    }

    private func resumeArrivalWaiters() {
        let reached = submittedCaptureIDs.count
        let ready = arrivalWaiters.filter { $0.threshold <= reached }
        arrivalWaiters.removeAll { $0.threshold <= reached }
        for waiter in ready { waiter.continuation.resume() }
    }
}

// MARK: - Controllable store (lets a test observe the durable-write window)

private actor ParkingFieldCaptureStore: FieldCaptureStore {
    private var snapshot: FieldCaptureSnapshot
    private(set) var saveCount = 0
    /// Parks the first `save` whose snapshot is "every capture uploaded and done".
    /// That is precisely the drain's post-upload persist, and never the save path's
    /// own persist (which always writes a `.pending` capture) — so the park point is
    /// selected by state, not by timing.
    private let parkOnFullyDrainedSnapshot: Bool
    private var hasParkedOnce = false
    private var parked: CheckedContinuation<Void, Never>?
    private var parkedWaiters: [CheckedContinuation<Void, Never>] = []

    init(
        snapshot: FieldCaptureSnapshot = FieldCaptureSnapshot(
            account: .defaultProduction,
            session: .demo,
            sites: BTQSession.demo.sites
        ),
        parkOnFullyDrainedSnapshot: Bool = false
    ) {
        self.snapshot = snapshot
        self.parkOnFullyDrainedSnapshot = parkOnFullyDrainedSnapshot
    }

    func load() async throws -> FieldCaptureSnapshot { snapshot }

    func save(_ snapshot: FieldCaptureSnapshot) async throws {
        saveCount += 1
        let isFullyDrained = !snapshot.captures.isEmpty
            && snapshot.captures.allSatisfy { $0.status == .done }
        if parkOnFullyDrainedSnapshot, !hasParkedOnce, isFullyDrained {
            hasParkedOnce = true
            await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
                parked = continuation
                let waiters = parkedWaiters
                parkedWaiters = []
                for waiter in waiters { waiter.resume() }
            }
        }
        self.snapshot = snapshot
    }

    var persistedCaptures: [LocalCapture] { snapshot.captures }

    func waitUntilParked() async {
        if parked != nil { return }
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            parkedWaiters.append(continuation)
        }
    }

    func releaseParkedSave() {
        let continuation = parked
        parked = nil
        continuation?.resume()
    }
}

// MARK: - Stand-in for IOSBackgroundSyncScheduler

/// Reproduces the two behaviours of `IOSBackgroundSyncScheduler` the claims depend on:
/// the `guard pendingCount > 0` early return, and ending the assertion the moment
/// `operation()` returns. Everything is touched on the MainActor.
private final class FakeExpiringScheduler: BackgroundSyncScheduling, @unchecked Sendable {
    nonisolated(unsafe) private(set) var scheduledPendingCounts: [Int] = []
    nonisolated(unsafe) private(set) var backgroundTaskRequestsSubmitted = 0
    nonisolated(unsafe) private(set) var assertionsBegun = 0
    nonisolated(unsafe) private(set) var assertionsEnded = 0
    nonisolated(unsafe) private(set) var lastOperation: Task<Void, Never>?

    /// Mirrors `IOSBackgroundSyncScheduler.scheduleSyncIfNeeded`: records the argument the
    /// caller passed, and applies the same `guard pendingCount > 0` before "submitting"
    /// the BGProcessingTaskRequest — so a test can assert the request really was made,
    /// not merely that a call happened.
    func scheduleSyncIfNeeded(pendingCount: Int) {
        scheduledPendingCounts.append(pendingCount)
        guard pendingCount > 0 else { return }
        backgroundTaskRequestsSubmitted += 1
    }

    @MainActor
    func beginExpiringSyncIfNeeded(
        pendingCount: Int,
        operation: @escaping @MainActor @Sendable () async -> Void
    ) {
        guard pendingCount > 0 else { return }
        assertionsBegun += 1
        lastOperation = Task { @MainActor in
            await operation()
            self.assertionsEnded += 1
        }
    }

    var assertionIsHeld: Bool { assertionsBegun > assertionsEnded }
}

// MARK: - Helpers

/// Replica of the `.background` branch in `BTQFieldCaptureRootView`. That branch lives
/// inside a SwiftUI `body`, so it cannot be invoked directly and must be mirrored here.
///
/// A hand-copied replica silently rots. The drift guard below checks this function and the
/// real source against one shared fragment list, in both directions, so editing either
/// without the other fails the suite. If you change this function, change
/// `backgroundBranchFragments` — and re-check every assertion that reads an argument value.
@MainActor
private func simulateScenePhaseBackground(
    model: FieldCaptureModel,
    scheduler: FakeExpiringScheduler
) {
    let activeUploadCount = model.backgroundSyncWorkCount
    scheduler.scheduleSyncIfNeeded(pendingCount: activeUploadCount)
    scheduler.beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {
        await model.syncPending()
        await model.waitForPendingSyncQuiescence()
    }
}

// MARK: - Drift guard
//
// `simulateScenePhaseBackground` is a hand-copy of code that lives inside a SwiftUI
// `body` and therefore cannot be called. Hand-copies rot: production moves, the copy
// does not, and the suite keeps certifying behaviour that no longer exists. (That
// happened in this very file: the replica kept `scheduleSyncIfNeeded(pendingCount:
// model.queueSummary.pending)` after production hoisted the count, and the suite stayed
// green while asserting the bug.)
//
// The guard below is BIDIRECTIONAL. One fragment list is checked, in order, against both
// the real `.background` branch AND the replica's own source text, so drift on either
// side fails. `driftGuardDetectsHistoricalWirings` proves the matcher actually bites.

private func sourceText(relativePath: String) throws -> String {
    let url = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()   // BTQFieldCaptureAppTests
        .deletingLastPathComponent()   // Tests
        .deletingLastPathComponent()   // package root
        .appendingPathComponent(relativePath)
    return try String(contentsOf: url, encoding: .utf8)
}

/// Slices `text` from the first occurrence of `start` up to the next `end`.
private func slice(_ text: String, from start: String, to end: String) -> String? {
    guard let startRange = text.range(of: start),
          let endRange = text.range(of: end, range: startRange.upperBound..<text.endIndex) else {
        return nil
    }
    return String(text[startRange.lowerBound..<endRange.lowerBound])
}

/// The wiring the replica models, written so each fragment appears verbatim in BOTH the
/// production branch and the replica. (Receiver names differ — `backgroundSyncScheduler`
/// vs `scheduler` — so the fragments start at the method name.)
private let backgroundBranchFragments = [
    "let activeUploadCount = model.backgroundSyncWorkCount",
    "scheduleSyncIfNeeded(pendingCount: activeUploadCount)",
    "beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {",
    "await model.syncPending()",
    "await model.waitForPendingSyncQuiescence()",
]

/// Wirings this file's negative controls assert are GONE from production. Each was, at
/// some point, the shipped `.background` branch; each is now known to leave a window
/// where in-flight or unpersisted work runs with no UIKit assertion.
private let retiredBackgroundBranchFragments = [
    // Original: pending-only, and no wait for quiescence.
    "scheduleSyncIfNeeded(pendingCount: model.queueSummary.pending)",
    "beginExpiringSyncIfNeeded(pendingCount: model.queueSummary.pending)",
    // Interim: count assembled inline from queueSummary, which reads 0 during the
    // drain's final persist. Superseded by `model.backgroundSyncWorkCount`.
    "let activeUploadCount = model.queueSummary.pending + model.queueSummary.uploading",
]

/// Returns a human-readable reason when `branch` does not match the modelled wiring.
private func backgroundBranchDriftReason(in branch: String) -> String? {
    var searchFrom = branch.startIndex
    for fragment in backgroundBranchFragments {
        guard let found = branch.range(of: fragment, range: searchFrom..<branch.endIndex) else {
            return "missing (or out of order): \"\(fragment)\""
        }
        searchFrom = found.upperBound
    }
    for retired in retiredBackgroundBranchFragments where branch.contains(retired) {
        return "retired wiring is back: \"\(retired)\""
    }
    return nil
}

@Test func replicaOfRootViewBackgroundBranchIsInSyncWithSource() throws {
    let source = try sourceText(
        relativePath: "Sources/BTQFieldCaptureApp/Views/BTQFieldCaptureRootView.swift"
    )
    guard let branch = slice(source, from: "case .background:", to: "case .inactive:") else {
        Issue.record("""
        REPLICA DRIFT: could not find the `case .background:` ... `case .inactive:` branch in \
        BTQFieldCaptureRootView.swift. The scene-phase handler was restructured, so \
        `simulateScenePhaseBackground` in this file is modelling code that no longer exists. \
        Re-sync the replica before trusting any background-assertion test here.
        """)
        return
    }
    if let reason = backgroundBranchDriftReason(in: branch) {
        Issue.record("""
        REPLICA DRIFT (production side): the `.background` branch of BTQFieldCaptureRootView.swift \
        no longer matches what this file models — \(reason).
        `simulateScenePhaseBackground` is a hand-copy of that branch, so every background-assertion \
        test in this file is now testing the replica instead of production and proves nothing. \
        Re-sync `simulateScenePhaseBackground` AND `backgroundBranchFragments`, then re-check every \
        assertion that depends on the argument values. Actual branch:
        \(branch)
        """)
    }
}

@Test func replicaHelperItselfStillMatchesTheModelledWiring() throws {
    // The other half of the loop: catches the replica being edited away from the fragment
    // list (which is how this file drifted in the first place).
    let ownSource = try sourceText(
        relativePath: "Tests/BTQFieldCaptureAppTests/BackgroundAssertionCoverageVerifierTests.swift"
    )
    guard let body = slice(ownSource, from: "private func simulateScenePhaseBackground(", to: "\n}\n") else {
        Issue.record("REPLICA DRIFT: could not locate `simulateScenePhaseBackground` in this file.")
        return
    }
    if let reason = backgroundBranchDriftReason(in: body) {
        Issue.record("""
        REPLICA DRIFT (test side): `simulateScenePhaseBackground` no longer matches \
        `backgroundBranchFragments` — \(reason). The replica and the wiring it claims to mirror \
        have diverged; re-sync them and re-run `replicaOfRootViewBackgroundBranchIsInSyncWithSource`.
        """)
    }
}

@Test func driftGuardDetectsHistoricalWirings() {
    // Proof the matcher bites rather than passing on anything. These are the two real
    // wirings this branch has had, frozen as text.
    let originalPreChangeBranch = """
    case .background:
        backgroundSyncScheduler.scheduleSyncIfNeeded(pendingCount: model.queueSummary.pending)
        backgroundSyncScheduler.beginExpiringSyncIfNeeded(pendingCount: model.queueSummary.pending) {
            await model.syncPending()
        }
    """
    let halfFixedBranch = """
    case .background:
        backgroundSyncScheduler.scheduleSyncIfNeeded(pendingCount: model.queueSummary.pending)
        let activeUploadCount = model.queueSummary.pending + model.queueSummary.uploading
        backgroundSyncScheduler.beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {
            await model.syncPending()
            await model.waitForPendingSyncQuiescence()
        }
    """
    let inlineQueueSummaryBranch = """
    case .background:
        let activeUploadCount = model.queueSummary.pending + model.queueSummary.uploading
        backgroundSyncScheduler.scheduleSyncIfNeeded(pendingCount: activeUploadCount)
        backgroundSyncScheduler.beginExpiringSyncIfNeeded(pendingCount: activeUploadCount) {
            await model.syncPending()
            await model.waitForPendingSyncQuiescence()
        }
    """
    #expect(backgroundBranchDriftReason(in: originalPreChangeBranch) != nil)
    // The half-fixed wiring is the one that slipped past this file before: every fragment
    // present, but the retired pending-only `scheduleSyncIfNeeded` still there.
    #expect(backgroundBranchDriftReason(in: halfFixedBranch) != nil)
    // The interim wiring: correct-looking, but reads 0 during the drain's final persist.
    #expect(backgroundBranchDriftReason(in: inlineQueueSummaryBranch) != nil)
}

/// Gives every already-runnable MainActor continuation a chance to run. Anything still
/// unfinished afterwards is blocked on a continuation nobody has resumed.
private func drainRunnableWork(_ iterations: Int = 200) async {
    for _ in 0..<iterations { await Task.yield() }
}

@MainActor
private func makeOnlineModel(
    apiClient: any CaptureAPIClient,
    store: any FieldCaptureStore = MemoryFieldCaptureStore(),
    mediaStore: LocalMediaStore = LocalMediaStore()
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
    await model.handleConnectivityChange(.satisfied)
    return model
}

// MARK: - C5: the save path is still not gated on the network

@Test @MainActor func saveReturnsWhileTheUploadIsStillParkedOnTheNetwork() async {
    let apiClient = ParkingSubmitAPIClient()
    let store = ParkingFieldCaptureStore()
    let model = await makeOnlineModel(apiClient: apiClient, store: store)

    model.observationText = "save must not wait for the network"
    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    // The capture is durably written before save returns.
    let persisted = await store.persistedCaptures
    #expect(persisted.contains { $0.note == "save must not wait for the network" })

    // And the upload really is still in flight, unfinished, at the moment save returned.
    await apiClient.waitForSubmitArrival(count: 1)
    let count = await apiClient.submitCount
    #expect(count == 1)
    #expect(model.captures.first?.status == .uploading)

    await apiClient.releaseEverything()
    await model.waitForPendingSyncQuiescence()
}

// MARK: - C1: the assertion must outlive the in-flight drain

@Test @MainActor func syncPendingAloneReturnsWhileADrainIsStillInFlight() async {
    // This is the failure mode the added `waitForPendingSyncQuiescence()` exists to
    // close: `syncPending()` on its own is not a wait.
    let apiClient = ParkingSubmitAPIClient()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "drain in flight"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)

    await model.syncPending() // returns even though the upload is parked

    #expect(model.captures.first?.status == .uploading)
    let count = await apiClient.submitCount
    #expect(count == 1)

    await apiClient.releaseEverything()
    await model.waitForPendingSyncQuiescence()
}

@Test @MainActor func backgroundAssertionIsHeldUntilTheInFlightDrainFinishes() async {
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "background mid-upload"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)
    #expect(model.captures.first?.status == .uploading)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)

    #expect(scheduler.assertionsBegun == 1)
    await drainRunnableWork()
    #expect(scheduler.assertionsEnded == 0)   // still held while the upload is parked
    #expect(scheduler.assertionIsHeld)

    await apiClient.releaseEverything()
    await scheduler.lastOperation?.value

    #expect(scheduler.assertionsEnded == 1)
    #expect(model.captures.first?.status == .done)
    #expect(!model.captures.contains { $0.status == .uploading || $0.status == .pending })
}

@Test @MainActor func backgroundAssertionAlsoCoversWorkCoalescedIntoTheRunningDrain() async {
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "capture A"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)

    // A second capture saved while A is uploading coalesces into the running drain.
    model.observationText = "capture B"
    _ = await model.saveQuickObservation()
    #expect(model.queueSummary.pending == 1)
    #expect(model.queueSummary.uploading == 1)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 1)

    // Finish A only. The coalesced follow-up (B) must keep the assertion held.
    await apiClient.release(1)
    await apiClient.waitForSubmitArrival(count: 2)
    await drainRunnableWork()
    #expect(scheduler.assertionsEnded == 0)
    #expect(model.captures.contains { $0.note == "capture B" && $0.status == .uploading })

    await apiClient.releaseEverything()
    await scheduler.lastOperation?.value

    #expect(scheduler.assertionsEnded == 1)
    #expect(model.captures.filter { $0.status == .done }.count == 2)

    // I2: exactly one submission per capture, no duplicate from the coalesced request.
    let submitCount = await apiClient.submitCount
    let duplicated = await apiClient.hasDuplicateSubmissions
    #expect(submitCount == 2)
    #expect(!duplicated)
}

// MARK: - C2: with queued work but no drain, the background path starts one

@Test @MainActor func backgroundStartsADrainWhenNoneIsInFlight() async {
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    // Save while offline so no drain is spawned, then come back online without
    // going through the connectivity path (which would start one itself).
    model.isOfflineMode = true
    model.observationText = "queued while offline"
    _ = await model.saveQuickObservation()
    model.isOfflineMode = false

    await drainRunnableWork()
    let submitsBefore = await apiClient.submitCount
    #expect(submitsBefore == 0)              // no drain in flight
    #expect(model.queueSummary.pending == 1)
    #expect(model.isSyncing == false)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 1)

    await apiClient.waitForSubmitArrival(count: 1)   // a drain really was started
    await drainRunnableWork()
    #expect(scheduler.assertionsEnded == 0)

    await apiClient.releaseEverything()
    await scheduler.lastOperation?.value
    #expect(scheduler.assertionsEnded == 1)
    #expect(model.captures.first?.status == .done)
}

// MARK: - C3: what the guard actually sees

@Test @MainActor func uploadingOnlyQueueTakesAnAssertionAndSchedulesABackgroundTask() async {
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "sole capture, mid-upload"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)

    // The exact state the change targets: in flight, nothing merely queued.
    #expect(model.queueSummary.pending == 0)
    #expect(model.queueSummary.uploading == 1)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)

    // The expiring UIKit assertion is taken...
    #expect(scheduler.assertionsBegun == 1)
    // ...and the BGProcessingTask is requested with a non-zero count, so an upload that
    // outlives the assertion still has a scheduled retry. Under the old pending-only
    // argument this was [0] and `guard pendingCount > 0` dropped the request entirely.
    #expect(scheduler.scheduledPendingCounts == [1])
    #expect(scheduler.backgroundTaskRequestsSubmitted == 1)

    await apiClient.releaseEverything()
    await scheduler.lastOperation?.value
}

@Test @MainActor func assertionIsHeldThroughTheFinalQueueStatePersist() async {
    // This assertion was FLIPPED (prompt 125). It previously documented a real gap: the
    // drain's last upload is confirmed, every capture is `.done`, and the queue state is
    // still being written to disk — so `queueSummary.pending + .uploading` reads 0 and
    // `guard pendingCount > 0` skipped the assertion. Suspending there made a successful
    // upload read back as a failed capture on next launch. `model.backgroundSyncWorkCount`
    // closes it by reporting 1 whenever a drain task exists.
    let apiClient = ParkingSubmitAPIClient(releaseBudget: 10)   // uploads never park
    let store = ParkingFieldCaptureStore(parkOnFullyDrainedSnapshot: true)
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient, store: store)

    model.observationText = "persist window"
    _ = await model.saveQuickObservation()
    await store.waitUntilParked()   // the drain is now suspended inside persist()

    #expect(model.captures.first?.status == .done)
    #expect(model.isSyncing == true)                 // a drain IS still running
    // The queue itself looks idle — this is exactly why the old inline count failed here.
    #expect(model.queueSummary.pending == 0)
    #expect(model.queueSummary.uploading == 0)
    // ...and this is the signal that rescues it.
    #expect(model.backgroundSyncWorkCount == 1)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 1)          // the durable write is now covered
    #expect(scheduler.backgroundTaskRequestsSubmitted == 1)

    // And it is HELD until the write completes, not released on entry.
    await drainRunnableWork()
    #expect(scheduler.assertionsEnded == 0)
    #expect(scheduler.assertionIsHeld)

    await store.releaseParkedSave()
    await scheduler.lastOperation?.value
    #expect(scheduler.assertionsEnded == 1)
    #expect(model.isSyncing == false)
}

// MARK: - Negative controls (prove the assertions above are not vacuous)

/// The *historical* `.background` branch, before the nonblocking-save follow-up. This is
/// deliberately NOT a mirror of current source — it is frozen wiring, kept so the tests
/// above can be shown to distinguish old behaviour from new. `replicaOfRootViewBackgroundBranchIsInSyncWithSource`
/// asserts this shape is absent from production; do not "fix" it to match the source.
@MainActor
private func simulateHistoricalPreChangeScenePhaseBackground(
    model: FieldCaptureModel,
    scheduler: FakeExpiringScheduler
) {
    scheduler.scheduleSyncIfNeeded(pendingCount: model.queueSummary.pending)
    scheduler.beginExpiringSyncIfNeeded(pendingCount: model.queueSummary.pending) {
        await model.syncPending()
    }
}

@Test @MainActor func controlPreChangeBackgroundBranchTakesNoAssertionMidUpload() async {
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "sole capture, mid-upload"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)

    simulateHistoricalPreChangeScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 0)                  // the bug the change fixes
    #expect(scheduler.backgroundTaskRequestsSubmitted == 0)  // and no BG retry was scheduled

    // Same state, current wiring: both are now taken.
    let fixedScheduler = FakeExpiringScheduler()
    simulateScenePhaseBackground(model: model, scheduler: fixedScheduler)
    #expect(fixedScheduler.assertionsBegun == 1)
    #expect(fixedScheduler.backgroundTaskRequestsSubmitted == 1)

    await apiClient.releaseEverything()
    await model.waitForPendingSyncQuiescence()
}

@Test @MainActor func controlPreChangeBackgroundBranchReleasesTheAssertionEarly() async {
    // Two captures: one uploading, one pending, so the old pending-only count is > 0
    // and an assertion *is* taken — and then released while the upload is still in
    // flight, because `syncPending()` alone does not wait.
    let apiClient = ParkingSubmitAPIClient()
    let scheduler = FakeExpiringScheduler()
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "capture A"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)
    model.observationText = "capture B"
    _ = await model.saveQuickObservation()

    simulateHistoricalPreChangeScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 1)

    await drainRunnableWork()
    #expect(scheduler.assertionsEnded == 1)              // released early...
    #expect(model.captures.contains { $0.status == .uploading })  // ...mid-upload

    await apiClient.releaseEverything()
    await model.waitForPendingSyncQuiescence()
}

// MARK: - A capture stranded in `.uploading` is not rescued by the background path

@Test @MainActor func backgroundPathTakesAnAssertionForAStrandedUploadingCaptureButCannotAdvanceIt() async {
    // Reachable state: the process died mid-upload after some other code path had
    // persisted the queue, and the app relaunched inside the 120s
    // `recoverInterruptedUploads` window, so the capture is still `.uploading`.
    let stranded = LocalCapture(
        captureID: "capture-stranded",
        jobID: "job-stranded",
        visitID: nil,
        siteID: BTQSession.demo.sites[0].siteID,
        siteLabel: BTQSession.demo.sites[0].label,
        targetID: BTQSession.demo.sites[0].siteID,
        qcCategory: "general_note",
        note: "stranded mid-upload",
        capturedAt: .now,
        exportedAt: .now,
        status: .uploading,
        lastTriedAt: .now
    )
    let apiClient = ParkingSubmitAPIClient(releaseBudget: 10)
    let scheduler = FakeExpiringScheduler()
    let store = MemoryFieldCaptureStore(
        snapshot: FieldCaptureSnapshot(
            account: .defaultProduction,
            session: .demo,
            sites: BTQSession.demo.sites,
            captures: [stranded]
        )
    )
    let model = await makeOnlineModel(apiClient: apiClient, store: store)

    #expect(model.queueSummary.uploading == 1)
    #expect(model.queueSummary.pending == 0)

    simulateScenePhaseBackground(model: model, scheduler: scheduler)
    #expect(scheduler.assertionsBegun == 1)      // assertion is taken...
    await scheduler.lastOperation?.value

    let submitted = await apiClient.submitCount
    #expect(submitted == 0)                      // ...but the drain cannot touch it
    #expect(model.captures.first?.status == .uploading)
}

@Test @MainActor func savingWhileADrainIsInFlightWritesAnUploadingCaptureToDisk() async {
    // Shows the stranded-`.uploading` state above is reachable, not hypothetical: a
    // save that lands mid-drain persists the in-flight capture with status `.uploading`.
    let apiClient = ParkingSubmitAPIClient()
    let store = ParkingFieldCaptureStore()
    let model = await makeOnlineModel(apiClient: apiClient, store: store)

    model.observationText = "capture A"
    _ = await model.saveQuickObservation()
    await apiClient.waitForSubmitArrival(count: 1)

    model.observationText = "capture B"
    _ = await model.saveQuickObservation()

    let persisted = await store.persistedCaptures
    #expect(persisted.contains { $0.status == .uploading })

    await apiClient.releaseEverything()
    await model.waitForPendingSyncQuiescence()
}

// MARK: - I1 / I3

@Test @MainActor func localMediaSurvivesAFailedUploadAndIsOnlyReleasedAfterConfirmation() async {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-verifier-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    // Failure case: permanent server rejection must not delete local media.
    let failingClient = ParkingSubmitAPIClient(
        outcome: .failure(CaptureAPIError.serverStatus(status: 400, code: "bad_request", message: "nope")),
        releaseBudget: 10
    )
    let failingModel = await makeOnlineModel(apiClient: failingClient, mediaStore: mediaStore)
    let failedPhotoURL = mediaStore.mediaDirectory(bucketID: "bucket-fail")
        .appendingPathComponent("photo-fail.jpg")
    try? FileManager.default.createDirectory(
        at: failedPhotoURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try? Data("jpeg-bytes".utf8).write(to: failedPhotoURL)

    failingModel.observationText = "failing upload keeps media"
    _ = await failingModel.saveQuickObservation(
        photos: [CapturePhoto(filename: "photo-fail.jpg", fileURL: failedPhotoURL)]
    )
    await failingModel.waitForPendingSyncQuiescence()

    #expect(failingModel.captures.first?.status == .failed)
    #expect(FileManager.default.fileExists(atPath: failedPhotoURL.path))
    #expect(failingModel.captures.first?.photos.first?.fileURL == failedPhotoURL)
    #expect(!failingModel.captures.contains { $0.status == .uploading })

    // Success case: media released only after a confirmed submit.
    let okClient = ParkingSubmitAPIClient()
    let okModel = await makeOnlineModel(apiClient: okClient, mediaStore: mediaStore)
    let okPhotoURL = mediaStore.mediaDirectory(bucketID: "bucket-ok")
        .appendingPathComponent("photo-ok.jpg")
    try? FileManager.default.createDirectory(
        at: okPhotoURL.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try? Data("jpeg-bytes".utf8).write(to: okPhotoURL)

    okModel.observationText = "media released only on success"
    _ = await okModel.saveQuickObservation(
        photos: [CapturePhoto(filename: "photo-ok.jpg", fileURL: okPhotoURL)]
    )
    await okClient.waitForSubmitArrival(count: 1)

    // Mid-flight, before the server confirmed: media must still be on disk.
    #expect(FileManager.default.fileExists(atPath: okPhotoURL.path))

    await okClient.releaseEverything()
    await okModel.waitForPendingSyncQuiescence()

    #expect(okModel.captures.first?.status == .done)
    #expect(!FileManager.default.fileExists(atPath: okPhotoURL.path))
}

@Test @MainActor func transientFailureLeavesCapturesPendingNotUploading() async {
    let apiClient = ParkingSubmitAPIClient(
        outcome: .failure(URLError(.notConnectedToInternet)),
        releaseBudget: 10
    )
    let model = await makeOnlineModel(apiClient: apiClient)

    model.observationText = "transient failure"
    _ = await model.saveQuickObservation()
    await model.waitForPendingSyncQuiescence()

    #expect(!model.captures.contains { $0.status == .uploading })
    #expect(model.isSyncing == false)
}

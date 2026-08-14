import Foundation
import Testing
@testable import BTQFieldCaptureApp

// Independent verification of the media-ownership fix (the field incident where a
// saved capture's voice note was deleted by the editor's "recorder cleared" handler
// and 18 photos were stranded behind the resulting "Missing audio file" error).
//
// Nothing here reuses the implementer's assertions. Every claim is driven through the
// real `LocalMediaStore` on a real temp directory and the real `FieldCaptureModel`.
// No sleeps: every wait is `waitForPendingSyncQuiescence()` or a direct `await`.
//
// Where a guarantee lives in SwiftUI view code that cannot be driven from a unit test,
// the behavioural test is paired with a contiguous source pin over the whole decision
// block, so a reordering that reintroduces the bug breaks the pin.

// MARK: - Fixtures

private func makeTempRoot() -> URL {
    let root = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-ownership-\(UUID().uuidString)", isDirectory: true)
    try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
    return root
}

private func writeFile(_ url: URL, _ contents: String) {
    try? FileManager.default.createDirectory(
        at: url.deletingLastPathComponent(),
        withIntermediateDirectories: true
    )
    try? Data(contents.utf8).write(to: url)
}

private func exists(_ url: URL?) -> Bool {
    guard let url else { return false }
    return FileManager.default.fileExists(atPath: url.path)
}

private enum OwnershipVerifierStoreError: Error {
    case saveFailed
}

/// Loads a seeded snapshot but can never write. Used to prove that ownership is
/// persisted *before* any file is removed.
private actor NeverPersistsFieldCaptureStore: FieldCaptureStore {
    private let snapshot: FieldCaptureSnapshot

    init(snapshot: FieldCaptureSnapshot) {
        self.snapshot = snapshot
    }

    func load() async throws -> FieldCaptureSnapshot { snapshot }

    func save(_ snapshot: FieldCaptureSnapshot) async throws {
        throw OwnershipVerifierStoreError.saveFailed
    }
}

/// Records the exact multipart body the server would receive, built at submit time
/// while the evidence files are still on disk.
private actor MultipartRecordingAPIClient: CaptureAPIClient {
    private(set) var submittedCaptures: [LocalCapture] = []
    private(set) var submittedBodies: [Data] = []

    func session(baseURL: URL, token: String) async throws -> BTQSession { .demo }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submittedCaptures.append(capture)
        submittedBodies.append((try? MultipartCaptureBuilder.body(for: capture, boundary: "verifier")) ?? Data())
        return SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audioAttachments.count,
            idempotentReplay: false
        )
    }
}

@MainActor
private func makeModel(
    store: any FieldCaptureStore = MemoryFieldCaptureStore(),
    apiClient: any CaptureAPIClient = MockCaptureAPIClient(),
    mediaStore: LocalMediaStore,
    online: Bool = true
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
    await tokenStore.saveToken("ownership-verifier-token", accountID: model.account.id)
    if online {
        await model.handleConnectivityChange(.satisfied)
    }
    return model
}

private func seededSnapshot(captures: [LocalCapture]) -> FieldCaptureSnapshot {
    FieldCaptureSnapshot(
        account: .defaultProduction,
        session: .demo,
        sites: BTQSession.demo.sites,
        captures: captures
    )
}

private func demoSiteID() -> String { BTQSession.demo.sites[0].siteID }

// MARK: - C1: the reported bug

@Test @MainActor func savingACaptureWithAVoiceNoteLeavesTheAudioOnDiskAndTheCaptureUploads() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)
    let api = MultipartRecordingAPIClient()
    // Offline first, so the save window can be inspected before any upload runs.
    let model = await makeModel(apiClient: api, mediaStore: mediaStore, online: false)

    let photoURL = mediaStore.mediaDirectory(bucketID: "visit-1").appendingPathComponent("photo-1.jpg")
    let audioURL = mediaStore.mediaDirectory(bucketID: "visit-1").appendingPathComponent("voice-1.m4a")
    writeFile(photoURL, "photo-evidence-bytes-1")
    writeFile(audioURL, "voice-evidence-bytes-1")
    let photo = CapturePhoto(filename: "photo-1.jpg", fileURL: photoURL)
    let audio = CaptureAudio(filename: "voice-1.m4a", fileURL: audioURL, durationSeconds: 12)

    model.observationText = "scuffing by the loading dock"
    let didSave = await model.saveQuickObservation(photos: [photo], audios: [audio])
    #expect(didSave)
    #expect(model.captures.contains { $0.status == .pending })

    // Step 3 of the field incident, verbatim: the editor's recorder-cleared handler
    // asks the media store to drop the audio it no longer holds in `pendingAudios`.
    mediaStore.deletePendingMedia(
        photos: [],
        audio: audio,
        preservingMediaOwnedBy: model.persistedCapturesOwningMedia
    )

    #expect(exists(audioURL))
    #expect(exists(photoURL))

    await model.handleConnectivityChange(.satisfied)
    await model.waitForPendingSyncQuiescence()

    #expect(model.captures.first?.status == .done)
    #expect(model.captures.first?.lastError == nil)

    let bodies = await api.submittedBodies
    #expect(bodies.count == 1)
    let wire = String(decoding: bodies.first ?? Data(), as: UTF8.self)
    // The evidence bytes actually reached the wire, byte-identical.
    #expect(wire.contains("voice-evidence-bytes-1"))
    #expect(wire.contains("photo-evidence-bytes-1"))
    #expect(wire.contains("name=\"audio\"; filename=\"voice-1.m4a\""))
}

// MARK: - C2: ownership is a media-store fact, not an editor guess

@Test func theMediaStoreRefusesToDeleteAFileAPersistedCaptureOwns() throws {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let store = LocalMediaStore(rootDirectory: root)

    let photoURL = store.mediaDirectory(bucketID: "visit-9").appendingPathComponent("owned.jpg")
    let audioURL = store.mediaDirectory(bucketID: "visit-9").appendingPathComponent("owned.m4a")
    writeFile(photoURL, "owned-photo")
    writeFile(audioURL, "owned-audio")
    let photo = CapturePhoto(filename: "owned.jpg", fileURL: photoURL)
    let audio = CaptureAudio(filename: "owned.m4a", fileURL: audioURL, durationSeconds: 4)

    let owner = LocalCapture(
        captureID: "capture-owner",
        jobID: "job-owner",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "owns the media",
        capturedAt: .now,
        exportedAt: .now,
        status: .pending,
        photos: [photo],
        audios: [audio]
    )

    // A: a pending-media deletion request naming the same files is refused.
    store.deletePendingMedia(photos: [photo], audio: audio, preservingMediaOwnedBy: [owner])
    #expect(exists(photoURL))
    #expect(exists(audioURL))

    // B: a whole-capture deletion of a *different* capture that happens to reference
    //    the same files is refused too (the check is per file, not per capture).
    var doppelganger = owner
    doppelganger.captureID = "capture-other"
    store.deleteMedia(for: [doppelganger], preservingMediaOwnedBy: [owner])
    #expect(exists(photoURL))
    #expect(exists(audioURL))

    // C: ownership survives a non-standardized URL spelling on the owner's side.
    var oddlySpelled = owner
    oddlySpelled.photos = [
        CapturePhoto(
            filename: "owned.jpg",
            fileURL: store.mediaDirectory(bucketID: "visit-9")
                .appendingPathComponent(".")
                .appendingPathComponent("owned.jpg")
        )
    ]
    oddlySpelled.audios = []
    oddlySpelled.audio = nil
    store.deletePendingMedia(photos: [photo], preservingMediaOwnedBy: [oddlySpelled])
    #expect(exists(photoURL))

    // MUTATION CONTROL: with no owners declared, the identical calls destroy the files.
    // Without this leg the assertions above could pass on a store that never deletes.
    store.deletePendingMedia(photos: [photo], audio: audio, preservingMediaOwnedBy: [])
    #expect(!exists(photoURL))
    #expect(!exists(audioURL))
}

@Test @MainActor func ownershipSpansEveryAccountWorkspaceNotJustTheActiveOne() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    let otherAccount = BTQAccount(label: "Second Crew", baseURL: URL(string: "https://fc.gregstoltz.com")!)
    let otherPhotoURL = mediaStore.mediaDirectory(bucketID: "other").appendingPathComponent("other.jpg")
    writeFile(otherPhotoURL, "other-account-photo")
    let otherPhoto = CapturePhoto(filename: "other.jpg", fileURL: otherPhotoURL)
    let otherCapture = LocalCapture(
        captureID: "capture-other-account",
        jobID: "job-other-account",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "belongs to the other crew",
        capturedAt: .now,
        exportedAt: .now,
        status: .pending,
        photos: [otherPhoto]
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: .demo,
        sites: BTQSession.demo.sites,
        captures: [],
        activeAccountID: BTQAccount.defaultProduction.id,
        accountWorkspaces: [
            BTQAccountWorkspace(
                account: .defaultProduction,
                session: .demo,
                sites: BTQSession.demo.sites,
                captures: []
            ),
            BTQAccountWorkspace(account: otherAccount, captures: [otherCapture]),
        ]
    )
    let model = await makeModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        mediaStore: mediaStore,
        online: false
    )

    let owners = model.persistedCapturesOwningMedia
    #expect(owners.contains { $0.captureID == "capture-other-account" })
    // The active workspace must not be double counted through `accountWorkspaces`.
    #expect(owners.filter { $0.captureID == "capture-other-account" }.count == 1)

    mediaStore.deletePendingMedia(photos: [otherPhoto], preservingMediaOwnedBy: owners)
    #expect(exists(otherPhotoURL))

    // MUTATION CONTROL: the file really is deletable; only ownership saved it.
    mediaStore.deletePendingMedia(photos: [otherPhoto], preservingMediaOwnedBy: [])
    #expect(!exists(otherPhotoURL))
}

@Test @MainActor func captureDeletionKeepsTheMediaWhenTheOwnershipWriteFails() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    let photoURL = mediaStore.mediaDirectory(bucketID: "fail").appendingPathComponent("keep.jpg")
    writeFile(photoURL, "must-survive")
    let capture = LocalCapture(
        captureID: "capture-persist-fails",
        jobID: "job-persist-fails",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "persist fails",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        lastError: "Previous failure",
        photos: [CapturePhoto(filename: "keep.jpg", fileURL: photoURL)]
    )
    let model = await makeModel(
        store: NeverPersistsFieldCaptureStore(snapshot: seededSnapshot(captures: [capture])),
        mediaStore: mediaStore,
        online: false
    )

    await model.deleteCapture("capture-persist-fails")

    #expect(model.captures.contains { $0.captureID == "capture-persist-fails" })
    #expect(exists(photoURL))
    #expect(model.statusMessage == "Could not remove the capture. Its media was kept.")
}

@Test @MainActor func draftRemovalKeepsTheMediaWhenTheOwnershipWriteFails() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    let photoURL = mediaStore.mediaDirectory(bucketID: "fail-draft").appendingPathComponent("draft.jpg")
    writeFile(photoURL, "draft-must-survive")
    let draft = LocalCapture(
        captureID: "capture-draft-persist-fails",
        jobID: "job-draft-persist-fails",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "draft persist fails",
        capturedAt: .now,
        exportedAt: .now,
        status: .draft,
        photos: [CapturePhoto(filename: "draft.jpg", fileURL: photoURL)]
    )
    let model = await makeModel(
        store: NeverPersistsFieldCaptureStore(snapshot: seededSnapshot(captures: [draft])),
        mediaStore: mediaStore,
        online: false
    )
    model.selectSite(id: demoSiteID())

    let removed = await model.removeDraftCapture()

    #expect(removed == false)
    #expect(model.captures.contains { $0.captureID == "capture-draft-persist-fails" })
    #expect(exists(photoURL))
    #expect(model.statusMessage == "Could not remove the saved draft. Its media was kept.")
}

// MARK: - C3: a genuine discard must still clean up

@Test @MainActor func aGenuineDiscardStillDeletesItsOwnMedia() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)
    let model = await makeModel(mediaStore: mediaStore, online: false)
    model.selectSite(id: demoSiteID())

    let photoURL = mediaStore.mediaDirectory(bucketID: "discard").appendingPathComponent("discard.jpg")
    let audioURL = mediaStore.mediaDirectory(bucketID: "discard").appendingPathComponent("discard.m4a")
    writeFile(photoURL, "discard-photo")
    writeFile(audioURL, "discard-audio")
    let photo = CapturePhoto(filename: "discard.jpg", fileURL: photoURL)
    let audio = CaptureAudio(filename: "discard.m4a", fileURL: audioURL, durationSeconds: 3)

    let persisted = await model.upsertDraftCapture(photos: [photo], audios: [audio])
    #expect(persisted)
    #expect(model.captures.contains { $0.status == .draft })
    // While the draft owns them, the files are protected.
    mediaStore.deletePendingMedia(
        photos: [photo],
        audio: audio,
        preservingMediaOwnedBy: model.persistedCapturesOwningMedia
    )
    #expect(exists(photoURL))
    #expect(exists(audioURL))

    // Operator discards the draft: ownership goes away and the media must go with it.
    let removed = await model.removeDraftCapture()
    #expect(removed)
    #expect(!model.captures.contains { $0.status == .draft })
    #expect(!exists(photoURL))
    #expect(!exists(audioURL))

    // And media the editor created that never became a capture is still collectable.
    let orphanURL = mediaStore.mediaDirectory(bucketID: "discard").appendingPathComponent("orphan.jpg")
    writeFile(orphanURL, "orphan-photo")
    mediaStore.deletePendingMedia(
        photos: [CapturePhoto(filename: "orphan.jpg", fileURL: orphanURL)],
        preservingMediaOwnedBy: model.persistedCapturesOwningMedia
    )
    #expect(!exists(orphanURL))
}

// MARK: - C4 / C5: "Upload Remaining" on already-stranded data

@Test @MainActor func alreadyStrandedCaptureUploadsSurvivingPhotosAndRecordsTheLoss() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    // The operator's phone, as it is right now: 18 photos on disk, the voice note gone,
    // the capture already persisted in `.failed` with the missing-audio error.
    var photos: [CapturePhoto] = []
    var photoURLs: [URL] = []
    for index in 0..<18 {
        let url = mediaStore.mediaDirectory(bucketID: "stranded")
            .appendingPathComponent("stranded-\(index).jpg")
        writeFile(url, "stranded-photo-bytes-\(index)")
        photoURLs.append(url)
        photos.append(CapturePhoto(filename: "stranded-\(index).jpg", fileURL: url))
    }
    let lostAudioURL = mediaStore.mediaDirectory(bucketID: "stranded")
        .appendingPathComponent("voice-note.m4a")
    #expect(!exists(lostAudioURL))
    let stranded = LocalCapture(
        captureID: "capture-stranded",
        jobID: "job-stranded",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "Entry mats soaked, mopped and fanned.",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        attempts: 1,
        lastError: "Missing audio file: voice-note.m4a",
        photos: photos,
        audios: [CaptureAudio(filename: "voice-note.m4a", fileURL: lostAudioURL, durationSeconds: 41)]
    )

    let api = MultipartRecordingAPIClient()
    let model = await makeModel(
        store: MemoryFieldCaptureStore(snapshot: seededSnapshot(captures: [stranded])),
        apiClient: api,
        mediaStore: mediaStore
    )

    // The capture came from persisted state, not from a fresh capture session.
    #expect(model.captures.count == 1)
    #expect(model.captures.first?.captureID == "capture-stranded")
    #expect(model.captures.first?.status == .failed)

    // (a) The operator is shown exactly what is lost, and reading it changes nothing.
    let before = model.captures
    let description = model.missingMediaRecoveryDescription(for: model.captures[0])
    #expect(model.captures == before)
    let shown = description ?? ""
    #expect(shown.contains("voice-note.m4a"))
    #expect(shown.contains("18 surviving photos"))
    #expect(shown.contains("0 surviving voice memos"))
    #expect(shown.localizedCaseInsensitiveContains("permanent media-loss note"))

    await model.uploadSurvivingMedia(for: "capture-stranded")
    await model.waitForPendingSyncQuiescence()

    // The same capture ID went up: no re-capture, no new record.
    let submitted = await api.submittedCaptures
    #expect(submitted.count == 1)
    #expect(submitted.first?.captureID == "capture-stranded")
    #expect(submitted.first?.photos.count == 18)
    #expect(submitted.first?.audioAttachments.isEmpty == true)
    #expect(model.captures.first?.status == .done)

    // (b) The loss is visible on the server side, in the record itself.
    let noteSent = submitted.first?.note ?? ""
    #expect(noteSent.contains("Entry mats soaked"))
    #expect(noteSent.contains("Media loss before upload"))
    #expect(noteSent.contains("voice-note.m4a"))

    let bodies = await api.submittedBodies
    let wire = String(decoding: bodies.first ?? Data(), as: UTF8.self)
    #expect(wire.contains("name=\"note\""))
    #expect(wire.contains("Media loss before upload"))
    // The QC record cannot claim a voice memo it does not carry.
    #expect(!wire.contains("name=\"audio\";"))
    #expect(wire.contains("\"has_audio\":false"))
    #expect(wire.contains("\"audio_count\":0"))
    #expect(wire.contains("\"photo_count\":18"))
    // I4: the stored evidence bytes went up unchanged, all 18 of them.
    for index in 0..<18 {
        #expect(wire.contains("stranded-photo-bytes-\(index)"))
    }
    // I1: the surviving files were only released after the server confirmed.
    for url in photoURLs {
        #expect(!exists(url))
    }
}

/// Builds a failed capture whose photo file has moved (the persisted URL is stale but the
/// file is still findable under the media root) and whose voice note is genuinely gone.
@MainActor
private func makeRelocatedPhotoScenario(
    mediaStore: LocalMediaStore,
    audioIsGone: Bool
) -> (capture: LocalCapture, realPhotoURL: URL, stalePhotoURL: URL) {
    let realPhotoURL = mediaStore.mediaDirectory(bucketID: "relocated").appendingPathComponent("p0.jpg")
    writeFile(realPhotoURL, "recoverable-photo-bytes")
    let stalePhotoURL = mediaStore.mediaDirectory(bucketID: "original").appendingPathComponent("p0.jpg")

    let audioURL = mediaStore.mediaDirectory(bucketID: "relocated").appendingPathComponent("voice-note.m4a")
    let staleAudioURL = mediaStore.mediaDirectory(bucketID: "original").appendingPathComponent("voice-note.m4a")
    if !audioIsGone {
        writeFile(audioURL, "recoverable-audio-bytes")
    }

    let capture = LocalCapture(
        captureID: "capture-relocated",
        jobID: "job-relocated",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "Grout haze along the west corridor.",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        attempts: 1,
        lastError: "Missing photo file: p0.jpg",
        photos: [CapturePhoto(filename: "p0.jpg", fileURL: stalePhotoURL)],
        audios: [CaptureAudio(filename: "voice-note.m4a", fileURL: staleAudioURL, durationSeconds: 41)]
    )
    return (capture, realPhotoURL, stalePhotoURL)
}

@Test @MainActor func theLossTheOperatorConfirmsIsTheLossActuallyTaken() async {
    // D1 regression. The confirmation used to be computed from unrepaired file URLs while
    // the action was computed from repaired ones, so a capture whose photos were all
    // recoverable told the operator "0 surviving photos ... will be uploaded". In the
    // field that reads as "everything is gone" and the operator cancels.
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)
    let scenario = makeRelocatedPhotoScenario(mediaStore: mediaStore, audioIsGone: true)
    #expect(!exists(scenario.stalePhotoURL))
    #expect(exists(scenario.realPhotoURL))

    let api = MultipartRecordingAPIClient()
    let model = await makeModel(
        store: MemoryFieldCaptureStore(snapshot: seededSnapshot(captures: [scenario.capture])),
        apiClient: api,
        mediaStore: mediaStore
    )

    let shown = model.missingMediaRecoveryDescription(for: model.captures[0]) ?? ""
    // The recovery affordance is still offered: something really is missing.
    #expect(!shown.isEmpty)
    // ...but only the voice memo is reported lost. The relocated photo is not.
    #expect(shown.contains("voice memo \"voice-note.m4a\""))
    #expect(!shown.contains("photo \"p0.jpg\""))
    #expect(shown.contains("1 surviving photo"))

    await model.uploadSurvivingMedia(for: "capture-relocated")
    await model.waitForPendingSyncQuiescence()

    let submitted = await api.submittedCaptures
    #expect(submitted.count == 1)
    let uploadedPhotoCount = submitted.first?.photos.count ?? -1
    #expect(uploadedPhotoCount == 1)
    // ANTI-DRIFT: the number the operator confirmed must be the number that went up.
    // Computed from the observed upload, not hard coded, so the two cannot diverge again.
    #expect(shown.contains("\(uploadedPhotoCount) surviving photo"))
    #expect(submitted.first?.audioAttachments.isEmpty == true)
    #expect(submitted.first?.note.contains("voice-note.m4a") == true)
    #expect(submitted.first?.note.contains("1 surviving photo") == true)

    let wire = String(decoding: await api.submittedBodies.first ?? Data(), as: UTF8.self)
    #expect(wire.contains("recoverable-photo-bytes"))
    #expect(!wire.contains("name=\"audio\";"))
    #expect(model.captures.first?.status == .done)
    // I1: the repaired file is still recognised as managed, so it is released on success
    // rather than leaking (`standardizedFileURL` normalises the symlinked prefix on both
    // sides of the managed-path and ownership checks).
    #expect(!exists(scenario.realPhotoURL))
}

@Test @MainActor func aCaptureWhoseMediaIsOnlyMisplacedIsRepairedRatherThanAnnotated() async {
    // The branch introduced alongside the D1 fix: every file is rediscovered after repair,
    // so nothing was actually lost and no loss note may be written.
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)
    let scenario = makeRelocatedPhotoScenario(mediaStore: mediaStore, audioIsGone: false)

    let api = MultipartRecordingAPIClient()
    let model = await makeModel(
        store: MemoryFieldCaptureStore(snapshot: seededSnapshot(captures: [scenario.capture])),
        apiClient: api,
        mediaStore: mediaStore
    )

    let shown = model.missingMediaRecoveryDescription(for: model.captures[0]) ?? ""
    #expect(shown.contains("All saved media is available"))
    #expect(shown.localizedCaseInsensitiveContains("no media-loss note will be added"))

    await model.uploadSurvivingMedia(for: "capture-relocated")

    let recovered = model.captures.first
    #expect(recovered?.note == "Grout haze along the west corridor.")
    #expect(recovered?.photos.count == 1)
    #expect(recovered?.audioAttachments.count == 1)
    // The repaired reference points at the rediscovered file. Compared through
    // `resolvingSymlinksInPath` because the directory enumerator hands back the
    // symlink-resolved spelling (/private/var/... on macOS) while the seeded URL keeps
    // the /var/... spelling. `URL` equality is textual; the media store's own checks use
    // `standardizedFileURL`, which unifies the two, so this is a test-side comparison
    // detail and not a store-side gap.
    #expect(
        recovered?.photos.first?.fileURL?.resolvingSymlinksInPath()
            == scenario.realPhotoURL.resolvingSymlinksInPath()
    )
    #expect(recovered?.status == .failed)
    let submitted = await api.submittedCaptures
    #expect(submitted.isEmpty)
    // The repair is persisted, so the row now offers a plain retry instead.
    #expect(model.missingMediaRecoveryDescription(for: recovered!) == nil)
}

@Test @MainActor func theRecoveryPathCannotBeTriggeredWithoutRealMissingMedia() async {
    let root = makeTempRoot()
    defer { try? FileManager.default.removeItem(at: root) }
    let mediaStore = LocalMediaStore(rootDirectory: root)

    let intactURL = mediaStore.mediaDirectory(bucketID: "intact").appendingPathComponent("intact.jpg")
    writeFile(intactURL, "intact-photo")
    let intactPhoto = CapturePhoto(filename: "intact.jpg", fileURL: intactURL)

    let failedButIntact = LocalCapture(
        captureID: "capture-intact",
        jobID: "job-intact",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "server rejected this once",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        attempts: 1,
        lastError: "Server error 500",
        photos: [intactPhoto]
    )
    let pendingWithMissingMedia = LocalCapture(
        captureID: "capture-pending-missing",
        jobID: "job-pending-missing",
        visitID: nil,
        siteID: demoSiteID(),
        siteLabel: "Site One",
        targetID: demoSiteID(),
        qcCategory: "general_note",
        note: "not failed yet",
        capturedAt: .now,
        exportedAt: .now,
        status: .pending,
        photos: [CapturePhoto(
            filename: "gone.jpg",
            fileURL: mediaStore.mediaDirectory(bucketID: "intact").appendingPathComponent("gone.jpg")
        )]
    )

    let api = MultipartRecordingAPIClient()
    let model = await makeModel(
        store: MemoryFieldCaptureStore(
            snapshot: seededSnapshot(captures: [failedButIntact, pendingWithMissingMedia])
        ),
        apiClient: api,
        mediaStore: mediaStore,
        online: false
    )

    // No missing media -> no recovery affordance at all.
    #expect(model.missingMediaRecoveryDescription(for: failedButIntact) == nil)
    // Not failed -> no recovery affordance, even though a file really is gone.
    #expect(model.missingMediaRecoveryDescription(for: pendingWithMissingMedia) == nil)

    // Invoking it anyway must not strip media or annotate the record.
    await model.uploadSurvivingMedia(for: "capture-intact")
    let intactAfter = model.captures.first { $0.captureID == "capture-intact" }
    #expect(intactAfter?.photos.count == 1)
    #expect(intactAfter?.note == "server rejected this once")
    #expect(intactAfter?.status == .failed)
    #expect(model.statusMessage == "All saved media is available. Retry the capture instead.")

    await model.uploadSurvivingMedia(for: "capture-pending-missing")
    let pendingAfter = model.captures.first { $0.captureID == "capture-pending-missing" }
    #expect(pendingAfter?.photos.count == 1)
    #expect(pendingAfter?.note == "not failed yet")

    // And it is refused outright while a sync is in flight.
    model.isSyncing = true
    await model.uploadSurvivingMedia(for: "capture-intact")
    #expect(model.statusMessage == "Wait for sync to finish before recovering this capture.")
    model.isSyncing = false

    let submitted = await api.submittedCaptures
    #expect(submitted.isEmpty)
}

// MARK: - C6: the Part 4 audit, checked rather than trusted

private func sourceFile(_ relativePath: String) throws -> String {
    try String(
        contentsOf: ownershipPackageRoot().appendingPathComponent(relativePath),
        encoding: .utf8
    )
}

private func ownershipPackageRoot() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
}

/// Returns each `call(` argument list in `source`, paren-balanced.
private func argumentLists(of call: String, in source: String) -> [String] {
    var lists: [String] = []
    var searchRange = source.startIndex..<source.endIndex
    while let found = source.range(of: call, range: searchRange) {
        var depth = 0
        var index = source.index(before: found.upperBound) // the opening paren
        var end: String.Index?
        while index < source.endIndex {
            let character = source[index]
            if character == "(" { depth += 1 }
            if character == ")" {
                depth -= 1
                if depth == 0 {
                    end = index
                    break
                }
            }
            index = source.index(after: index)
        }
        guard let end else { break }
        lists.append(String(source[found.upperBound..<end]))
        searchRange = source.index(after: end)..<source.endIndex
    }
    return lists
}

@Test func everyMediaDeletionCallSiteInSourcesDeclaresItsOwners() throws {
    let sourceRoot = ownershipPackageRoot().appendingPathComponent("Sources", isDirectory: true)
    let enumerator = try #require(FileManager.default.enumerator(
        at: sourceRoot,
        includingPropertiesForKeys: nil
    ))

    var checkedCallSites = 0
    var offenders: [String] = []
    for case let url as URL in enumerator where url.pathExtension == "swift" {
        let source = try String(contentsOf: url, encoding: .utf8)
        // Skip the declarations themselves inside LocalMediaStore.
        for call in ["mediaStore.deletePendingMedia(", "mediaStore.deleteMedia("] {
            for arguments in argumentLists(of: call, in: source) {
                checkedCallSites += 1
                if !arguments.contains("preservingMediaOwnedBy") {
                    offenders.append("\(url.lastPathComponent): \(call)\(arguments))")
                }
            }
        }
    }

    // Mutation control: an empty or broken scan must not be able to pass.
    #expect(checkedCallSites >= 12)
    #expect(offenders.isEmpty, "media deletion without declared owners: \(offenders)")
}

@Test func theOnlyOwnershipOptOutInSourcesIsTheDocumentedAccountRemoval() throws {
    let sourceRoot = ownershipPackageRoot().appendingPathComponent("Sources", isDirectory: true)
    let enumerator = try #require(FileManager.default.enumerator(
        at: sourceRoot,
        includingPropertiesForKeys: nil
    ))

    var anonymousOptOuts: [String] = []
    for case let url as URL in enumerator where url.pathExtension == "swift" {
        let source = try String(contentsOf: url, encoding: .utf8)
        for call in ["mediaStore.deletePendingMedia(", "mediaStore.deleteMedia("] {
            for arguments in argumentLists(of: call, in: source) {
                // An owner set spelled as a bare literal is an undocumented opt-out.
                if arguments.contains("preservingMediaOwnedBy: []") {
                    anonymousOptOuts.append("\(url.lastPathComponent): \(call)")
                }
            }
        }
    }
    #expect(anonymousOptOuts.isEmpty, "undocumented ownership opt-out: \(anonymousOptOuts)")

    // The one deliberate opt-out names itself and states why it is safe.
    let model = try sourceFile("Sources/BTQFieldCaptureApp/Stores/FieldCaptureModel.swift")
    #expect(model.contains(contiguousBlock([
        "            // Account removal is blocked while any capture is not done, and done captures have",
        "            // already released managed references. The empty owner set is therefore intentional.",
        "            let noRetainedMediaOwnersAfterGuardedAccountRemoval: [LocalCapture] = []",
        "            mediaStore.deleteMedia(",
        "                for: removedWorkspace.captures,",
        "                preservingMediaOwnedBy: noRetainedMediaOwnersAfterGuardedAccountRemoval",
        "            )",
    ])))
    // The guard that opt-out depends on must stay upstream of it.
    #expect(model.contains("guard queuedCaptureCount == 0 else {"))

    // The entry points must not hand out a default owner set again.
    let mediaStore = try sourceFile("Sources/BTQFieldCaptureApp/Services/LocalMediaStore.swift")
    #expect(!mediaStore.contains("preservingMediaOwnedBy captures: [LocalCapture] = []"))
    #expect(!mediaStore.contains("preservingMediaOwnedBy retainedCaptures: [LocalCapture] = []"))
    #expect(mediaStore.contains("preservingMediaOwnedBy captures: [LocalCapture]"))
    #expect(mediaStore.contains("preservingMediaOwnedBy retainedCaptures: [LocalCapture]"))
}

@Test func theRecoveryDescriptionAndTheRecoveryActionShareOneRepairedView() throws {
    // D1 anti-drift, at the structural level: both the sentence the operator confirms and
    // the mutation it authorises must derive from `repairedMissingMedia`.
    let model = try sourceFile("Sources/BTQFieldCaptureApp/Stores/FieldCaptureModel.swift")

    let describe = try #require(
        argumentLists(of: "repairedMissingMedia(", in: model).first,
        "missingMediaRecoveryDescription must consult the repaired view"
    )
    #expect(describe.contains("for: capture") || describe.contains("for: previousCapture"))
    // Both call sites exist: the description and the action.
    #expect(argumentLists(of: "= repairedMissingMedia(", in: model).count == 2)
    #expect(model.contains("let recovery = repairedMissingMedia(for: capture)"))
    #expect(model.contains("let recovery = repairedMissingMedia(for: previousCapture)"))
    // The surviving summary shown to the operator is taken from the repaired capture,
    // never from the stale one.
    #expect(model.contains("survivingMediaSummary(for: recovery.capture, excluding: missing)"))
    #expect(!model.contains("survivingMediaSummary(for: capture, excluding: missing)"))

    // The description is now cached per capture value rather than recomputed on every
    // render. Retry must not be offered off an unpopulated cache, or a capture with
    // missing media would show the button that re-fails it instead of the recovery.
    let queueView = try sourceFile("Sources/BTQFieldCaptureApp/Views/QueueView.swift")
    #expect(queueView.contains(contiguousBlock([
        "        capture.status == .failed",
        "            && recoveryDescriptionSource == capture",
        "            && missingMediaRecoveryDescription == nil",
    ])))
    // The cache is keyed on the capture value, so any change to it re-derives.
    #expect(queueView.contains(contiguousBlock([
        "        .task(id: capture) {",
        "            guard recoveryDescriptionSource != capture else { return }",
        "            missingMediaRecoveryDescription = model.missingMediaRecoveryDescription(for: capture)",
        "            recoveryDescriptionSource = capture",
        "        }",
    ])))
}

/// Joins exact source lines so a pin stays one contiguous region and cannot be
/// silently weakened by being split into disjoint slices.
private func contiguousBlock(_ lines: [String]) -> String {
    lines.joined(separator: "\n")
}

@Test func discardPendingMediaRemovesOwnershipBeforeItRemovesFiles() throws {
    let captureView = try sourceFile("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift")

    // One contiguous pin over the whole discard decision. Deleting the files before
    // the draft capture stops owning them would have to break this string.
    #expect(captureView.contains(contiguousBlock([
        "        let photosToDelete = pendingPhotos",
        "        let audiosToDelete = pendingAudios",
        "        let recorderAudioToDelete = recorder.lastAudio",
        "        pendingPhotos = []",
        "        pendingAudios = []",
        "        clearRecorder(intent: .discard)",
        "        Task {",
        "            _ = await model.removeDraftCapture(siteID: draftSiteID)",
        "            let owners = model.persistedCapturesOwningMedia",
        "            mediaStore.deletePendingMedia(",
        "                photos: photosToDelete,",
        "                audio: recorderAudioToDelete,",
        "                preservingMediaOwnedBy: owners",
        "            )",
        "            for audio in audiosToDelete {",
        "                mediaStore.deletePendingMedia(",
        "                    photos: [],",
        "                    audio: audio,",
        "                    preservingMediaOwnedBy: owners",
        "                )",
        "            }",
        "        }",
    ])))

    // The exact call shape that caused the field incident must never return.
    #expect(!captureView.contains("mediaStore.deletePendingMedia(photos: pendingPhotos, audio: recorder.lastAudio)"))
}

@Test func thePhotoAndVoiceRemovalPathsDeleteOnlyAfterASuccessfulPersist() throws {
    let captureView = try sourceFile("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift")

    // Per-photo trash can: persist the reduced draft, and only then drop the file.
    #expect(captureView.contains(contiguousBlock([
        "            guard didPersist else {",
        "                pendingPhotos = previousPhotos",
        "                return",
        "            }",
        "            mediaStore.deletePendingMedia(",
        "                photos: [photo],",
        "                preservingMediaOwnedBy: model.persistedCapturesOwningMedia",
        "            )",
    ])))

    // Voice-memo erase: same ordering, plus the recorder is restored on failure.
    #expect(captureView.contains(contiguousBlock([
        "            guard didPersist else {",
        "                pendingAudios = previousAudios",
        "                recorder.restore(audio: previousAudios.last)",
        "                return",
        "            }",
        "            mediaStore.deletePendingMedia(",
        "                photos: [],",
        "                audio: audio,",
        "                preservingMediaOwnedBy: model.persistedCapturesOwningMedia",
        "            )",
    ])))

    // A handoff (save) and a discard (erase) are distinguishable at the clear site.
    #expect(captureView.contains("private enum RecorderClearIntent"))
    #expect(captureView.contains(contiguousBlock([
        "            recordRecorderClearIntent(.handoff)",
        "            recorder.clear()",
    ])))
    #expect(captureView.contains("clearRecorder(intent: .discard)"))
    #expect(captureView.contains("recorderClearIntents[audio.id] = .discard"))
}

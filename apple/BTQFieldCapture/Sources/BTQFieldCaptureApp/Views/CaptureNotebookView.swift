import CoreTransferable
import PhotosUI
import SwiftUI
#if os(iOS)
import UIKit
#endif

private struct DraftContext: Equatable {
    let accountID: UUID
    let siteID: String?
}

private struct PickedPhotoFile: Transferable {
    let fileURL: URL

    static var transferRepresentation: some TransferRepresentation {
        FileRepresentation(importedContentType: .image) { received in
            let sourceExtension = received.file.pathExtension
            let fileExtension = sourceExtension.isEmpty ? "image" : sourceExtension
            let destination = FileManager.default.temporaryDirectory
                .appendingPathComponent("btq-picked-photo-\(UUID().uuidString).\(fileExtension)")
            try FileManager.default.copyItem(at: received.file, to: destination)
            return PickedPhotoFile(fileURL: destination)
        }
    }
}

struct CaptureNotebookView: View {
    @Bindable var model: FieldCaptureModel
    @State private var selectedPhotoItems: [PhotosPickerItem] = []
    @State private var pendingPhotos: [CapturePhoto] = []
    @State private var recorder = VoiceRecorder()
    @State private var showCamera = false
    @State private var cameraDraftContext: DraftContext?
    @State private var cameraMessage: String?
    @State private var isImportingPhotos = false
    @State private var photoImportMessage: String?
    @State private var isSavingDraft = false
    @State private var showingClearDraftMediaConfirmation = false
    private let mediaStore = LocalMediaStore()
    private let cameraPermissionChecker: any CameraCapturePermissionChecking

    init(
        model: FieldCaptureModel,
        cameraPermissionChecker: any CameraCapturePermissionChecking = SystemCameraCapturePermissionChecker()
    ) {
        self.model = model
        self.cameraPermissionChecker = cameraPermissionChecker
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                captureHeader
                statusHeader
                siteCard
                captureTools
                noteEditor
                AppVersionFooter()
            }
            .padding(.horizontal, 16)
            .padding(.top, 8)
            .padding(.bottom, 24)
            .frame(maxWidth: 760, alignment: .center)
            .frame(maxWidth: .infinity)
        }
        #if os(iOS)
        .safeAreaInset(edge: .bottom) {
            Color.clear
                .frame(height: 112)
                .allowsHitTesting(false)
        }
        #endif
        .navigationTitle(captureNavigationTitle)
        #if os(iOS)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar(.hidden, for: .navigationBar)
        #endif
        .toolbar {
            captureToolbar
        }
        .onChange(of: selectedPhotoItems) { _, items in
            let context = currentDraftContext
            Task { await loadPhotos(items, context: context) }
        }
        .onChange(of: pendingPhotos) { _, photos in
            guard !photos.isEmpty, !isSavingDraft else { return }
            Task { await persistActiveMediaDraft(photos: photos) }
        }
        .onChange(of: model.captures) { _, _ in
            restoreActiveDraftIfAvailable()
        }
        .onChange(of: model.observationText) { _, _ in
            Task { await persistActiveDraftIfPresent() }
        }
        .onChange(of: model.selectedCategoryValue) { _, _ in
            Task { await persistActiveDraftIfPresent() }
        }
        .onChange(of: model.selectedSiteID) { oldValue, newValue in
            guard oldValue != nil, oldValue != newValue else { return }
            discardDraftAfterSiteChange(previousSiteID: oldValue)
        }
        .onChange(of: model.account.id) { oldValue, newValue in
            guard oldValue != newValue else { return }
            discardDraftAfterAccountChange()
        }
        .onChange(of: model.canSubmitCaptures) { _, canSubmit in
            guard !canSubmit else { return }
            discardDraftAfterSubmitPermissionRevoked()
        }
        .task {
            restoreActiveDraftIfAvailable()
        }
        #if os(iOS)
        .sheet(isPresented: $showCamera) {
            CameraCaptureView { data in
                guard let context = cameraDraftContext, canAttachMedia(to: context) else { return }
                if let photo = savePhoto(data: data, prefix: "camera") {
                    await appendPhotoToDraft(photo, context: context)
                }
            }
            .ignoresSafeArea()
            .onDisappear {
                cameraDraftContext = nil
            }
        }
        #endif
        .confirmationDialog(
            "Clear pending media?",
            isPresented: $showingClearDraftMediaConfirmation,
            titleVisibility: .visible
        ) {
            Button("Clear Media", role: .destructive) {
                discardPendingMedia()
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes unsaved photos and voice memo from the current draft.")
        }
    }

    @ToolbarContentBuilder
    private var captureToolbar: some ToolbarContent {
        #if os(macOS)
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await model.syncPending() }
            } label: {
                Label("Sync", systemImage: "arrow.trianglehead.2.clockwise")
            }
            .disabled(model.isSyncing || !model.canSubmitCaptures)
        }
        #endif

        #if os(iOS)
        ToolbarItemGroup(placement: .keyboard) {
            Spacer()
            Button("Done") {
                dismissKeyboard()
            }
        }
        #endif
    }

    private var captureHeader: some View {
        HStack(alignment: .center, spacing: 12) {
            FieldCaptureBrandHeader()
            Button {
                Task { await model.syncPending() }
            } label: {
                Image(systemName: "arrow.trianglehead.2.clockwise")
                    .font(.headline)
                    .frame(width: 44, height: 44)
            }
            .buttonStyle(HeaderIconButtonStyle())
            .disabled(model.isSyncing || !model.canSubmitCaptures)
            .accessibilityLabel("Sync")
            .accessibilityIdentifier("capture.header.sync")
        }
    }

    private var statusHeader: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(model.statusMessage)
                .font(.headline)
                .accessibilityIdentifier("capture.status.message")
            let summary = model.queueSummary
            HStack {
                Label("\(summary.pending) pending", systemImage: "clock")
                    .accessibilityIdentifier("capture.status.pending")
                Label("\(summary.failed) failed", systemImage: "exclamationmark.triangle")
                    .accessibilityIdentifier("capture.status.failed")
                Label("\(summary.done) done", systemImage: "checkmark.circle")
                    .accessibilityIdentifier("capture.status.done")
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
    }

    private var captureNavigationTitle: String {
        #if os(iOS)
        ""
        #else
        "Field Capture"
        #endif
    }

    private var siteCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            sitePicker

            if let site = model.selectedSite {
                if !site.captureGuidance.isEmpty {
                    Text(site.captureGuidance)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var captureTools: some View {
        VStack(alignment: .leading, spacing: 12) {
            categoryPicker

            photoTools
            voiceTools

            if !model.canSubmitCaptures {
                Label("This account can view BTQ but cannot submit captures.", systemImage: "lock")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            if let cameraMessage {
                Text(cameraMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }

            if isImportingPhotos {
                Label("Importing selected photos...", systemImage: "hourglass")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("capture.photos.importing")
            } else if let photoImportMessage {
                Text(photoImportMessage)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
                    .accessibilityIdentifier("capture.photos.import.message")
            }

            if !pendingPhotos.isEmpty || recorder.lastAudio != nil {
                mediaStrip
            }
        }
    }

    private var categoryPicker: some View {
        Picker(selection: categorySelection) {
            Text("Select category...").tag(Optional<String>.none)
            ForEach(model.selectedSite?.displayCategories ?? []) { category in
                Text(category.label).tag(Optional(category.value))
            }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: "tag")
                    .font(.headline)
                    .foregroundStyle(.primary)
                Text("Area / QC")
                    .font(.headline)
                    .foregroundStyle(.primary)
                Spacer(minLength: 12)
                Text(selectedCategoryLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(model.selectedCategoryValue == nil ? .secondary : Color.btqAccent)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.btqAccent)
            }
        }
        .pickerStyle(.menu)
        .tint(.btqAccent)
        .disabled(!canEditDraft)
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(minHeight: 56)
        .contentShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(model.selectedCategoryValue == nil ? Color.btqAccent : Color.secondary.opacity(0.25), lineWidth: 1.25)
        )
        .accessibilityIdentifier("capture.category.picker")
        .accessibilityLabel("Area / QC")
        .accessibilityValue(selectedCategoryLabel)
    }

    private var sitePicker: some View {
        Picker(selection: siteSelection) {
            ForEach(model.prioritizedSites) { site in
                Text(site.label).tag(Optional(site.siteID))
            }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: "mappin.and.ellipse")
                    .font(.headline)
                    .foregroundStyle(.primary)
                Text("Site")
                    .font(.headline)
                    .foregroundStyle(.primary)
                Spacer(minLength: 12)
                Text(selectedSiteLabel)
                    .font(.subheadline.weight(.semibold))
                    .foregroundStyle(Color.btqAccent)
                    .lineLimit(1)
                Image(systemName: "chevron.up.chevron.down")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.btqAccent)
            }
        }
        .pickerStyle(.menu)
        .tint(.btqAccent)
        .disabled(!canEditDraft)
        .padding(.horizontal, 14)
        .padding(.vertical, 14)
        .frame(minHeight: 56)
        .contentShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.secondary.opacity(0.25), lineWidth: 1.25)
        )
        .accessibilityIdentifier("capture.site.picker")
        .accessibilityLabel("Site")
        .accessibilityValue(selectedSiteLabel)
    }

    private var photoTools: some View {
        VStack(alignment: .leading, spacing: 10) {
            let photoPickerTitle = isImportingPhotos ? "Importing" : "Camera Roll"

            HStack {
                Text("Photos")
                    .font(.headline)
                Spacer()
                Text("\(pendingPhotos.count) of \(model.maxImagesPerCapture)")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("capture.photos.count")
            }

            HStack(spacing: 12) {
                #if os(iOS)
                Button {
                    Task { await openCameraIfAllowed() }
                } label: {
                    CaptureToolLabel(title: "Take Photo", icon: .camera)
                }
                .buttonStyle(CaptureToolButtonStyle(kind: .primary))
                .disabled(!canEditDraft || isAtPhotoLimit)
                #endif

                PhotosPicker(selection: $selectedPhotoItems, maxSelectionCount: max(1, remainingPhotoSlots), matching: .images) {
                    CaptureToolLabel(title: photoPickerTitle, icon: .library)
                }
                .buttonStyle(CaptureToolButtonStyle(kind: .secondary))
                .disabled(!canEditDraft || isAtPhotoLimit || isImportingPhotos)
                .accessibilityIdentifier("capture.photos.picker")
            }
        }
    }

    private var voiceTools: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack {
                Text("Voice Note")
                    .font(.headline)
                Spacer()
                Text("Optional \(voiceDurationLabel)")
                    .font(.subheadline.weight(.medium))
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("voice.duration")
            }

            VoiceRecorderView(recorder: recorder)
                .disabled(!canEditDraft)
        }
    }

    private var mediaStrip: some View {
        LazyVStack(alignment: .leading, spacing: 10) {
            HStack {
                if !pendingPhotos.isEmpty {
                    Label("\(pendingPhotos.count) photo\(pendingPhotos.count == 1 ? "" : "s")", systemImage: "photo")
                }
                if recorder.lastAudio != nil {
                    Label("Voice memo", systemImage: "waveform")
                }
                Spacer()
                Button("Clear") {
                    showingClearDraftMediaConfirmation = true
                }
                .disabled(!canEditDraft)
                .accessibilityLabel("Clear pending media")
                .accessibilityHint("Removes unsaved photos and voice memo from this draft.")
            }

            ForEach($pendingPhotos) { $photo in
                HStack(alignment: .top, spacing: 10) {
                    CapturePhotoThumbnail(photo: photo)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Photo")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                        TextField("Photo note", text: $photo.note, axis: .vertical)
                            .textFieldStyle(.roundedBorder)
                            .lineLimit(1...3)
                            .disabled(!canEditDraft)
                            .accessibilityLabel("Photo note for \(photo.filename)")
                    }
                }
                .accessibilityElement(children: .contain)
            }
        }
        .font(.callout)
    }

    private var noteEditor: some View {
        VStack(alignment: .leading, spacing: 10) {
            TextField("Observation note", text: $model.observationText, axis: .vertical)
                .textFieldStyle(.roundedBorder)
                .lineLimit(3...5)
                .accessibilityIdentifier("capture.observation.text")
                .disabled(!canEditDraft)

            Button {
                Task { await saveCurrentDraft() }
            } label: {
                Label("Upload Capture", systemImage: "arrow.up.circle")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!canEditDraft)
            .accessibilityIdentifier("capture.upload")
        }
    }

    private var siteSelection: Binding<String?> {
        Binding {
            model.selectedSiteID
        } set: { newValue in
            model.selectSite(id: newValue)
        }
    }

    private var categorySelection: Binding<String?> {
        Binding {
            model.selectedCategoryValue
        } set: { newValue in
            model.selectedCategoryValue = newValue
        }
    }

    private var remainingPhotoSlots: Int {
        max(0, model.maxImagesPerCapture - pendingPhotos.count)
    }

    private var selectedCategoryLabel: String {
        guard let selectedCategoryValue = model.selectedCategoryValue,
              let category = model.selectedSite?.displayCategories.first(where: { $0.value == selectedCategoryValue }) else {
            return "Not selected"
        }
        return category.label
    }

    private var selectedSiteLabel: String {
        model.selectedSite?.label ?? "No site selected"
    }

    private var isAtPhotoLimit: Bool {
        remainingPhotoSlots == 0
    }

    private var hasDraftContent: Bool {
        !pendingPhotos.isEmpty
            || recorder.lastAudio != nil
            || !model.observationText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    private var hasPendingAudio: Bool {
        recorder.lastAudio != nil || recorder.isRecording || recorder.isPaused
    }

    private var voiceDurationLabel: String {
        if recorder.isRecording || recorder.isPaused {
            return VoiceRecorder.formatDuration(recorder.elapsedSeconds)
        }
        if let audio = recorder.lastAudio {
            return VoiceRecorder.formatDuration(audio.durationSeconds)
        }
        return VoiceRecorder.formatDuration(0)
    }

    private var canEditDraft: Bool {
        model.canSubmitCaptures && !isSavingDraft && !isImportingPhotos
    }

    private var currentDraftContext: DraftContext {
        DraftContext(accountID: model.account.id, siteID: model.selectedSiteID)
    }

    #if os(iOS)
    private func openCameraIfAllowed() async {
        let status = await cameraPermissionChecker.authorizationStatus()
        await handleCameraDecision(
            CameraCapturePermissionGate.decision(
                status: status,
                cameraAvailable: CameraCaptureView.isCameraAvailable
            )
        )
    }

    private func handleCameraDecision(_ decision: CameraCaptureStartDecision) async {
        switch decision {
        case .requestPermission:
            let requestedStatus = await cameraPermissionChecker.requestAuthorization()
            await handleCameraDecision(
                CameraCapturePermissionGate.decision(
                    status: requestedStatus,
                    cameraAvailable: CameraCaptureView.isCameraAvailable
                )
            )
        case .presentCamera:
            cameraDraftContext = currentDraftContext
            cameraMessage = nil
            showCamera = true
        case .showMessage(let message):
            cameraDraftContext = nil
            cameraMessage = message
            showCamera = false
        }
    }
    #endif

    private func canAttachMedia(to context: DraftContext) -> Bool {
        isActiveDraftContext(context) && !isSavingDraft
    }

    private func isActiveDraftContext(_ context: DraftContext) -> Bool {
        context == currentDraftContext && model.canSubmitCaptures
    }

    private func saveCurrentDraft() async {
        guard !isSavingDraft else { return }
        guard model.validateQuickObservationDraft(photoCount: pendingPhotos.count, hasAudio: hasPendingAudio) else {
            return
        }

        isSavingDraft = true
        defer { isSavingDraft = false }

        let context = currentDraftContext
        let hadPendingAudio = hasPendingAudio
        let audio = persistPendingAudio()
        guard !hadPendingAudio || audio != nil else {
            model.statusMessage = "Could not save voice memo. Try recording again."
            return
        }
        guard isActiveDraftContext(context) else {
            mediaStore.deletePendingMedia(photos: [], audio: audio)
            model.statusMessage = "Draft changed before save completed. Review and save again."
            return
        }

        let savedPhotos = pendingPhotos
        let didSave = await model.saveQuickObservation(photos: savedPhotos, audio: audio)
        if didSave {
            pendingPhotos.removeAll { savedPhoto in
                savedPhotos.contains { $0.id == savedPhoto.id }
            }
            recorder.clear()
        }
    }

    private func loadPhotos(_ items: [PhotosPickerItem], context: DraftContext) async {
        defer { selectedPhotoItems = [] }
        guard canAttachMedia(to: context) else { return }
        guard !items.isEmpty else { return }

        isImportingPhotos = true
        photoImportMessage = nil
        defer { isImportingPhotos = false }

        var importedCount = 0
        var failedCount = 0
        for item in items {
            guard pendingPhotos.count < model.maxImagesPerCapture else { break }
            guard canAttachMedia(to: context) else { break }
            guard let pickedPhoto = try? await item.loadTransferable(type: PickedPhotoFile.self) else {
                failedCount += 1
                await Task.yield()
                continue
            }
            guard canAttachMedia(to: context) else {
                try? FileManager.default.removeItem(at: pickedPhoto.fileURL)
                break
            }
            guard let photo = savePhoto(fileURL: pickedPhoto.fileURL, prefix: "photo") else {
                try? FileManager.default.removeItem(at: pickedPhoto.fileURL)
                failedCount += 1
                await Task.yield()
                continue
            }
            try? FileManager.default.removeItem(at: pickedPhoto.fileURL)
            if await appendPhotoToDraft(photo, context: context) {
                importedCount += 1
            } else {
                failedCount += 1
            }
            await Task.yield()
        }

        if failedCount > 0 {
            photoImportMessage = "Imported \(importedCount) photo\(importedCount == 1 ? "" : "s"). \(failedCount) could not be added."
        } else if importedCount > 0 {
            photoImportMessage = "Imported \(importedCount) photo\(importedCount == 1 ? "" : "s")."
        }
    }

    private func savePhoto(data: Data, prefix: String) -> CapturePhoto? {
        guard pendingPhotos.count < model.maxImagesPerCapture else { return nil }
        return try? mediaStore.savePhotoData(data, preferredStem: prefix, bucketID: mediaBucketID)
    }

    private func savePhoto(fileURL: URL, prefix: String) -> CapturePhoto? {
        guard pendingPhotos.count < model.maxImagesPerCapture else { return nil }
        return try? mediaStore.savePhotoFile(fileURL, preferredStem: prefix, bucketID: mediaBucketID)
    }

    @discardableResult
    private func appendPhotoToDraft(_ photo: CapturePhoto, context: DraftContext) async -> Bool {
        guard canAttachMedia(to: context) else {
            mediaStore.deletePendingMedia(photos: [photo])
            return false
        }
        pendingPhotos.append(photo)
        let didPersist = await model.upsertDraftCapture(photos: pendingPhotos, audio: nil)
        if !didPersist {
            pendingPhotos.removeAll { $0.id == photo.id }
            mediaStore.deletePendingMedia(photos: [photo])
        }
        return didPersist
    }

    private func persistActiveMediaDraft(photos: [CapturePhoto]) async {
        guard !photos.isEmpty, canEditDraft else { return }
        await model.upsertDraftCapture(photos: photos, audio: nil)
    }

    private func persistActiveDraftIfPresent() async {
        guard !pendingPhotos.isEmpty, canEditDraft else { return }
        await model.upsertDraftCapture(photos: pendingPhotos, audio: nil)
    }

    private func restoreActiveDraftIfAvailable() {
        guard pendingPhotos.isEmpty,
              recorder.lastAudio == nil,
              let draft = model.activeDraftCapture else {
            return
        }
        if model.selectedSiteID != draft.siteID {
            model.selectSite(id: draft.siteID)
        }
        pendingPhotos = draft.photos
        if !draft.note.isEmpty {
            model.observationText = draft.note
        }
        if model.selectedSite?.displayCategories.contains(where: { $0.value == draft.qcCategory }) == true {
            model.selectedCategoryValue = draft.qcCategory
        }
        model.statusMessage = "Recovered local draft."
    }

    private func discardPendingMedia(draftSiteID: String? = nil) {
        mediaStore.deletePendingMedia(photos: pendingPhotos, audio: recorder.lastAudio)
        pendingPhotos = []
        recorder.clear()
        Task { await model.removeDraftCapture(siteID: draftSiteID) }
    }

    private func discardDraftAfterSiteChange(previousSiteID: String?) {
        guard hasDraftContent else { return }
        #if os(iOS)
        dismissKeyboard()
        #endif
        discardPendingMedia(draftSiteID: previousSiteID)
        selectedPhotoItems = []
        model.observationText = ""
        cameraMessage = nil
        model.statusMessage = "Draft cleared after site change."
    }

    private func discardDraftAfterSubmitPermissionRevoked() {
        guard hasDraftContent else { return }
        #if os(iOS)
        dismissKeyboard()
        #endif
        discardPendingMedia()
        selectedPhotoItems = []
        model.observationText = ""
        cameraMessage = nil
        model.statusMessage = "Draft cleared because this account cannot submit captures."
    }

    private func discardDraftAfterAccountChange() {
        guard hasDraftContent else { return }
        #if os(iOS)
        dismissKeyboard()
        #endif
        pendingPhotos = []
        recorder.clear()
        selectedPhotoItems = []
        model.observationText = ""
        cameraMessage = nil
        model.statusMessage = "Draft kept with previous account."
    }

    private func persistPendingAudio() -> CaptureAudio? {
        guard let audio = recorder.finalizeForSave() else { return nil }
        return try? mediaStore.persistAudio(audio, bucketID: mediaBucketID, removeSourceAfterCopy: true)
    }

    private var mediaBucketID: String {
        model.activeVisit(forSiteID: model.selectedSite?.siteID)?.id.uuidString ?? "loose-capture"
    }

    #if os(iOS)
    private func dismissKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
    #endif

}

struct VoiceRecorderView: View {
    @Bindable var recorder: VoiceRecorder

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(alignment: .center, spacing: 10) {
                HStack(spacing: 10) {
                    Button {
                        Task { await recorder.start() }
                    } label: {
                        Text("●")
                            .font(.system(size: 22, weight: .heavy))
                    }
                    .buttonStyle(TapeRecordButtonStyle())
                    .disabled(recorder.isRecording)
                    .accessibilityLabel(recorder.lastAudio == nil ? "Record Voice Memo" : "Re-record Voice Memo")
                    .accessibilityIdentifier(recorder.lastAudio == nil ? "voice.record" : "voice.rerecord")
                    .accessibilityHint(recorder.lastAudio == nil ? "Starts recording a voice memo." : "Replaces the current voice memo.")

                    Button {
                        _ = recorder.stop()
                    } label: {
                        Text("■")
                            .font(.system(size: 18, weight: .heavy))
                    }
                    .buttonStyle(TapeStopButtonStyle())
                    .disabled(!recorder.isRecording)
                    .accessibilityLabel("Stop Voice Memo")
                    .accessibilityIdentifier("voice.stop")
                    .accessibilityHint("Finishes this voice memo and keeps it with the draft.")
                }

                if recorder.isRecording {
                    Button {
                        recorder.isPaused ? recorder.resume() : recorder.pause()
                    } label: {
                        Label(recorder.isPaused ? "Resume" : "Pause", systemImage: recorder.isPaused ? "play.fill" : "pause.fill")
                    }
                    .buttonStyle(.bordered)
                    .tint(.btqAccent)
                    .accessibilityIdentifier(recorder.isPaused ? "voice.resume" : "voice.pause")
                    .accessibilityHint(recorder.isPaused ? "Continues the current voice memo." : "Pauses the current voice memo.")
                } else if recorder.lastAudio != nil {
                    Button {
                        recorder.isPlaying ? recorder.stopPlayback() : recorder.play()
                    } label: {
                        Label(recorder.isPlaying ? "Stop Playback" : "Play Voice Memo", systemImage: recorder.isPlaying ? "stop.fill" : "play.fill")
                    }
                    .buttonStyle(.bordered)
                    .tint(.btqAccent)
                    .accessibilityIdentifier(recorder.isPlaying ? "voice.playback.stop" : "voice.playback.play")
                    .accessibilityHint(recorder.isPlaying ? "Stops voice memo playback." : "Plays the saved voice memo.")
                }

                Spacer(minLength: 0)

                Button {
                    recorder.clear()
                } label: {
                    Text("×")
                        .font(.system(size: 18, weight: .bold))
                }
                .buttonStyle(TapeClearButtonStyle())
                .disabled(!recorder.isRecording && recorder.lastAudio == nil)
                .accessibilityLabel("Erase Voice Memo")
                .accessibilityIdentifier("voice.clear")
                .accessibilityHint("Erases the current recording from the draft.")
            }
            .padding(10)
            .background(.quaternary.opacity(0.28))
            .overlay {
                RoundedRectangle(cornerRadius: 2)
                    .stroke(.quaternary)
            }

            if recorder.isRecording {
                Text(
                    recorder.isPaused
                        ? "Voice memo paused \(VoiceRecorder.formatDuration(recorder.elapsedSeconds))"
                        : "Recording voice memo \(VoiceRecorder.formatDuration(recorder.elapsedSeconds))"
                )
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("voice.status")
            } else if let audio = recorder.lastAudio {
                Text("Voice memo ready \(VoiceRecorder.formatDuration(audio.durationSeconds))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .accessibilityIdentifier("voice.status")
            }

            if let errorMessage = recorder.errorMessage {
                Text(errorMessage)
                    .font(.caption)
                    .foregroundStyle(.red)
                    .lineLimit(2)
            }
        }
    }
}

private enum CaptureToolIcon {
    case camera
    case library
}

private struct CaptureToolLabel: View {
    let title: String
    let icon: CaptureToolIcon

    var body: some View {
        VStack(spacing: 8) {
            switch icon {
            case .camera:
                PWACameraGlyph()
            case .library:
                PWAPhotoLibraryGlyph()
            }
            Text(title)
                .font(.headline)
        }
        .frame(maxWidth: .infinity, minHeight: 104)
    }
}

private enum CaptureToolButtonKind {
    case primary
    case secondary
}

private struct CaptureToolButtonStyle: ButtonStyle {
    let kind: CaptureToolButtonKind

    func makeBody(configuration: Configuration) -> some View {
        let strokeStyle: AnyShapeStyle = kind == .primary ? AnyShapeStyle(Color.btqAccent) : AnyShapeStyle(.quaternary)

        configuration.label
            .foregroundStyle(kind == .primary ? Color.btqAccent : .primary)
            .background(.background)
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .stroke(strokeStyle, lineWidth: kind == .primary ? 1.5 : 1)
            }
            .opacity(configuration.isPressed ? 0.78 : 1)
    }
}

private struct PWACameraGlyph: View {
    var body: some View {
        ZStack {
            Path { path in
                path.move(to: CGPoint(x: 3, y: 7))
                path.addLine(to: CGPoint(x: 7, y: 7))
                path.addLine(to: CGPoint(x: 9, y: 5))
                path.addLine(to: CGPoint(x: 15, y: 5))
                path.addLine(to: CGPoint(x: 17, y: 7))
                path.addLine(to: CGPoint(x: 21, y: 7))
                path.addLine(to: CGPoint(x: 21, y: 19))
                path.addLine(to: CGPoint(x: 3, y: 19))
                path.closeSubpath()
            }
            .stroke(style: StrokeStyle(lineWidth: 1.5, lineJoin: .round))

            Circle()
                .stroke(lineWidth: 1.5)
                .frame(width: 7, height: 7)
                .offset(y: 1)
        }
        .frame(width: 24, height: 24)
    }
}

private struct PWAPhotoLibraryGlyph: View {
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 2)
                .stroke(lineWidth: 1.5)
                .frame(width: 18, height: 14)
                .offset(y: 1)

            Path { path in
                path.move(to: CGPoint(x: 3, y: 17))
                path.addLine(to: CGPoint(x: 8, y: 12))
                path.addLine(to: CGPoint(x: 13, y: 17))
            }
            .stroke(style: StrokeStyle(lineWidth: 1.5, lineCap: .round, lineJoin: .round))

            Circle()
                .fill(.primary)
                .frame(width: 3, height: 3)
                .offset(x: 4, y: -2)
        }
        .frame(width: 24, height: 24)
    }
}

private struct TapeRecordButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.white)
            .frame(width: 48, height: 48)
            .background(Color(red: 0.75, green: 0.22, blue: 0.17))
            .clipShape(Circle())
            .overlay {
                Circle()
                    .stroke(Color(red: 0.91, green: 0.30, blue: 0.24), lineWidth: 2)
            }
            .scaleEffect(configuration.isPressed ? 1.04 : 1)
    }
}

private struct TapeStopButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.primary)
            .frame(width: 44, height: 44)
            .background(.background)
            .clipShape(RoundedRectangle(cornerRadius: 3))
            .overlay {
                RoundedRectangle(cornerRadius: 3)
                    .stroke(.quaternary, lineWidth: 2)
            }
            .scaleEffect(configuration.isPressed ? 1.03 : 1)
    }
}

private struct TapeClearButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.secondary)
            .frame(width: 36, height: 36)
            .background(.clear)
            .clipShape(Circle())
            .overlay {
                Circle()
                    .stroke(.quaternary)
            }
            .scaleEffect(configuration.isPressed ? 1.03 : 1)
    }
}

private struct HeaderIconButtonStyle: ButtonStyle {
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.primary)
            .background(.background)
            .clipShape(RoundedRectangle(cornerRadius: 8))
            .overlay {
                RoundedRectangle(cornerRadius: 8)
                    .stroke(.quaternary, lineWidth: 1)
            }
            .opacity(configuration.isPressed ? 0.72 : 1)
    }
}

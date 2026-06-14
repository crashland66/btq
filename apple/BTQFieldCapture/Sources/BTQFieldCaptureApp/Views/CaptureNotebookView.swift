import PhotosUI
import SwiftUI
#if os(iOS)
import UIKit
#endif

private struct DraftContext: Equatable {
    let accountID: UUID
    let siteID: String?
}

struct CaptureNotebookView: View {
    @Bindable var model: FieldCaptureModel
    @State private var selectedPhotoItems: [PhotosPickerItem] = []
    @State private var pendingPhotos: [CapturePhoto] = []
    @State private var recorder = VoiceRecorder()
    @State private var showCamera = false
    @State private var cameraDraftContext: DraftContext?
    @State private var cameraMessage: String?
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
                statusHeader
                siteCard
                captureTools
                noteEditor
                timeline
            }
            .padding()
            .frame(maxWidth: 760, alignment: .center)
            .frame(maxWidth: .infinity)
        }
        .navigationTitle("Field Capture")
        .toolbar {
            captureToolbar
        }
        .onChange(of: selectedPhotoItems) { _, items in
            let context = currentDraftContext
            Task { await loadPhotos(items, context: context) }
        }
        .onChange(of: model.selectedSiteID) { oldValue, newValue in
            guard oldValue != nil, oldValue != newValue else { return }
            discardDraftAfterSiteChange()
        }
        .onChange(of: model.account.id) { oldValue, newValue in
            guard oldValue != newValue else { return }
            discardDraftAfterAccountChange()
        }
        .onChange(of: model.canSubmitCaptures) { _, canSubmit in
            guard !canSubmit else { return }
            discardDraftAfterSubmitPermissionRevoked()
        }
        #if os(iOS)
        .sheet(isPresented: $showCamera) {
            CameraCaptureView { data in
                guard let context = cameraDraftContext, canAttachMedia(to: context) else { return }
                if let photo = savePhoto(data: data, prefix: "camera") {
                    pendingPhotos.append(photo)
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
        ToolbarItem(placement: .primaryAction) {
            Button {
                Task { await model.syncPending() }
            } label: {
                Label("Sync", systemImage: "arrow.trianglehead.2.clockwise")
            }
            .disabled(model.isSyncing || !model.canSubmitCaptures)
        }

        #if os(iOS)
        ToolbarItemGroup(placement: .keyboard) {
            Spacer()
            Button("Done") {
                dismissKeyboard()
            }
        }
        #endif
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

    private var siteCard: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Site", selection: siteSelection) {
                ForEach(model.prioritizedSites) { site in
                    Text(site.label).tag(Optional(site.siteID))
                }
            }
            .pickerStyle(.menu)
            .disabled(!canEditDraft)

            if let site = model.selectedSite {
                let activeVisit = model.activeVisit(forSiteID: site.siteID)
                if !site.captureGuidance.isEmpty {
                    Text(site.captureGuidance)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }

                HStack {
                    Button {
                        Task { await model.startVisit(site: site) }
                    } label: {
                        Label(activeVisit == nil ? "Start Visit" : "Restart Visit", systemImage: "play.circle")
                    }
                    .disabled(!canEditDraft)

                    Button {
                        Task { await model.endVisit(site: site) }
                    } label: {
                        Label("End Visit", systemImage: "stop.circle")
                    }
                    .disabled(activeVisit == nil || !canEditDraft)
                }
                .buttonStyle(.bordered)
            }
        }
    }

    private var captureTools: some View {
        VStack(alignment: .leading, spacing: 12) {
            Picker("Observation", selection: categorySelection) {
                ForEach(model.selectedSite?.displayCategories ?? []) { category in
                    Text(category.label).tag(Optional(category.value))
                }
            }
            .pickerStyle(.menu)
            .disabled(!canEditDraft)

            HStack(spacing: 12) {
                #if os(iOS)
                Button {
                    Task { await openCameraIfAllowed() }
                } label: {
                    Label("Camera", systemImage: "camera")
                }
                .buttonStyle(.borderedProminent)
                .disabled(!canEditDraft || isAtPhotoLimit)
                #endif

                PhotosPicker(selection: $selectedPhotoItems, maxSelectionCount: max(1, remainingPhotoSlots), matching: .images) {
                    Label("Photos", systemImage: "photo.on.rectangle")
                }
                .buttonStyle(.bordered)
                .disabled(!canEditDraft || isAtPhotoLimit)

                VoiceRecorderView(recorder: recorder)
                    .disabled(!canEditDraft)
            }

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

            if !pendingPhotos.isEmpty || recorder.lastAudio != nil {
                mediaStrip
            }
        }
    }

    private var mediaStrip: some View {
        VStack(alignment: .leading, spacing: 10) {
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
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                        .frame(width: 24)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(photo.filename)
                            .font(.caption)
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
            TextEditor(text: $model.observationText)
                .frame(minHeight: 120)
                .accessibilityIdentifier("capture.observation.text")
                .disabled(!canEditDraft)
                .overlay {
                    RoundedRectangle(cornerRadius: 8)
                        .stroke(.quaternary)
                }

            Button {
                Task { await saveCurrentDraft() }
            } label: {
                Label("Save Locally", systemImage: "tray.and.arrow.down")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!canEditDraft)
            .accessibilityIdentifier("capture.save.local")
        }
    }

    private var timeline: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text("Visit Timeline")
                .font(.headline)
            ForEach(model.timeline) { entry in
                HStack(alignment: .top) {
                    Image(systemName: icon(for: entry.status))
                        .foregroundStyle(color(for: entry.status))
                    VStack(alignment: .leading) {
                        Text(entry.title)
                            .lineLimit(2)
                        Text(entry.subtitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                    Spacer()
                }
                .padding(.vertical, 4)
                .accessibilityElement(children: .combine)
                .accessibilityLabel("\(entry.title). \(entry.subtitle)")
            }
        }
    }

    private var siteSelection: Binding<String?> {
        Binding {
            model.selectedSiteID
        } set: { newValue in
            model.selectedSiteID = newValue
            model.selectedCategoryValue = model.selectedSite?.displayCategories.first?.value
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

    private var canEditDraft: Bool {
        model.canSubmitCaptures && !isSavingDraft
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

        for item in items {
            guard pendingPhotos.count < model.maxImagesPerCapture else { break }
            guard canAttachMedia(to: context) else { break }
            guard let data = try? await item.loadTransferable(type: Data.self) else { continue }
            guard canAttachMedia(to: context) else { break }
            guard let photo = savePhoto(data: data, prefix: "photo") else { continue }
            pendingPhotos.append(photo)
        }
    }

    private func savePhoto(data: Data, prefix: String) -> CapturePhoto? {
        guard pendingPhotos.count < model.maxImagesPerCapture else { return nil }
        return try? mediaStore.savePhotoData(data, preferredStem: prefix, bucketID: mediaBucketID)
    }

    private func discardPendingMedia() {
        mediaStore.deletePendingMedia(photos: pendingPhotos, audio: recorder.lastAudio)
        pendingPhotos = []
        recorder.clear()
    }

    private func discardDraftAfterSiteChange() {
        guard hasDraftContent else { return }
        #if os(iOS)
        dismissKeyboard()
        #endif
        discardPendingMedia()
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
        discardPendingMedia()
        selectedPhotoItems = []
        model.observationText = ""
        cameraMessage = nil
        model.statusMessage = "Draft cleared after account change."
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

    private func icon(for status: CaptureQueueStatus) -> String {
        switch status {
        case .draft: "circle"
        case .pending: "clock"
        case .uploading: "arrow.up.circle"
        case .done: "checkmark.circle"
        case .failed: "exclamationmark.triangle"
        }
    }

    private func color(for status: CaptureQueueStatus) -> Color {
        switch status {
        case .failed: .red
        case .done: .green
        case .uploading: .blue
        default: .secondary
        }
    }
}

struct VoiceRecorderView: View {
    @Bindable var recorder: VoiceRecorder

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                if recorder.isRecording {
                    Button {
                        recorder.isPaused ? recorder.resume() : recorder.pause()
                    } label: {
                        Label(recorder.isPaused ? "Resume" : "Pause", systemImage: recorder.isPaused ? "play.fill" : "pause.fill")
                    }
                    .accessibilityIdentifier(recorder.isPaused ? "voice.resume" : "voice.pause")
                    .accessibilityHint(recorder.isPaused ? "Continues the current voice memo." : "Pauses the current voice memo.")
                    Button {
                        _ = recorder.stop()
                    } label: {
                        Label("Stop", systemImage: "stop.fill")
                    }
                    .accessibilityIdentifier("voice.stop")
                    .accessibilityHint("Finishes this voice memo and keeps it with the draft.")
                } else {
                    Button {
                        Task { await recorder.start() }
                    } label: {
                        Label(recorder.lastAudio == nil ? "Voice" : "Re-record", systemImage: "mic")
                    }
                    .accessibilityIdentifier(recorder.lastAudio == nil ? "voice.record" : "voice.rerecord")
                    .accessibilityHint(recorder.lastAudio == nil ? "Starts recording a voice memo." : "Replaces the current voice memo.")

                    if recorder.lastAudio != nil {
                        Button {
                            recorder.isPlaying ? recorder.stopPlayback() : recorder.play()
                        } label: {
                            Label(recorder.isPlaying ? "Stop Playback" : "Play Voice Memo", systemImage: recorder.isPlaying ? "stop.fill" : "play.fill")
                        }
                        .accessibilityIdentifier(recorder.isPlaying ? "voice.playback.stop" : "voice.playback.play")
                        .accessibilityHint(recorder.isPlaying ? "Stops voice memo playback." : "Plays the saved voice memo.")
                    }
                }
            }
            .buttonStyle(.bordered)

            if recorder.isRecording {
                Text(recorder.isPaused ? "Voice memo paused" : "Recording voice memo...")
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

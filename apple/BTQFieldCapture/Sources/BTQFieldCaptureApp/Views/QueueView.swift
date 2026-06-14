import SwiftUI

struct QueueView: View {
    @Bindable var model: FieldCaptureModel
    @State private var selectedDetail: QueueCaptureDetail?

    var body: some View {
        List {
            summarySection
            localQueueSection
            serverHistorySection
            versionSection
        }
        .navigationTitle("Upload Queue")
        .toolbar {
            Button {
                Task { await model.syncPending() }
            } label: {
                Label("Sync", systemImage: "arrow.trianglehead.2.clockwise")
            }
            .disabled(model.isSyncing || !model.canSubmitCaptures)
        }
        .sheet(item: $selectedDetail) { detail in
            QueueCaptureDetailSheet(detail: detail)
        }
    }

    private var localQueueSection: some View {
        Section("Local Queue") {
            if model.captures.isEmpty {
                Text("No local captures waiting.")
                    .foregroundStyle(.secondary)
            }
            ForEach(model.captures.sorted { $0.capturedAt > $1.capturedAt }) { capture in
                LocalCaptureQueueRow(model: model, capture: capture) {
                    selectedDetail = .local(capture)
                }
            }
        }
    }

    private var serverHistorySection: some View {
        Section("Server History") {
            Button {
                Task { await model.refreshSubmittedHistory() }
            } label: {
                Label("Refresh Submitted History", systemImage: "arrow.clockwise")
            }
            .disabled(model.isRefreshingSubmittedHistory)
            .accessibilityIdentifier("queue.server.refresh")

            if let summary = model.submissionQualitySummary {
                ServerQualitySummaryRow(summary: summary)
                    .accessibilityIdentifier("queue.server.quality")
            }

            if model.submittedCaptures.isEmpty {
                Text("Refresh to see accepted captures and processing status.")
                    .foregroundStyle(.secondary)
            }

            ForEach(model.submittedCaptures.prefix(10)) { submission in
                SubmittedCaptureRow(submission: submission) {
                    selectedDetail = .submitted(submission)
                }
                    .accessibilityIdentifier("queue.server.capture.\(submission.captureID)")
            }
        }
    }

    private var versionSection: some View {
        Section {
            AppVersionFooter()
                .listRowBackground(Color.clear)
        }
    }

    private var summarySection: some View {
        let summary = model.queueSummary
        return Section {
            HStack {
                metric("Pending", summary.pending)
                metric("Uploading", summary.uploading)
                metric("Failed", summary.failed)
                metric("Done", summary.done)
            }
        }
    }

    private func metric(_ label: String, _ value: Int) -> some View {
        VStack {
            Text("\(value)")
                .font(.headline)
            Text(label)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
        .accessibilityIdentifier("queue.summary.\(label.lowercased())")
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(label)
        .accessibilityValue("\(value)")
    }
}

private struct LocalCaptureQueueRow: View {
    @Bindable var model: FieldCaptureModel
    let capture: LocalCapture
    let onDetails: () -> Void
    @State private var isExpanded = false
    @State private var showingDeleteConfirmation = false

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                if !capture.captureID.isEmpty {
                    detailLine("Capture", capture.captureID)
                }
                detailLine("Category", capture.qcCategory)
                detailLine("Captured", capture.capturedAt.formatted(date: .abbreviated, time: .shortened))
                if !capture.note.isEmpty {
                    Text(capture.note)
                        .font(.callout)
                }
                mediaSummary

                HStack {
                    Button {
                        onDetails()
                    } label: {
                        Label("Details", systemImage: "info.circle")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)

                    if capture.status == .failed {
                        Button {
                            Task { await model.retryCapture(capture.captureID) }
                        } label: {
                            Label("Retry", systemImage: "arrow.counterclockwise")
                        }
                        .buttonStyle(.bordered)
                        .controlSize(.small)
                        .disabled(model.isSyncing || !model.canSubmitCaptures)
                        .accessibilityLabel("Retry upload for \(capture.siteLabel)")
                        .accessibilityHint("Moves this failed capture back to pending.")
                    }
                }
            }
            .padding(.top, 4)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Label(capture.siteLabel, systemImage: icon(for: capture.status))
                        .foregroundStyle(color(for: capture.status))
                    Spacer()
                    Text(capture.status.rawValue)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Text(capture.note.isEmpty ? capture.qcCategory : capture.note)
                    .lineLimit(2)
                HStack {
                    if !capture.photos.isEmpty {
                        Label("\(capture.photos.count)", systemImage: "photo")
                    }
                    if capture.audio != nil {
                        Label("1", systemImage: "waveform")
                    }
                    if capture.attempts > 0 {
                        Text("\(capture.attempts) attempts")
                    }
                    if let retryAfter = capture.retryAfter, capture.status == .pending {
                        Text("retry \(retryAfter.formatted(date: .omitted, time: .shortened))")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)

                if let lastError = capture.lastError, !lastError.isEmpty {
                    Text(lastError)
                        .font(.caption)
                        .foregroundStyle(capture.status == .failed ? .red : .secondary)
                        .lineLimit(2)
                }

                if let failureRecoveryHint = capture.failureRecoveryHint {
                    Label(failureRecoveryHint, systemImage: "lightbulb")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .lineLimit(3)
                        .accessibilityLabel("Recovery guidance: \(failureRecoveryHint)")
                }
            }
        }
        .swipeActions(edge: .trailing) {
            if capture.status != .uploading {
                Button(role: .destructive) {
                    showingDeleteConfirmation = true
                } label: {
                    Label("Delete", systemImage: "trash")
                }
            }
        }
        .contextMenu {
            if capture.status != .uploading {
                Button(role: .destructive) {
                    showingDeleteConfirmation = true
                } label: {
                    Label("Delete", systemImage: "trash")
                }
            }
        }
        .confirmationDialog(
            "Delete this local capture?",
            isPresented: $showingDeleteConfirmation,
            titleVisibility: .visible
        ) {
            Button("Delete Capture", role: .destructive) {
                Task { await model.deleteCapture(capture.captureID) }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("This removes the queued capture and any app-owned photo or voice memo files stored for \(capture.siteLabel).")
        }
        .accessibilityIdentifier("queue.capture.\(capture.captureID)")
        .accessibilityElement(children: .contain)
    }

    private var mediaSummary: some View {
        HStack(spacing: 10) {
            if !capture.photos.isEmpty {
                Label("\(capture.photos.count) photo\(capture.photos.count == 1 ? "" : "s")", systemImage: "photo")
            }
            if let audio = capture.audio {
                Label(VoiceRecorder.formatDuration(audio.durationSeconds), systemImage: "waveform")
            }
            if capture.photos.isEmpty && capture.audio == nil {
                Label("Text only", systemImage: "note.text")
            }
        }
        .font(.caption)
        .foregroundStyle(.secondary)
    }

    private func detailLine(_ label: String, _ value: String) -> some View {
        LabeledContent(label, value: value)
            .font(.caption)
    }

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
        case .done: .btqAccent
        case .uploading: .btqUploading
        default: .primary
        }
    }
}

private struct ServerQualitySummaryRow: View {
    let summary: SubmissionQualitySummary

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Label("Photo quality", systemImage: "camera.metering.center.weighted")
            HStack {
                Text("\(summary.clear) clear")
                Text("\(summary.totalProcessed) processed")
                if !summary.flagCounts.isEmpty {
                    Text(summary.flagCounts.keys.sorted().joined(separator: ", "))
                }
            }
            .font(.caption)
            .foregroundStyle(.secondary)
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
    }

    private var accessibilitySummary: String {
        var parts = ["Photo quality", "\(summary.clear) clear", "\(summary.totalProcessed) processed"]
        if !summary.flagCounts.isEmpty {
            parts.append("Flags: \(summary.flagCounts.keys.sorted().joined(separator: ", "))")
        }
        return parts.joined(separator: ", ")
    }
}

private struct SubmittedCaptureRow: View {
    let submission: SubmittedCapture
    let onDetails: () -> Void
    @State private var isExpanded = false

    var body: some View {
        DisclosureGroup(isExpanded: $isExpanded) {
            VStack(alignment: .leading, spacing: 8) {
                detailLine("Capture", submission.captureID)
                detailLine("Captured", submission.capturedAt)
                if !submission.noteText.isEmpty {
                    Text(submission.noteText)
                        .font(.callout)
                }
                if !submission.outcomeLabel.isEmpty {
                    Text(submission.outcomeLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Button {
                    onDetails()
                } label: {
                    Label("Details", systemImage: "info.circle")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
            }
            .padding(.top, 4)
        } label: {
            VStack(alignment: .leading, spacing: 6) {
                HStack {
                    Label(submission.siteName, systemImage: stageIcon)
                    Spacer()
                    Text(stageLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                if !submission.noteText.isEmpty {
                    Text(submission.noteText)
                        .lineLimit(2)
                }
                HStack {
                    if submission.photoCount > 0 {
                        Label("\(submission.photoCount)", systemImage: "photo")
                    }
                    if submission.hasAudio {
                        Label("1", systemImage: "waveform")
                    }
                    if submission.hasTextNote {
                        Label("note", systemImage: "note.text")
                    }
                    if !qualityFlags.isEmpty {
                        Label(qualityFlags.joined(separator: ", "), systemImage: "exclamationmark.triangle")
                    }
                }
                .font(.caption)
                .foregroundStyle(.secondary)
                if !submission.outcomeLabel.isEmpty {
                    Text(submission.outcomeLabel)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .accessibilityElement(children: .combine)
        .accessibilityLabel(accessibilitySummary)
    }

    private var accessibilitySummary: String {
        var parts = [submission.siteName, stageLabel]
        if submission.photoCount > 0 {
            parts.append("\(submission.photoCount) photo\(submission.photoCount == 1 ? "" : "s")")
        }
        if submission.hasAudio {
            parts.append("voice memo")
        }
        if submission.hasTextNote {
            parts.append("text note")
        }
        if !qualityFlags.isEmpty {
            parts.append("Flags: \(qualityFlags.joined(separator: ", "))")
        }
        if !submission.outcomeLabel.isEmpty {
            parts.append(submission.outcomeLabel)
        }
        return parts.joined(separator: ", ")
    }

    private var stageLabel: String {
        switch submission.stage {
        case "processed":
            "Processed"
        case "reviewed":
            "Reviewed"
        case "acted_on":
            "Acted on"
        case "processing":
            "Processing"
        default:
            submission.stage.isEmpty ? "Submitted" : submission.stage
        }
    }

    private var stageIcon: String {
        switch submission.stage {
        case "processed", "reviewed", "acted_on":
            "checkmark.circle"
        case "processing":
            "clock"
        default:
            "tray.full"
        }
    }

    private var qualityFlags: [String] {
        Array(Set(submission.perPhotoQuality.flatMap(\.flags))).sorted()
    }

    private func detailLine(_ label: String, _ value: String) -> some View {
        LabeledContent(label, value: value)
            .font(.caption)
    }
}

private enum QueueCaptureDetail: Identifiable {
    case local(LocalCapture)
    case submitted(SubmittedCapture)

    var id: String {
        switch self {
        case .local(let capture):
            "local-\(capture.captureID)"
        case .submitted(let submission):
            "submitted-\(submission.captureID)"
        }
    }
}

private struct QueueCaptureDetailSheet: View {
    let detail: QueueCaptureDetail
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        NavigationStack {
            List {
                switch detail {
                case .local(let capture):
                    LocalCaptureDetailContent(capture: capture)
                case .submitted(let submission):
                    SubmittedCaptureDetailContent(submission: submission)
                }
            }
            .navigationTitle("Capture Details")
            #if os(iOS)
            .navigationBarTitleDisplayMode(.inline)
            #endif
            .toolbar {
                ToolbarItem(placement: .confirmationAction) {
                    Button("Done") {
                        dismiss()
                    }
                }
            }
        }
    }
}

private struct LocalCaptureDetailContent: View {
    let capture: LocalCapture

    var body: some View {
        Section("Capture") {
            LabeledContent("Site", value: capture.siteLabel)
            LabeledContent("Status", value: capture.status.rawValue)
            LabeledContent("Category", value: capture.qcCategory)
            LabeledContent("Captured", value: capture.capturedAt.formatted(date: .abbreviated, time: .shortened))
            LabeledContent("Capture ID", value: capture.captureID)
            LabeledContent("Target", value: capture.targetID)
        }

        if !capture.note.isEmpty {
            Section("Note") {
                Text(capture.note)
            }
        }

        if !capture.photos.isEmpty {
            Section("Photos") {
                ForEach(capture.photos) { photo in
                    HStack(alignment: .top, spacing: 12) {
                        CapturePhotoThumbnail(photo: photo)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(photo.filename)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            if !photo.note.isEmpty {
                                Text(photo.note)
                            }
                        }
                    }
                }
            }
        }

        if let audio = capture.audio {
            Section("Voice Memo") {
                LabeledContent("Duration", value: VoiceRecorder.formatDuration(audio.durationSeconds))
                LabeledContent("File", value: audio.filename)
            }
        }

        if let lastError = capture.lastError, !lastError.isEmpty {
            Section("Upload") {
                Text(lastError)
                    .foregroundStyle(capture.status == .failed ? .red : .secondary)
                if let failureRecoveryHint = capture.failureRecoveryHint {
                    Label(failureRecoveryHint, systemImage: "lightbulb")
                }
            }
        }
    }
}

private struct SubmittedCaptureDetailContent: View {
    let submission: SubmittedCapture

    var body: some View {
        Section("Capture") {
            LabeledContent("Site", value: submission.siteName)
            LabeledContent("Stage", value: stageLabel)
            LabeledContent("Captured", value: submission.capturedAt)
            LabeledContent("Capture ID", value: submission.captureID)
            LabeledContent("Target", value: submission.targetID)
        }

        if !submission.noteText.isEmpty {
            Section("Note") {
                Text(submission.noteText)
            }
        }

        Section("Media") {
            LabeledContent("Photos", value: "\(submission.photoCount)")
            LabeledContent("Voice memo", value: submission.hasAudio ? "Yes" : "No")
            LabeledContent("Text note", value: submission.hasTextNote ? "Yes" : "No")
        }

        if !submission.outcomeLabel.isEmpty || !submission.perPhotoQuality.isEmpty {
            Section("Processing") {
                if !submission.outcomeLabel.isEmpty {
                    Text(submission.outcomeLabel)
                }
                ForEach(Array(submission.perPhotoQuality.enumerated()), id: \.offset) { index, quality in
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Photo \(index + 1)")
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(.secondary)
                        if !quality.description.isEmpty {
                            Text(quality.description)
                        }
                        if !quality.flags.isEmpty {
                            Text(quality.flags.joined(separator: ", "))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private var stageLabel: String {
        switch submission.stage {
        case "processed":
            "Processed"
        case "reviewed":
            "Reviewed"
        case "acted_on":
            "Acted on"
        case "processing":
            "Processing"
        default:
            submission.stage.isEmpty ? "Submitted" : submission.stage
        }
    }
}

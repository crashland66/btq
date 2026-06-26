import AVFoundation
import Foundation

public enum AudioMemoUploadPreparationError: Error, LocalizedError, Equatable {
    case missingAudioFile(String)
    case unreadableAudio(String)
    case cannotCreateComposition
    case cannotCreateExporter
    case exportFailed(String)

    public var errorDescription: String? {
        switch self {
        case .missingAudioFile(let filename):
            return "Missing audio file: \(filename)"
        case .unreadableAudio(let filename):
            return "Could not prepare voice memo: \(filename)"
        case .cannotCreateComposition:
            return "Could not prepare voice memos for upload."
        case .cannotCreateExporter:
            return "Could not export voice memos for upload."
        case .exportFailed(let message):
            return message.isEmpty ? "Could not export voice memos for upload." : message
        }
    }
}

public protocol AudioMemoUploadPreparing: Sendable {
    func capturePreparedForUpload(_ capture: LocalCapture) async throws -> LocalCapture
}

public struct AudioMemoUploadPreparer: AudioMemoUploadPreparing {
    private let mediaStore: LocalMediaStore

    public init(mediaStore: LocalMediaStore = LocalMediaStore()) {
        self.mediaStore = mediaStore
    }

    public func capturePreparedForUpload(_ capture: LocalCapture) async throws -> LocalCapture {
        let audios = capture.audioAttachments
        guard audios.count > 1 else { return capture }

        let mergedAudio = try await merge(audios: audios, captureID: capture.captureID)
        var prepared = capture
        prepared.audios = [mergedAudio]
        prepared.audio = mergedAudio
        return prepared
    }

    private func merge(audios: [CaptureAudio], captureID: String) async throws -> CaptureAudio {
        let composition = AVMutableComposition()
        guard let compositionTrack = composition.addMutableTrack(
            withMediaType: .audio,
            preferredTrackID: kCMPersistentTrackID_Invalid
        ) else {
            throw AudioMemoUploadPreparationError.cannotCreateComposition
        }

        var cursor = CMTime.zero
        for audio in audios {
            guard let fileURL = audio.fileURL, FileManager.default.fileExists(atPath: fileURL.path) else {
                throw AudioMemoUploadPreparationError.missingAudioFile(audio.filename)
            }
            let asset = AVURLAsset(url: fileURL)
            let tracks = try await asset.loadTracks(withMediaType: .audio)
            guard let track = tracks.first else {
                throw AudioMemoUploadPreparationError.unreadableAudio(audio.filename)
            }
            let duration = try await asset.load(.duration)
            try compositionTrack.insertTimeRange(
                CMTimeRange(start: .zero, duration: duration),
                of: track,
                at: cursor
            )
            cursor = CMTimeAdd(cursor, duration)
        }

        let filename = "voice-notes-\(captureID).m4a"
        let outputURL = mediaStore.mediaDirectory(bucketID: captureID).appendingPathComponent(filename)
        try LocalFilePrivacy.prepareDirectory(outputURL.deletingLastPathComponent())
        if FileManager.default.fileExists(atPath: outputURL.path) {
            try FileManager.default.removeItem(at: outputURL)
        }

        guard let exportSession = AVAssetExportSession(asset: composition, presetName: AVAssetExportPresetAppleM4A) else {
            throw AudioMemoUploadPreparationError.cannotCreateExporter
        }
        exportSession.outputURL = outputURL
        exportSession.outputFileType = .m4a
        try await export(exportSession)
        try LocalFilePrivacy.protectExistingItem(outputURL)

        return CaptureAudio(
            filename: filename,
            mimeType: "audio/mp4",
            fileURL: outputURL,
            durationSeconds: cursor.seconds
        )
    }

    private func export(_ exportSession: AVAssetExportSession) async throws {
        await exportSession.export()
        switch exportSession.status {
        case .completed:
            return
        case .failed:
            throw AudioMemoUploadPreparationError.exportFailed(
                exportSession.error?.localizedDescription ?? "Could not export voice memos for upload."
            )
        case .cancelled:
            throw AudioMemoUploadPreparationError.exportFailed("Voice memo export was cancelled.")
        default:
            throw AudioMemoUploadPreparationError.exportFailed("Could not export voice memos for upload.")
        }
    }
}

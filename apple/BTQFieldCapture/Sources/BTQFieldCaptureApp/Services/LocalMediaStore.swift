import Foundation

public enum LocalMediaStoreError: Error, Equatable {
    case missingSourceFile
}

public struct LocalMediaStore: Sendable {
    public var rootDirectory: URL
    public var imagePolicy: ImageUploadPolicy

    public init(rootDirectory: URL = LocalMediaStore.defaultRootDirectory(), imagePolicy: ImageUploadPolicy = .fieldCapture) {
        self.rootDirectory = rootDirectory
        self.imagePolicy = imagePolicy
    }

    public func savePhotoData(_ data: Data, preferredStem: String = "photo", bucketID: String = UUID().uuidString) throws -> CapturePhoto {
        let filename = "\(safeStem(preferredStem))-\(UUID().uuidString).\(imagePolicy.format.fileExtension)"
        let url = mediaDirectory(bucketID: bucketID).appendingPathComponent(filename)
        let normalizedData = try ImageNormalizer.normalizedData(from: data, policy: imagePolicy)
        try prepareMediaDirectory(for: url)
        try normalizedData.write(to: url, options: [.atomic])
        try LocalFilePrivacy.protectExistingItem(url)
        return CapturePhoto(filename: filename, mimeType: imagePolicy.format.mimeType, fileURL: url)
    }

    public func savePhotoFile(_ sourceURL: URL, preferredStem: String = "photo", bucketID: String = UUID().uuidString) throws -> CapturePhoto {
        let filename = "\(safeStem(preferredStem))-\(UUID().uuidString).\(imagePolicy.format.fileExtension)"
        let url = mediaDirectory(bucketID: bucketID).appendingPathComponent(filename)
        let normalizedData = try ImageNormalizer.normalizedData(from: sourceURL, policy: imagePolicy)
        try prepareMediaDirectory(for: url)
        try normalizedData.write(to: url, options: [.atomic])
        try LocalFilePrivacy.protectExistingItem(url)
        return CapturePhoto(filename: filename, mimeType: imagePolicy.format.mimeType, fileURL: url)
    }

    public func persistAudio(_ audio: CaptureAudio, bucketID: String = UUID().uuidString, removeSourceAfterCopy: Bool = false) throws -> CaptureAudio {
        guard let sourceURL = audio.fileURL, FileManager.default.fileExists(atPath: sourceURL.path) else {
            throw LocalMediaStoreError.missingSourceFile
        }
        let filename = safeFilename(audio.filename, fallback: "voice-\(UUID().uuidString).m4a")
        let destination = mediaDirectory(bucketID: bucketID).appendingPathComponent(filename)
        try prepareMediaDirectory(for: destination)
        if sourceURL != destination {
            if FileManager.default.fileExists(atPath: destination.path) {
                try FileManager.default.removeItem(at: destination)
            }
            try FileManager.default.copyItem(at: sourceURL, to: destination)
            if removeSourceAfterCopy {
                try? FileManager.default.removeItem(at: sourceURL)
            }
        }
        try LocalFilePrivacy.protectExistingItem(destination)
        return CaptureAudio(
            id: audio.id,
            filename: filename,
            mimeType: audio.mimeType,
            fileURL: destination,
            durationSeconds: audio.durationSeconds
        )
    }

    public func mediaDirectory(bucketID: String) -> URL {
        rootDirectory.appendingPathComponent(safeStem(bucketID), isDirectory: true)
    }

    public func deleteMedia(for captures: [LocalCapture]) {
        var urls = Set<URL>()
        for capture in captures {
            for photo in capture.photos {
                if let url = photo.fileURL {
                    urls.insert(url)
                }
            }
            for audio in capture.audioAttachments {
                if let url = audio.fileURL {
                    urls.insert(url)
                }
            }
        }

        deleteManagedMediaURLs(urls)
    }

    public func deletePendingMedia(photos: [CapturePhoto], audio: CaptureAudio? = nil) {
        var urls = Set<URL>()
        for photo in photos {
            if let url = photo.fileURL {
                urls.insert(url)
            }
        }
        if let url = audio?.fileURL {
            urls.insert(url)
        }

        deleteManagedMediaURLs(urls)
    }

    private func deleteManagedMediaURLs(_ urls: Set<URL>) {
        for url in urls where isManagedMediaURL(url) {
            try? FileManager.default.removeItem(at: url)
            removeEmptyParentDirectories(startingAt: url.deletingLastPathComponent())
        }
    }

    public func releaseManagedMedia(for capture: LocalCapture) -> LocalCapture {
        deleteMedia(for: [capture])
        var released = capture
        released.photos = capture.photos.map { photo in
            var releasedPhoto = photo
            if let fileURL = photo.fileURL, isManagedMediaURL(fileURL) {
                releasedPhoto.fileURL = nil
            }
            return releasedPhoto
        }
        released.audios = capture.audioAttachments.map { audio in
            var releasedAudio = audio
            if let fileURL = audio.fileURL, isManagedMediaURL(fileURL) {
                releasedAudio.fileURL = nil
            }
            return releasedAudio
        }
        released.audio = released.audios.first
        return released
    }

    public static func defaultRootDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("Media", isDirectory: true)
    }

    private func safeStem(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics.union(CharacterSet(charactersIn: "-_"))
        let characters = value.unicodeScalars.map { scalar in
            allowed.contains(scalar) ? Character(scalar) : "-"
        }
        let collapsed = String(characters)
            .split(separator: "-", omittingEmptySubsequences: true)
            .joined(separator: "-")
        return collapsed.isEmpty ? "media" : String(collapsed.prefix(80))
    }

    private func safeFilename(_ value: String, fallback: String) -> String {
        let raw = value.isEmpty ? fallback : value
        let url = URL(fileURLWithPath: raw)
        let stem = safeStem(url.deletingPathExtension().lastPathComponent)
        let ext = url.pathExtension.isEmpty ? URL(fileURLWithPath: fallback).pathExtension : url.pathExtension
        return ext.isEmpty ? stem : "\(stem).\(ext)"
    }

    private func isManagedMediaURL(_ url: URL) -> Bool {
        let rootPath = rootDirectory.standardizedFileURL.path
        let mediaPath = url.standardizedFileURL.path
        return mediaPath == rootPath || mediaPath.hasPrefix(rootPath + "/")
    }

    private func prepareMediaDirectory(for fileURL: URL) throws {
        try LocalFilePrivacy.prepareDirectory(rootDirectory)
        try LocalFilePrivacy.prepareDirectory(fileURL.deletingLastPathComponent())
    }

    private func removeEmptyParentDirectories(startingAt directory: URL) {
        let rootPath = rootDirectory.standardizedFileURL.path
        var current = directory.standardizedFileURL
        while current.path.hasPrefix(rootPath + "/") {
            guard let contents = try? FileManager.default.contentsOfDirectory(atPath: current.path), contents.isEmpty else {
                return
            }
            try? FileManager.default.removeItem(at: current)
            current.deleteLastPathComponent()
        }
    }
}

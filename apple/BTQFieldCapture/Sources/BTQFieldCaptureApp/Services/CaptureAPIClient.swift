import Foundation

public struct SubmitCaptureResponse: Codable, Equatable, Sendable {
    public var status: String
    public var jobID: String
    public var captureID: String
    public var couchdbDocID: String?
    public var photoCount: Int
    public var audioCount: Int
    public var idempotentReplay: Bool?

    enum CodingKeys: String, CodingKey {
        case status
        case jobID = "job_id"
        case captureID = "capture_id"
        case couchdbDocID = "couchdb_doc_id"
        case photoCount = "photo_count"
        case audioCount = "audio_count"
        case idempotentReplay = "idempotent_replay"
    }
}

public struct CaptureUploadReconciliation: Equatable, Sendable {
    public var liveCaptureIDs: Set<String>
    public var completedResponses: [String: SubmitCaptureResponse]
    public var isAuthoritative: Bool

    public init(
        liveCaptureIDs: Set<String> = [],
        completedResponses: [String: SubmitCaptureResponse] = [:],
        isAuthoritative: Bool = true
    ) {
        self.liveCaptureIDs = liveCaptureIDs
        self.completedResponses = completedResponses
        self.isAuthoritative = isAuthoritative
    }

    public static let unavailable = CaptureUploadReconciliation(isAuthoritative: false)
}

public enum CaptureAPIError: Error, Equatable, LocalizedError, CustomStringConvertible, Sendable {
    case insecureBaseURL
    case invalidResponse
    case unauthorized
    case serverStatus(status: Int, code: String?, message: String?)

    public var errorDescription: String? {
        description
    }

    public var description: String {
        switch self {
        case .insecureBaseURL:
            return "Server URL must use HTTPS"
        case .invalidResponse:
            return "Invalid server response"
        case .unauthorized:
            return "Token is invalid, expired, or revoked"
        case .serverStatus(let status, let code, let message):
            if let message, !message.isEmpty {
                return message
            }
            if let code, !code.isEmpty {
                return code
            }
            return "Server returned HTTP \(status)"
        }
    }
}

public protocol CaptureAPIClient: Sendable {
    func session(baseURL: URL, token: String) async throws -> BTQSession
    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse
    func inbox(baseURL: URL, token: String) async throws -> InboxResponse
    func decideInboxItem(action: InboxDecisionAction, item: InboxItem, reason: String?, baseURL: URL, token: String) async throws -> InboxDecisionResponse
    func decideInboxSet(_ drafts: [InboxSetDecisionEntry], baseURL: URL, token: String) async throws -> InboxSetDecisionResponse
    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse
    func reconcileBackgroundUploads(captureIDs: Set<String>) async -> CaptureUploadReconciliation
    func finishBackgroundUpload(captureID: String) async
    func sweepUploadBodies(preservingCaptureIDs: Set<String>) async
}

public extension CaptureAPIClient {
    func inbox(baseURL: URL, token: String) async throws -> InboxResponse {
        throw CaptureAPIError.serverStatus(status: 501, code: "inbox_unavailable", message: "Inbox review is not available.")
    }

    func decideInboxItem(action: InboxDecisionAction, item: InboxItem, reason: String?, baseURL: URL, token: String) async throws -> InboxDecisionResponse {
        throw CaptureAPIError.serverStatus(status: 501, code: "inbox_unavailable", message: "Inbox review is not available.")
    }

    func decideInboxSet(_ drafts: [InboxSetDecisionEntry], baseURL: URL, token: String) async throws -> InboxSetDecisionResponse {
        throw CaptureAPIError.serverStatus(status: 501, code: "inbox_unavailable", message: "Inbox review is not available.")
    }

    func reconcileBackgroundUploads(captureIDs: Set<String>) async -> CaptureUploadReconciliation {
        .unavailable
    }

    func finishBackgroundUpload(captureID: String) async {}
    func sweepUploadBodies(preservingCaptureIDs: Set<String>) async {}
}

public final class HTTPCaptureAPIClient: CaptureAPIClient, @unchecked Sendable {
    /// Foreground session for the JSON GET/POST calls (session, submissions, inbox, decisions).
    let session: URLSession
    /// The capture upload runs on this — a background uploader in production, so it survives the
    /// phone being locked mid-transfer.
    private let uploader: any CaptureUploader
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private let uploadBodyDirectory: URL
    private let uploadBodyLock = NSLock()
    private var reservedUploadBodyCaptureIDs: [String: Int] = [:]

    public init(
        session: URLSession = BackgroundUploadSupport.makeForegroundUploadSession(),
        uploader: any CaptureUploader = BackgroundUploader.shared,
        uploadBodyDirectory: URL = BackgroundUploadSupport.defaultUploadRootDirectory()
            .appendingPathComponent("Bodies", isDirectory: true)
    ) {
        self.session = session
        self.uploader = uploader
        self.uploadBodyDirectory = uploadBodyDirectory
        encoder = JSONEncoder()
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
    }

    public func session(baseURL: URL, token: String) async throws -> BTQSession {
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: "api/session"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(BTQSession.self, from: data)
    }

    public func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: "api/my-submissions"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(MySubmissionsResponse.self, from: data)
    }

    public func inbox(baseURL: URL, token: String) async throws -> InboxResponse {
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: "api/inbox"))
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.cachePolicy = .reloadIgnoringLocalCacheData

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(InboxResponse.self, from: data)
    }

    public func decideInboxItem(
        action: InboxDecisionAction,
        item: InboxItem,
        reason: String?,
        baseURL: URL,
        token: String
    ) async throws -> InboxDecisionResponse {
        let path = action == .approve ? "api/inbox/approve" : "api/inbox/reject"
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: path))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try encoder.encode(InboxDecisionRequest(draftID: item.draftID, revision: item.revision, reason: reason))

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        if http.statusCode == 409 {
            return (try? decoder.decode(InboxDecisionResponse.self, from: data))
                ?? InboxDecisionResponse(status: "already_decided")
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(InboxDecisionResponse.self, from: data)
    }

    public func decideInboxSet(
        _ drafts: [InboxSetDecisionEntry],
        baseURL: URL,
        token: String
    ) async throws -> InboxSetDecisionResponse {
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: "api/inbox/approve-set"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.httpBody = try encoder.encode(InboxSetDecisionRequest(drafts: drafts))

        let (data, response) = try await session.data(for: request)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(InboxSetDecisionResponse.self, from: data)
    }

    public func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        reserveUploadBody(for: capture.captureID)
        defer { releaseUploadBodyReservation(for: capture.captureID) }

        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: try secureAPIURL(baseURL: baseURL, path: "api/submit"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let bodyStore = MultipartUploadBodyStore(rootDirectory: uploadBodyDirectory)
        let bodyFile = try bodyStore.makeBodyFileURL(captureID: capture.captureID)
        defer { removeUploadBody(bodyFile, captureID: capture.captureID) }
        try MultipartCaptureBuilder.writeBody(for: capture, boundary: boundary, to: bodyFile)
        try LocalFilePrivacy.protectExistingItem(bodyFile)

        let (data, response) = try await uploader.upload(request, fromFile: bodyFile, captureID: capture.captureID)
        guard let http = response as? HTTPURLResponse else {
            throw CaptureAPIError.invalidResponse
        }
        if http.statusCode == 401 {
            throw CaptureAPIError.unauthorized
        }
        guard (200..<300).contains(http.statusCode) else {
            throw serverError(status: http.statusCode, data: data)
        }
        return try decoder.decode(SubmitCaptureResponse.self, from: data)
    }

    public func reconcileBackgroundUploads(captureIDs: Set<String>) async -> CaptureUploadReconciliation {
        let snapshot = await uploader.reconciliationSnapshot()
        guard snapshot.isAuthoritative else { return .unavailable }
        let globallyLiveCaptureIDs = snapshot.liveCaptureIDs.union(uploadBodyReservations())

        var completedResponses: [String: SubmitCaptureResponse] = [:]
        let relevantCompletions = snapshot.completions
            .filter { captureIDs.contains($0.captureID) }
            .sorted { $0.completedAt > $1.completedAt }
        for completion in relevantCompletions where completedResponses[completion.captureID] == nil {
            guard completion.errorDescription == nil,
                  let statusCode = completion.statusCode,
                  (200..<300).contains(statusCode),
                  let response = try? decoder.decode(SubmitCaptureResponse.self, from: completion.responseData),
                  response.captureID == completion.captureID else {
                continue
            }
            completedResponses[completion.captureID] = response
        }

        let failedCaptureIDs = Set(relevantCompletions.map(\.captureID))
            .subtracting(completedResponses.keys)
        for captureID in failedCaptureIDs {
            await uploader.discardCompletion(for: captureID)
        }

        return CaptureUploadReconciliation(
            liveCaptureIDs: globallyLiveCaptureIDs.subtracting(completedResponses.keys),
            completedResponses: completedResponses,
            isAuthoritative: true
        )
    }

    public func finishBackgroundUpload(captureID: String) async {
        await uploader.discardCompletion(for: captureID)
        uploadBodyLock.withLock {
            guard reservedUploadBodyCaptureIDs[captureID] == nil else { return }
            MultipartUploadBodyStore(rootDirectory: uploadBodyDirectory).removeBodies(for: captureID)
        }
    }

    public func sweepUploadBodies(preservingCaptureIDs: Set<String>) async {
        let snapshot = await uploader.reconciliationSnapshot()
        guard snapshot.isAuthoritative else { return }
        uploadBodyLock.withLock {
            let globallyPreservedCaptureIDs = preservingCaptureIDs
                .union(snapshot.liveCaptureIDs)
                .union(reservedUploadBodyCaptureIDs.keys)
            MultipartUploadBodyStore(rootDirectory: uploadBodyDirectory)
                .sweep(preservingCaptureIDs: globallyPreservedCaptureIDs)
        }
    }

    private func reserveUploadBody(for captureID: String) {
        uploadBodyLock.withLock {
            reservedUploadBodyCaptureIDs[captureID, default: 0] += 1
        }
    }

    private func releaseUploadBodyReservation(for captureID: String) {
        uploadBodyLock.withLock {
            guard let reservationCount = reservedUploadBodyCaptureIDs[captureID] else { return }
            if reservationCount == 1 {
                reservedUploadBodyCaptureIDs.removeValue(forKey: captureID)
            } else {
                reservedUploadBodyCaptureIDs[captureID] = reservationCount - 1
            }
        }
    }

    private func uploadBodyReservations() -> Set<String> {
        uploadBodyLock.withLock { Set(reservedUploadBodyCaptureIDs.keys) }
    }

    private func removeUploadBody(_ bodyFile: URL, captureID: String) {
        uploadBodyLock.withLock {
            if reservedUploadBodyCaptureIDs[captureID] == 1 {
                MultipartUploadBodyStore(rootDirectory: uploadBodyDirectory)
                    .removeBodies(for: captureID)
            } else {
                try? FileManager.default.removeItem(at: bodyFile)
            }
        }
    }

    private func secureAPIURL(baseURL: URL, path: String) throws -> URL {
        guard baseURL.btqUsesHTTPS else {
            throw CaptureAPIError.insecureBaseURL
        }
        return baseURL.appendingPathComponent(path)
    }

    private func serverError(status: Int, data: Data) -> CaptureAPIError {
        let payload = try? decoder.decode(CaptureAPIErrorPayload.self, from: data)
        return .serverStatus(status: status, code: payload?.error, message: payload?.message)
    }
}

private struct MultipartUploadBodyStore: Sendable {
    let rootDirectory: URL

    func makeBodyFileURL(captureID: String) throws -> URL {
        let directory = captureDirectory(captureID: captureID)
        try LocalFilePrivacy.prepareDirectory(rootDirectory)
        try LocalFilePrivacy.prepareDirectory(directory)
        return directory.appendingPathComponent("body-\(UUID().uuidString).multipart")
    }

    func removeBodies(for captureID: String) {
        try? FileManager.default.removeItem(at: captureDirectory(captureID: captureID))
    }

    func sweep(preservingCaptureIDs: Set<String>) {
        guard let entries = try? FileManager.default.contentsOfDirectory(
            at: rootDirectory,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        let preservedDirectoryNames = Set(preservingCaptureIDs.map(encodedCaptureID))
        for entry in entries where !preservedDirectoryNames.contains(entry.lastPathComponent) {
            try? FileManager.default.removeItem(at: entry)
        }
    }

    private func captureDirectory(captureID: String) -> URL {
        rootDirectory.appendingPathComponent(encodedCaptureID(captureID), isDirectory: true)
    }

    private func encodedCaptureID(_ captureID: String) -> String {
        Data(captureID.utf8)
            .base64EncodedString()
            .replacingOccurrences(of: "/", with: "_")
            .replacingOccurrences(of: "+", with: "-")
            .replacingOccurrences(of: "=", with: "")
    }
}

private struct CaptureAPIErrorPayload: Decodable {
    var error: String?
    var message: String?
}

private struct InboxDecisionRequest: Encodable {
    var draftID: String
    var revision: String
    var reason: String?

    enum CodingKeys: String, CodingKey {
        case draftID = "draft_id"
        case revision = "_rev"
        case reason
    }
}

private struct InboxSetDecisionRequest: Encodable {
    var drafts: [InboxSetDecisionEntry]
}

public enum MultipartCaptureBuilder {
    public static func fields(for capture: LocalCapture) -> [(String, String)] {
        var fields = [
            ("job_id", capture.jobID),
            ("capture_id", capture.captureID),
            ("site", capture.siteLabel),
            ("site_id", capture.siteID),
            ("target_type", capture.targetType),
            ("target_id", capture.targetID),
            ("qc_category", capture.qcCategory),
            ("note", capture.note),
            ("captured_at", BTQFormatting.fieldTimestamp(capture.capturedAt)),
            ("exported_at", BTQFormatting.fieldTimestamp(capture.exportedAt)),
        ]
        if let photoNotesJSON = photoNotesJSON(for: capture.photos) {
            fields.append(("photo_notes_json", photoNotesJSON))
        }
        if let audioDurationsJSON = audioDurationsJSON(for: capture.audioAttachments) {
            fields.append(("audio_durations_json", audioDurationsJSON))
        }
        if let metadataJSON = metadataJSON(for: capture) {
            fields.append(("metadata_json", metadataJSON))
        }
        return fields
    }

    public static func body(for capture: LocalCapture, boundary: String) throws -> Data {
        var body = Data()
        for (name, value) in fields(for: capture) {
            body.appendFormField(name: name, value: value, boundary: boundary)
        }
        for photo in capture.photos {
            try body.appendFileField(
                name: "photos",
                filename: photo.filename,
                mimeType: photo.mimeType,
                fileURL: photo.fileURL,
                boundary: boundary
            )
        }
        for audio in capture.audioAttachments {
            try body.appendFileField(
                name: "audio",
                filename: audio.filename,
                mimeType: audio.mimeType,
                fileURL: audio.fileURL,
                boundary: boundary
            )
        }
        if let audio = capture.audioAttachments.first {
            body.appendFormField(name: "audio_duration_seconds", value: String(audio.durationSeconds), boundary: boundary)
        }
        body.appendString("--\(boundary)--\r\n")
        return body
    }

    public static func writeBody(for capture: LocalCapture, boundary: String, to fileURL: URL) throws {
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        FileManager.default.createFile(atPath: fileURL.path, contents: nil)
        let handle = try FileHandle(forWritingTo: fileURL)
        defer { try? handle.close() }

        for (name, value) in fields(for: capture) {
            try handle.write(contentsOf: formFieldData(name: name, value: value, boundary: boundary))
        }
        for photo in capture.photos {
            try handle.write(contentsOf: fileHeaderData(name: "photos", filename: photo.filename, mimeType: photo.mimeType, boundary: boundary))
            if let fileURL = photo.fileURL {
                try writeFileContents(from: fileURL, to: handle)
            }
            try handle.write(contentsOf: Data("\r\n".utf8))
        }
        for audio in capture.audioAttachments {
            try handle.write(contentsOf: fileHeaderData(name: "audio", filename: audio.filename, mimeType: audio.mimeType, boundary: boundary))
            if let fileURL = audio.fileURL {
                try writeFileContents(from: fileURL, to: handle)
            }
            try handle.write(contentsOf: Data("\r\n".utf8))
        }
        if let audio = capture.audioAttachments.first {
            try handle.write(contentsOf: formFieldData(name: "audio_duration_seconds", value: String(audio.durationSeconds), boundary: boundary))
        }
        try handle.write(contentsOf: Data("--\(boundary)--\r\n".utf8))
    }

    private static func formFieldData(name: String, value: String, boundary: String) -> Data {
        Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"\r\n\r\n\(value)\r\n".utf8)
    }

    private static func fileHeaderData(name: String, filename: String, mimeType: String, boundary: String) -> Data {
        Data("--\(boundary)\r\nContent-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\nContent-Type: \(mimeType)\r\n\r\n".utf8)
    }

    private static func writeFileContents(from sourceURL: URL, to output: FileHandle) throws {
        let input = try FileHandle(forReadingFrom: sourceURL)
        defer { try? input.close() }

        while let chunk = try input.read(upToCount: 256 * 1024), !chunk.isEmpty {
            try output.write(contentsOf: chunk)
        }
    }

    private static func photoNotesJSON(for photos: [CapturePhoto]) -> String? {
        let notes = photos.enumerated().compactMap { index, photo -> MultipartPhotoNotePayload? in
            let note = photo.note.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !note.isEmpty else { return nil }
            return MultipartPhotoNotePayload(index: index, filename: photo.filename, note: note)
        }
        guard !notes.isEmpty, let data = try? JSONEncoder().encode(notes) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func audioDurationsJSON(for audios: [CaptureAudio]) -> String? {
        let durations = audios.enumerated().map { index, audio in
            MultipartAudioDurationPayload(index: index, filename: audio.filename, durationSeconds: audio.durationSeconds)
        }
        guard durations.count > 1, let data = try? JSONEncoder().encode(durations) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func metadataJSON(for capture: LocalCapture) -> String? {
        let audioAttachments = capture.audioAttachments
        let payload = MultipartCaptureMetadataPayload(
            visitID: capture.visitID?.uuidString,
            siteID: capture.siteID,
            targetType: capture.targetType,
            targetID: capture.targetID,
            qcCategory: capture.qcCategory,
            assetKind: assetKind(for: capture).rawValue,
            photoCount: capture.photos.count,
            hasAudio: !audioAttachments.isEmpty,
            audioCount: audioAttachments.count,
            audioDurationSeconds: audioAttachments.first?.durationSeconds
        )
        guard let data = try? JSONEncoder().encode(payload) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func assetKind(for capture: LocalCapture) -> CaptureAssetKind {
        if !capture.photos.isEmpty && !capture.audioAttachments.isEmpty {
            return .photoVoice
        }
        if !capture.photos.isEmpty {
            return .photo
        }
        if !capture.audioAttachments.isEmpty {
            return .voice
        }
        return .text
    }
}

private struct MultipartPhotoNotePayload: Codable {
    var index: Int
    var filename: String
    var note: String
}

private struct MultipartAudioDurationPayload: Codable {
    var index: Int
    var filename: String
    var durationSeconds: Double

    enum CodingKeys: String, CodingKey {
        case index
        case filename
        case durationSeconds = "duration_seconds"
    }
}

private struct MultipartCaptureMetadataPayload: Codable {
    var schemaVersion = 1
    var client = "btq_native_apple"
    var visitID: String?
    var siteID: String
    var targetType: String
    var targetID: String
    var qcCategory: String
    var assetKind: String
    var photoCount: Int
    var hasAudio: Bool
    var audioCount: Int
    var audioDurationSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case client
        case visitID = "visit_id"
        case siteID = "site_id"
        case targetType = "target_type"
        case targetID = "target_id"
        case qcCategory = "qc_category"
        case assetKind = "asset_kind"
        case photoCount = "photo_count"
        case hasAudio = "has_audio"
        case audioCount = "audio_count"
        case audioDurationSeconds = "audio_duration_seconds"
    }
}

private extension Data {
    mutating func appendFormField(name: String, value: String, boundary: String) {
        appendString("--\(boundary)\r\n")
        appendString("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
        appendString("\(value)\r\n")
    }

    mutating func appendFileField(name: String, filename: String, mimeType: String, fileURL: URL?, boundary: String) throws {
        appendString("--\(boundary)\r\n")
        appendString("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n")
        appendString("Content-Type: \(mimeType)\r\n\r\n")
        if let fileURL {
            append(try Data(contentsOf: fileURL))
        }
        appendString("\r\n")
    }

    mutating func appendString(_ string: String) {
        append(Data(string.utf8))
    }
}

public actor MockCaptureAPIClient: CaptureAPIClient {
    public var submitted: [LocalCapture] = []

    public init() {}

    public func session(baseURL: URL, token: String) async throws -> BTQSession {
        BTQSession.demo
    }

    public func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    public func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submitted.append(capture)
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

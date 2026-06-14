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

public enum CaptureAPIError: Error, Equatable, LocalizedError, CustomStringConvertible, Sendable {
    case invalidResponse
    case unauthorized
    case serverStatus(status: Int, code: String?, message: String?)

    public var errorDescription: String? {
        description
    }

    public var description: String {
        switch self {
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
    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse
}

public final class HTTPCaptureAPIClient: CaptureAPIClient, @unchecked Sendable {
    let session: URLSession
    private let decoder: JSONDecoder
    private let uploadBodyDirectory: URL

    public init(
        session: URLSession = BackgroundUploadSupport.makeForegroundUploadSession(),
        uploadBodyDirectory: URL = FileManager.default.temporaryDirectory
    ) {
        self.session = session
        self.uploadBodyDirectory = uploadBodyDirectory
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
    }

    public func session(baseURL: URL, token: String) async throws -> BTQSession {
        var request = URLRequest(url: baseURL.appendingPathComponent("api/session"))
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
        var request = URLRequest(url: baseURL.appendingPathComponent("api/my-submissions"))
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

    public func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        let boundary = "Boundary-\(UUID().uuidString)"
        var request = URLRequest(url: baseURL.appendingPathComponent("api/submit"))
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")
        let bodyFile = uploadBodyDirectory.appendingPathComponent("btq-upload-\(capture.captureID)-\(UUID().uuidString).multipart")
        try MultipartCaptureBuilder.writeBody(for: capture, boundary: boundary, to: bodyFile)
        defer { try? FileManager.default.removeItem(at: bodyFile) }

        let (data, response) = try await session.upload(for: request, fromFile: bodyFile)
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

    private func serverError(status: Int, data: Data) -> CaptureAPIError {
        let payload = try? decoder.decode(CaptureAPIErrorPayload.self, from: data)
        return .serverStatus(status: status, code: payload?.error, message: payload?.message)
    }
}

private struct CaptureAPIErrorPayload: Decodable {
    var error: String?
    var message: String?
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
        if let audio = capture.audio {
            try body.appendFileField(
                name: "audio",
                filename: audio.filename,
                mimeType: audio.mimeType,
                fileURL: audio.fileURL,
                boundary: boundary
            )
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
                try handle.write(contentsOf: Data(contentsOf: fileURL))
            }
            try handle.write(contentsOf: Data("\r\n".utf8))
        }
        if let audio = capture.audio {
            try handle.write(contentsOf: fileHeaderData(name: "audio", filename: audio.filename, mimeType: audio.mimeType, boundary: boundary))
            if let fileURL = audio.fileURL {
                try handle.write(contentsOf: Data(contentsOf: fileURL))
            }
            try handle.write(contentsOf: Data("\r\n".utf8))
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

    private static func photoNotesJSON(for photos: [CapturePhoto]) -> String? {
        let notes = photos.enumerated().compactMap { index, photo -> MultipartPhotoNotePayload? in
            let note = photo.note.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !note.isEmpty else { return nil }
            return MultipartPhotoNotePayload(index: index, filename: photo.filename, note: note)
        }
        guard !notes.isEmpty, let data = try? JSONEncoder().encode(notes) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func metadataJSON(for capture: LocalCapture) -> String? {
        let payload = MultipartCaptureMetadataPayload(
            visitID: capture.visitID?.uuidString,
            siteID: capture.siteID,
            targetType: capture.targetType,
            targetID: capture.targetID,
            qcCategory: capture.qcCategory,
            assetKind: assetKind(for: capture).rawValue,
            photoCount: capture.photos.count,
            hasAudio: capture.audio != nil,
            audioDurationSeconds: capture.audio?.durationSeconds
        )
        guard let data = try? JSONEncoder().encode(payload) else { return nil }
        return String(data: data, encoding: .utf8)
    }

    private static func assetKind(for capture: LocalCapture) -> CaptureAssetKind {
        if !capture.photos.isEmpty && capture.audio != nil {
            return .photoVoice
        }
        if !capture.photos.isEmpty {
            return .photo
        }
        if capture.audio != nil {
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
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

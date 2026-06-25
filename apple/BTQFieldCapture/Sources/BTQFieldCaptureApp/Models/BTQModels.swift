import Foundation

public struct BTQAccount: Identifiable, Codable, Equatable, Sendable {
    public var id: UUID
    public var label: String
    public var baseURL: URL
    public var tokenID: String?
    public var personID: String?
    public var personName: String?
    public var tokenRole: String?

    public init(
        id: UUID = UUID(),
        label: String,
        baseURL: URL,
        tokenID: String? = nil,
        personID: String? = nil,
        personName: String? = nil,
        tokenRole: String? = nil
    ) {
        self.id = id
        self.label = label
        self.baseURL = baseURL
        self.tokenID = tokenID
        self.personID = personID
        self.personName = personName
        self.tokenRole = tokenRole
    }
}

public struct BTQAccountWorkspace: Identifiable, Codable, Equatable, Sendable {
    public var id: UUID { account.id }
    public var account: BTQAccount
    public var session: BTQSession?
    public var sites: [BTQSite]
    public var visits: [Visit]
    public var captures: [LocalCapture]

    public init(
        account: BTQAccount,
        session: BTQSession? = nil,
        sites: [BTQSite] = [],
        visits: [Visit] = [],
        captures: [LocalCapture] = []
    ) {
        self.account = account
        self.session = session
        self.sites = sites
        self.visits = visits
        self.captures = captures
    }
}

public struct BTQSession: Codable, Equatable, Sendable {
    public var person: BTQPerson
    public var token: BTQToken
    public var sites: [BTQSite]
    public var canSubmit: Bool
    public var canReview: Bool
    public var maxImages: Int
    public var inboxCount: Int

    public init(
        person: BTQPerson,
        token: BTQToken,
        sites: [BTQSite],
        canSubmit: Bool,
        canReview: Bool,
        maxImages: Int,
        inboxCount: Int = 0
    ) {
        self.person = person
        self.token = token
        self.sites = sites
        self.canSubmit = canSubmit
        self.canReview = canReview
        self.maxImages = maxImages
        self.inboxCount = inboxCount
    }

    enum CodingKeys: String, CodingKey {
        case person
        case token
        case sites
        case canSubmit = "can_submit"
        case canReview = "can_review"
        case maxImages = "max_images"
        case inboxCount = "inbox_count"
    }
}

public struct BTQPerson: Codable, Equatable, Sendable {
    public var personID: String
    public var name: String

    public init(personID: String, name: String) {
        self.personID = personID
        self.name = name
    }

    enum CodingKeys: String, CodingKey {
        case personID = "person_id"
        case name
    }
}

public struct BTQToken: Codable, Equatable, Sendable {
    public var tokenID: String
    public var label: String
    public var role: String?
    public var tokenType: String?

    public init(tokenID: String, label: String, role: String? = nil, tokenType: String? = nil) {
        self.tokenID = tokenID
        self.label = label
        self.role = role
        self.tokenType = tokenType
    }

    enum CodingKeys: String, CodingKey {
        case tokenID = "token_id"
        case label
        case role
        case tokenType = "token_type"
    }
}

public struct MySubmissionsResponse: Codable, Equatable, Sendable {
    public var submissions: [SubmittedCapture]
    public var qualitySummary: SubmissionQualitySummary?

    public init(submissions: [SubmittedCapture], qualitySummary: SubmissionQualitySummary? = nil) {
        self.submissions = submissions
        self.qualitySummary = qualitySummary
    }

    enum CodingKeys: String, CodingKey {
        case submissions
        case qualitySummary = "quality_summary"
    }
}

public struct SubmittedCapture: Identifiable, Codable, Equatable, Sendable {
    public var id: String { captureID }
    public var captureID: String
    public var siteID: String
    public var siteName: String
    public var targetType: String
    public var targetID: String
    public var capturedAt: String
    public var photoCount: Int
    public var hasAudio: Bool
    public var hasTextNote: Bool
    public var noteText: String
    public var photoURLs: [String]
    public var track: String
    public var stage: String
    public var retargetable: Bool
    public var outcomeLabel: String
    public var perPhotoQuality: [SubmittedPhotoQuality]

    public init(
        captureID: String,
        siteID: String,
        siteName: String,
        targetType: String = "location",
        targetID: String,
        capturedAt: String,
        photoCount: Int = 0,
        hasAudio: Bool = false,
        hasTextNote: Bool = false,
        noteText: String = "",
        photoURLs: [String] = [],
        track: String = "",
        stage: String = "",
        retargetable: Bool = false,
        outcomeLabel: String = "",
        perPhotoQuality: [SubmittedPhotoQuality] = []
    ) {
        self.captureID = captureID
        self.siteID = siteID
        self.siteName = siteName
        self.targetType = targetType
        self.targetID = targetID
        self.capturedAt = capturedAt
        self.photoCount = photoCount
        self.hasAudio = hasAudio
        self.hasTextNote = hasTextNote
        self.noteText = noteText
        self.photoURLs = photoURLs
        self.track = track
        self.stage = stage
        self.retargetable = retargetable
        self.outcomeLabel = outcomeLabel
        self.perPhotoQuality = perPhotoQuality
    }

    enum CodingKeys: String, CodingKey {
        case captureID = "capture_id"
        case siteID = "site_id"
        case siteName = "site_name"
        case targetType = "target_type"
        case targetID = "target_id"
        case capturedAt = "captured_at"
        case photoCount = "photo_count"
        case hasAudio = "has_audio"
        case hasTextNote = "has_text_note"
        case noteText = "note_text"
        case photoURLs = "photo_urls"
        case track
        case stage
        case retargetable
        case outcomeLabel = "outcome_label"
        case perPhotoQuality = "per_photo_quality"
    }
}

public struct SubmittedPhotoQuality: Codable, Equatable, Sendable {
    public var severity: String
    public var flags: [String]
    public var description: String
    public var possibleIssues: [String]

    public init(severity: String, flags: [String] = [], description: String = "", possibleIssues: [String] = []) {
        self.severity = severity
        self.flags = flags
        self.description = description
        self.possibleIssues = possibleIssues
    }

    enum CodingKeys: String, CodingKey {
        case severity
        case flags
        case description
        case possibleIssues = "possible_issues"
    }
}

public struct SubmissionQualitySummary: Codable, Equatable, Sendable {
    public var totalProcessed: Int
    public var clear: Int
    public var flagCounts: [String: Int]

    public init(totalProcessed: Int, clear: Int, flagCounts: [String: Int] = [:]) {
        self.totalProcessed = totalProcessed
        self.clear = clear
        self.flagCounts = flagCounts
    }

    enum CodingKeys: String, CodingKey {
        case totalProcessed = "total_processed"
        case clear
        case flagCounts = "flag_counts"
    }
}

public struct InboxResponse: Codable, Equatable, Sendable {
    public var count: Int
    public var items: [InboxItem]

    public init(count: Int, items: [InboxItem]) {
        self.count = count
        self.items = items
    }
}

public struct InboxItem: Identifiable, Codable, Equatable, Sendable {
    public var id: String { draftID }
    public var draftID: String
    public var revision: String
    public var sourceCaptureID: String
    public var source: String
    public var message: String
    public var evidence: String
    public var site: String
    public var siteID: String
    public var groupID: String
    public var submitterName: String
    public var createdAt: String
    public var jobType: String
    public var payload: [String: JSONValue]

    public init(
        draftID: String,
        revision: String,
        sourceCaptureID: String = "",
        source: String = "",
        message: String = "",
        evidence: String = "",
        site: String = "",
        siteID: String = "",
        groupID: String = "",
        submitterName: String = "",
        createdAt: String = "",
        jobType: String,
        payload: [String: JSONValue] = [:]
    ) {
        self.draftID = draftID
        self.revision = revision
        self.sourceCaptureID = sourceCaptureID
        self.source = source
        self.message = message
        self.evidence = evidence
        self.site = site
        self.siteID = siteID
        self.groupID = groupID
        self.submitterName = submitterName
        self.createdAt = createdAt
        self.jobType = jobType
        self.payload = payload
    }

    enum CodingKeys: String, CodingKey {
        case draftID = "draft_id"
        case revision = "_rev"
        case sourceCaptureID = "source_capture_id"
        case source
        case message
        case evidence
        case site
        case siteID = "site_id"
        case groupID = "group_id"
        case submitterName = "submitter_name"
        case createdAt = "created_at"
        case jobType = "job_type"
        case payload
    }
}

public struct InboxGroup: Identifiable, Equatable, Sendable {
    public var id: String
    public var items: [InboxItem]

    public init(id: String, items: [InboxItem]) {
        self.id = id
        self.items = items
    }
}

public enum InboxDecisionAction: String, Codable, Equatable, Sendable {
    case approve
    case reject
}

public struct InboxDecisionResponse: Codable, Equatable, Sendable {
    public var ok: Bool?
    public var draftID: String?
    public var status: String?
    public var error: String?
    public var message: String?

    public init(ok: Bool? = nil, draftID: String? = nil, status: String? = nil, error: String? = nil, message: String? = nil) {
        self.ok = ok
        self.draftID = draftID
        self.status = status
        self.error = error
        self.message = message
    }

    public var alreadyDecided: Bool {
        status == "already_decided" || error == "already_decided"
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case draftID = "draft_id"
        case status
        case error
        case message
    }
}

public struct InboxSetDecisionEntry: Codable, Equatable, Sendable {
    public var draftID: String
    public var revision: String
    public var checked: Bool

    public init(draftID: String, revision: String, checked: Bool) {
        self.draftID = draftID
        self.revision = revision
        self.checked = checked
    }

    enum CodingKeys: String, CodingKey {
        case draftID = "draft_id"
        case revision = "_rev"
        case checked
    }
}

public struct InboxSetDecisionResponse: Codable, Equatable, Sendable {
    public var ok: Bool?
    public var approved: Int
    public var rejected: Int
    public var alreadyDecided: Int
    public var results: [InboxSetDecisionResult]

    public init(ok: Bool? = nil, approved: Int = 0, rejected: Int = 0, alreadyDecided: Int = 0, results: [InboxSetDecisionResult] = []) {
        self.ok = ok
        self.approved = approved
        self.rejected = rejected
        self.alreadyDecided = alreadyDecided
        self.results = results
    }

    enum CodingKeys: String, CodingKey {
        case ok
        case approved
        case rejected
        case alreadyDecided = "already_decided"
        case results
    }
}

public struct InboxSetDecisionResult: Codable, Equatable, Sendable {
    public var draftID: String
    public var action: String?
    public var status: String?
    public var revision: String?
    public var error: String?
    public var message: String?

    public init(draftID: String, action: String? = nil, status: String? = nil, revision: String? = nil, error: String? = nil, message: String? = nil) {
        self.draftID = draftID
        self.action = action
        self.status = status
        self.revision = revision
        self.error = error
        self.message = message
    }

    enum CodingKeys: String, CodingKey {
        case draftID = "draft_id"
        case action
        case status
        case revision = "_rev"
        case error
        case message
    }
}

public enum JSONValue: Codable, Equatable, Sendable, CustomStringConvertible {
    case string(String)
    case number(Double)
    case bool(Bool)
    case object([String: JSONValue])
    case array([JSONValue])
    case null

    public init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else if let value = try? container.decode(String.self) {
            self = .string(value)
        } else if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
        } else {
            self = .object(try container.decode([String: JSONValue].self))
        }
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    public var description: String {
        switch self {
        case .string(let value):
            value
        case .number(let value):
            value.rounded() == value ? String(Int(value)) : String(value)
        case .bool(let value):
            value ? "true" : "false"
        case .object(let value):
            value
                .sorted { $0.key < $1.key }
                .map { "\($0.key): \($0.value.description)" }
                .joined(separator: ", ")
        case .array(let value):
            value.map(\.description).joined(separator: ", ")
        case .null:
            ""
        }
    }
}

public struct BTQSite: Identifiable, Codable, Equatable, Sendable {
    public var id: String { siteID }
    public var siteID: String
    public var label: String
    public var captureGuidance: String
    public var displayCategories: [BTQDisplayCategory]
    public var isFavorite: Bool
    public var lastUsedAt: Date?

    public init(
        siteID: String,
        label: String,
        captureGuidance: String = "",
        displayCategories: [BTQDisplayCategory] = [],
        isFavorite: Bool = false,
        lastUsedAt: Date? = nil
    ) {
        self.siteID = siteID
        self.label = label
        self.captureGuidance = captureGuidance
        self.displayCategories = displayCategories
        self.isFavorite = isFavorite
        self.lastUsedAt = lastUsedAt
    }

    enum CodingKeys: String, CodingKey {
        case siteID = "site_id"
        case label
        case captureGuidance = "capture_guidance"
        case displayCategories = "display_categories"
        case isFavorite
        case lastUsedAt
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        siteID = try container.decode(String.self, forKey: .siteID)
        label = try container.decode(String.self, forKey: .label)
        captureGuidance = try container.decodeIfPresent(String.self, forKey: .captureGuidance) ?? ""
        displayCategories = try container.decodeIfPresent([BTQDisplayCategory].self, forKey: .displayCategories) ?? []
        isFavorite = try container.decodeIfPresent(Bool.self, forKey: .isFavorite) ?? false
        lastUsedAt = try container.decodeIfPresent(Date.self, forKey: .lastUsedAt)
    }
}

public struct BTQDisplayCategory: Identifiable, Codable, Equatable, Sendable {
    public var id: String { value }
    public var value: String
    public var label: String

    public init(value: String, label: String) {
        self.value = value
        self.label = label
    }

    enum CodingKeys: String, CodingKey {
        case value
        case label
        case canonical
        case slug
        case id
        case key
        case name
    }

    public init(from decoder: Decoder) throws {
        if let singleValueContainer = try? decoder.singleValueContainer(),
           let text = try? singleValueContainer.decode(String.self) {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            self.value = trimmed
            self.label = trimmed
            return
        }

        let container = try decoder.container(keyedBy: CodingKeys.self)
        let labelCandidate = Self.firstPresentString(
            in: container,
            keys: [.label, .name, .value, .canonical, .slug, .id, .key]
        )
        let valueCandidate = Self.firstPresentString(
            in: container,
            keys: [.canonical, .value, .slug, .id, .key, .name, .label]
        )

        guard let resolvedValue = valueCandidate ?? labelCandidate else {
            throw DecodingError.keyNotFound(
                CodingKeys.value,
                DecodingError.Context(
                    codingPath: decoder.codingPath,
                    debugDescription: "Display category requires a value, canonical, slug, id, key, name, or label."
                )
            )
        }

        self.value = resolvedValue
        self.label = labelCandidate ?? resolvedValue
    }

    public func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(value, forKey: .value)
        try container.encode(label, forKey: .label)
    }

    private static func firstPresentString(
        in container: KeyedDecodingContainer<CodingKeys>,
        keys: [CodingKeys]
    ) -> String? {
        for key in keys {
            if let value = try? container.decodeIfPresent(String.self, forKey: key)?
                .trimmingCharacters(in: .whitespacesAndNewlines),
               !value.isEmpty {
                return value
            }
        }
        return nil
    }
}

public struct Visit: Identifiable, Codable, Equatable, Sendable {
    public var id: UUID
    public var siteID: String
    public var siteLabel: String
    public var startedAt: Date
    public var endedAt: Date?

    public init(id: UUID = UUID(), siteID: String, siteLabel: String, startedAt: Date = .now, endedAt: Date? = nil) {
        self.id = id
        self.siteID = siteID
        self.siteLabel = siteLabel
        self.startedAt = startedAt
        self.endedAt = endedAt
    }
}

public enum CaptureQueueStatus: String, Codable, CaseIterable, Sendable {
    case draft
    case pending
    case uploading
    case done
    case failed
}

public enum CaptureAssetKind: String, Codable, Sendable {
    case photo
    case voice
    case text
    case photoVoice = "photo-voice"
}

public struct LocalCapture: Identifiable, Codable, Equatable, Sendable {
    public var id: String { captureID }
    public var captureID: String
    public var jobID: String
    public var visitID: UUID?
    public var siteID: String
    public var siteLabel: String
    public var targetType: String
    public var targetID: String
    public var qcCategory: String
    public var note: String
    public var capturedAt: Date
    public var exportedAt: Date
    public var status: CaptureQueueStatus
    public var attempts: Int
    public var lastError: String?
    public var lastTriedAt: Date?
    public var retryAfter: Date?
    public var photos: [CapturePhoto]
    public var audio: CaptureAudio?

    public init(
        captureID: String,
        jobID: String,
        visitID: UUID?,
        siteID: String,
        siteLabel: String,
        targetType: String = "location",
        targetID: String,
        qcCategory: String,
        note: String,
        capturedAt: Date,
        exportedAt: Date,
        status: CaptureQueueStatus = .pending,
        attempts: Int = 0,
        lastError: String? = nil,
        lastTriedAt: Date? = nil,
        retryAfter: Date? = nil,
        photos: [CapturePhoto] = [],
        audio: CaptureAudio? = nil
    ) {
        self.captureID = captureID
        self.jobID = jobID
        self.visitID = visitID
        self.siteID = siteID
        self.siteLabel = siteLabel
        self.targetType = targetType
        self.targetID = targetID
        self.qcCategory = qcCategory
        self.note = note
        self.capturedAt = capturedAt
        self.exportedAt = exportedAt
        self.status = status
        self.attempts = attempts
        self.lastError = lastError
        self.lastTriedAt = lastTriedAt
        self.retryAfter = retryAfter
        self.photos = photos
        self.audio = audio
    }

    public var failureRecoveryHint: String? {
        guard status == .failed, let lastError, !lastError.isEmpty else { return nil }
        let normalized = lastError.localizedLowercase
        if normalized.contains("missing photo file") || normalized.contains("missing audio file") {
            return "Delete this local capture and capture it again; the saved media file is no longer on this device."
        }
        if normalized.contains("too many") || normalized.contains("at most") || normalized.contains("max images") {
            return "Delete and resave this capture with fewer photos."
        }
        return "Retry after the issue is fixed, or delete and capture it again."
    }
}

public struct CapturePhoto: Identifiable, Codable, Equatable, Sendable {
    public var id: UUID
    public var filename: String
    public var mimeType: String
    public var fileURL: URL?
    public var note: String

    public init(id: UUID = UUID(), filename: String, mimeType: String = "image/jpeg", fileURL: URL? = nil, note: String = "") {
        self.id = id
        self.filename = filename
        self.mimeType = mimeType
        self.fileURL = fileURL
        self.note = note
    }
}

public struct CaptureAudio: Identifiable, Codable, Equatable, Sendable {
    public var id: UUID
    public var filename: String
    public var mimeType: String
    public var fileURL: URL?
    public var durationSeconds: Double

    public init(
        id: UUID = UUID(),
        filename: String,
        mimeType: String = "audio/mp4",
        fileURL: URL? = nil,
        durationSeconds: Double = 0
    ) {
        self.id = id
        self.filename = filename
        self.mimeType = mimeType
        self.fileURL = fileURL
        self.durationSeconds = durationSeconds
    }
}

public struct QueueSummary: Equatable, Sendable {
    public var pending: Int
    public var uploading: Int
    public var done: Int
    public var failed: Int

    public init(captures: [LocalCapture]) {
        pending = captures.filter { $0.status == .pending }.count
        uploading = captures.filter { $0.status == .uploading }.count
        done = captures.filter { $0.status == .done }.count
        failed = captures.filter { $0.status == .failed }.count
    }
}

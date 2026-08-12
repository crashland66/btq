import Foundation
import OSLog

public struct ReconciliationFieldDiagnosticRecord: Codable, Equatable, Identifiable, Sendable {
    public enum TaskDescription: Codable, Equatable, Sendable {
        case nilValue
        case empty
        case value(String)

        fileprivate init(_ value: String?) {
            switch value {
            case nil:
                self = .nilValue
            case "":
                self = .empty
            case let value?:
                self = .value(value)
            }
        }

        fileprivate var rendered: String {
            switch self {
            case .nilValue:
                "nil"
            case .empty:
                "empty(\"\")"
            case .value(let value):
                "value(\(Self.quoted(value)))"
            }
        }

        fileprivate static func quoted(_ value: String) -> String {
            guard let data = try? JSONEncoder().encode(value),
                  let quoted = String(data: data, encoding: .utf8) else {
                return "\"<unrenderable>\""
            }
            return quoted
        }
    }

    public let id: UUID
    public let timestamp: Date
    public let isAuthoritative: Bool
    public let taskDescriptions: [TaskDescription]?
    public let liveCaptureIDs: [String]
    public let confirmedCaptureIDs: [String]
    public let strandedCaptureIDs: [String]

    public init(
        id: UUID = UUID(),
        timestamp: Date = .now,
        isAuthoritative: Bool,
        taskDescriptions: [String?]?,
        liveCaptureIDs: Set<String>,
        confirmedCaptureIDs: Set<String>,
        strandedCaptureIDs: Set<String>
    ) {
        self.id = id
        self.timestamp = timestamp
        self.isAuthoritative = isAuthoritative
        self.taskDescriptions = taskDescriptions?.map(TaskDescription.init)
        self.liveCaptureIDs = liveCaptureIDs.sorted()
        self.confirmedCaptureIDs = confirmedCaptureIDs.sorted()
        self.strandedCaptureIDs = strandedCaptureIDs.sorted()
    }

    public var renderedText: String {
        var lines = [
            "timestamp = \(BTQFormatting.fieldTimestamp(timestamp))",
            "authoritative = \(isAuthoritative)",
        ]

        if let taskDescriptions {
            lines.append("outstanding_tasks = \(taskDescriptions.count)")
            if taskDescriptions.isEmpty {
                lines.append("task_descriptions = none (no outstanding tasks)")
            } else {
                for (index, description) in taskDescriptions.enumerated() {
                    lines.append("task[\(index)].taskDescription = \(description.rendered)")
                }
            }
        } else {
            lines.append("outstanding_tasks = unavailable")
            lines.append("task_descriptions = unavailable (snapshot not authoritative)")
        }

        lines.append("live_capture_ids = \(Self.renderedIDs(liveCaptureIDs))")
        lines.append("confirmed_capture_ids = \(Self.renderedIDs(confirmedCaptureIDs))")
        lines.append("stranded_capture_ids = \(Self.renderedIDs(strandedCaptureIDs))")
        return lines.joined(separator: "\n")
    }

    private static func renderedIDs(_ values: [String]) -> String {
        guard !values.isEmpty else { return "[]" }
        return "[\(values.map(TaskDescription.quoted).joined(separator: ", "))]"
    }
}

struct ReconciliationFieldDiagnosticRecorder {
    static let retainedRunCount = 8
    static let logger = Logger(
        subsystem: "com.btq.fieldcapture",
        category: "reconciliation-field-diagnostic"
    )

    private let fileURL: URL

    init(fileURL: URL? = nil) {
        self.fileURL = fileURL ?? Self.defaultFileURL()
    }

    func load() -> [ReconciliationFieldDiagnosticRecord] {
        guard let data = try? Data(contentsOf: fileURL),
              let records = try? JSONDecoder().decode(
                  [ReconciliationFieldDiagnosticRecord].self,
                  from: data
              ) else {
            return []
        }
        return records.sorted { $0.timestamp > $1.timestamp }
    }

    func record(_ record: ReconciliationFieldDiagnosticRecord) -> [ReconciliationFieldDiagnosticRecord] {
        let records = Array(
            ([record] + load())
                .sorted { $0.timestamp > $1.timestamp }
                .prefix(Self.retainedRunCount)
        )
        Self.logger.info("\(record.renderedText, privacy: .public)")

        do {
            try LocalFilePrivacy.prepareDirectory(fileURL.deletingLastPathComponent())
            let data = try JSONEncoder().encode(records)
            try data.write(to: fileURL, options: [.atomic])
            try LocalFilePrivacy.protectExistingItem(fileURL)
        } catch {
            Self.logger.error("Could not persist reconciliation field diagnostic.")
        }
        return records
    }

    private static func defaultFileURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("ReconciliationFieldDiagnostics.json")
    }
}

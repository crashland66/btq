import Foundation
import OSLog

#if os(iOS)
import AVFoundation
import Darwin
#endif

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

struct CameraCapabilityFieldDiagnosticRecord: Codable, Equatable, Identifiable, Sendable {
    let id: UUID
    let timestamp: Date
    let cameraFacing: String
    let isZeroShutterLagSupported: Bool
    let isZeroShutterLagEnabled: Bool
    let isResponsiveCaptureSupported: Bool
    let isResponsiveCaptureEnabled: Bool
    let isFastCapturePrioritizationSupported: Bool
    let isFastCapturePrioritizationEnabled: Bool
    let isAutoDeferredPhotoDeliverySupported: Bool
    let isAutoDeferredPhotoDeliveryEnabled: Bool
    let maxPhotoQualityPrioritization: String

    init(
        id: UUID = UUID(),
        timestamp: Date = .now,
        cameraFacing: String,
        isZeroShutterLagSupported: Bool,
        isZeroShutterLagEnabled: Bool,
        isResponsiveCaptureSupported: Bool,
        isResponsiveCaptureEnabled: Bool,
        isFastCapturePrioritizationSupported: Bool,
        isFastCapturePrioritizationEnabled: Bool,
        isAutoDeferredPhotoDeliverySupported: Bool,
        isAutoDeferredPhotoDeliveryEnabled: Bool,
        maxPhotoQualityPrioritization: String
    ) {
        self.id = id
        self.timestamp = timestamp
        self.cameraFacing = cameraFacing
        self.isZeroShutterLagSupported = isZeroShutterLagSupported
        self.isZeroShutterLagEnabled = isZeroShutterLagEnabled
        self.isResponsiveCaptureSupported = isResponsiveCaptureSupported
        self.isResponsiveCaptureEnabled = isResponsiveCaptureEnabled
        self.isFastCapturePrioritizationSupported = isFastCapturePrioritizationSupported
        self.isFastCapturePrioritizationEnabled = isFastCapturePrioritizationEnabled
        self.isAutoDeferredPhotoDeliverySupported = isAutoDeferredPhotoDeliverySupported
        self.isAutoDeferredPhotoDeliveryEnabled = isAutoDeferredPhotoDeliveryEnabled
        self.maxPhotoQualityPrioritization = maxPhotoQualityPrioritization
    }

    var renderedText: String {
        [
            "timestamp = \(BTQFormatting.fieldTimestamp(timestamp))",
            "camera_facing = \(cameraFacing)",
            "zero_shutter_lag.supported = \(isZeroShutterLagSupported)",
            "zero_shutter_lag.enabled = \(isZeroShutterLagEnabled)",
            "responsive_capture.supported = \(isResponsiveCaptureSupported)",
            "responsive_capture.enabled = \(isResponsiveCaptureEnabled)",
            "fast_capture_prioritization.supported = \(isFastCapturePrioritizationSupported)",
            "fast_capture_prioritization.enabled = \(isFastCapturePrioritizationEnabled)",
            "auto_deferred_photo_delivery.supported = \(isAutoDeferredPhotoDeliverySupported)",
            "auto_deferred_photo_delivery.enabled = \(isAutoDeferredPhotoDeliveryEnabled)",
            "max_photo_quality_prioritization = \(maxPhotoQualityPrioritization)",
        ].joined(separator: "\n")
    }
}

private protocol FieldDiagnosticRecord: Codable {
    var timestamp: Date { get }
    var renderedText: String { get }
}

extension ReconciliationFieldDiagnosticRecord: FieldDiagnosticRecord {}
extension CameraCapabilityFieldDiagnosticRecord: FieldDiagnosticRecord {}

struct ReconciliationFieldDiagnosticRecorder {
    static let retainedRunCount = 8
    static let logger = Logger(
        subsystem: "com.btq.fieldcapture",
        category: "reconciliation-field-diagnostic"
    )

    private let fileURL: URL
    private let cameraCapabilityFileURL: URL

    init(fileURL: URL? = nil, cameraCapabilityFileURL: URL? = nil) {
        self.fileURL = fileURL ?? Self.defaultFileURL()
        self.cameraCapabilityFileURL = cameraCapabilityFileURL ?? Self.defaultCameraCapabilityFileURL()
    }

    func load() -> [ReconciliationFieldDiagnosticRecord] {
        loadRecords(from: fileURL)
    }

    func record(_ record: ReconciliationFieldDiagnosticRecord) -> [ReconciliationFieldDiagnosticRecord] {
        persistRecord(record, to: fileURL, persistenceLabel: "reconciliation")
    }

    func loadCameraCapabilityReadbacks() -> [CameraCapabilityFieldDiagnosticRecord] {
        loadRecords(from: cameraCapabilityFileURL)
    }

    func record(
        _ record: CameraCapabilityFieldDiagnosticRecord
    ) -> [CameraCapabilityFieldDiagnosticRecord] {
        persistRecord(record, to: cameraCapabilityFileURL, persistenceLabel: "camera capability")
    }

    #if os(iOS)
    @discardableResult
    func recordCameraCapabilityReadback(
        from output: AVCapturePhotoOutput,
        cameraPosition: AVCaptureDevice.Position
    ) -> [CameraCapabilityFieldDiagnosticRecord] {
        record(
            CameraCapabilityFieldDiagnosticRecord(
                cameraFacing: Self.renderedCameraFacing(cameraPosition),
                isZeroShutterLagSupported: output.isZeroShutterLagSupported,
                isZeroShutterLagEnabled: output.isZeroShutterLagEnabled,
                isResponsiveCaptureSupported: output.isResponsiveCaptureSupported,
                isResponsiveCaptureEnabled: output.isResponsiveCaptureEnabled,
                isFastCapturePrioritizationSupported: output.isFastCapturePrioritizationSupported,
                isFastCapturePrioritizationEnabled: output.isFastCapturePrioritizationEnabled,
                isAutoDeferredPhotoDeliverySupported: output.isAutoDeferredPhotoDeliverySupported,
                isAutoDeferredPhotoDeliveryEnabled: output.isAutoDeferredPhotoDeliveryEnabled,
                maxPhotoQualityPrioritization: Self.renderedPhotoQualityPrioritization(
                    output.maxPhotoQualityPrioritization
                )
            )
        )
    }
    #endif

    private func loadRecords<Record: FieldDiagnosticRecord>(from fileURL: URL) -> [Record] {
        guard let data = try? Data(contentsOf: fileURL),
              let records = try? JSONDecoder().decode([Record].self, from: data) else {
            return []
        }
        return records.sorted { $0.timestamp > $1.timestamp }
    }

    private func persistRecord<Record: FieldDiagnosticRecord>(
        _ record: Record,
        to fileURL: URL,
        persistenceLabel: String
    ) -> [Record] {
        let records = Array(
            ([record] + loadRecords(from: fileURL))
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
            Self.logger.error("Could not persist \(persistenceLabel, privacy: .public) field diagnostic.")
        }
        return records
    }

    #if os(iOS)
    private static func renderedCameraFacing(_ position: AVCaptureDevice.Position) -> String {
        switch position {
        case .back:
            "back"
        case .front:
            "front"
        case .unspecified:
            "unspecified"
        @unknown default:
            "unknown"
        }
    }

    private static func renderedPhotoQualityPrioritization(
        _ prioritization: AVCapturePhotoOutput.QualityPrioritization
    ) -> String {
        switch prioritization {
        case .speed:
            "speed"
        case .balanced:
            "balanced"
        case .quality:
            "quality"
        @unknown default:
            "unknown"
        }
    }
    #endif

    private static func defaultFileURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("ReconciliationFieldDiagnostics.json")
    }

    private static func defaultCameraCapabilityFileURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("CameraCapabilityFieldDiagnostics.json")
    }
}

#if os(iOS)
enum ReconciliationFieldDiagnosticTermination {
    static func terminateImmediately() -> Never {
        Darwin.abort()
    }
}
#endif

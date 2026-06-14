import Foundation

public enum BTQFormatting {
    public static func fieldTimestamp(_ date: Date = .now) -> String {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter.string(from: date)
    }

    public static func fileStem(_ value: String) -> String {
        let allowed = CharacterSet.alphanumerics
        let scalars = value.lowercased().unicodeScalars.map { scalar in
            allowed.contains(scalar) ? Character(scalar) : "-"
        }
        let collapsed = String(scalars)
            .split(separator: "-", omittingEmptySubsequences: true)
            .joined(separator: "-")
        return String(collapsed.prefix(48)).isEmpty ? "field-capture" : String(collapsed.prefix(48))
    }

    public static func makeCaptureID(capturedAt: Date = .now, suffix: String = UUID().uuidString.prefix(8).lowercased()) -> String {
        let stamp = fieldTimestamp(capturedAt)
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: ".", with: "-")
        return "cap-unified-\(stamp)-\(suffix)"
    }

    public static func makeJobID(exportedAt: Date = .now, assetKind: CaptureAssetKind, siteLabel: String, suffix: String) -> String {
        let stamp = fieldTimestamp(exportedAt)
            .replacingOccurrences(of: ":", with: "-")
            .replacingOccurrences(of: ".", with: "-")
        return "\(stamp)__\(assetKind.rawValue)-capture-\(fileStem(siteLabel))-\(suffix)"
    }
}

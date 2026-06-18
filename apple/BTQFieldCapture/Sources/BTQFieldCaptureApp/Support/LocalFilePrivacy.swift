import Foundation

public enum LocalFilePrivacy {
    public static func prepareDirectory(_ directory: URL) throws {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        try protectExistingItem(directory)
    }

    public static func protectExistingItem(_ url: URL) throws {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        try (url as NSURL).setResourceValue(true, forKey: .isExcludedFromBackupKey)
        try applyDataProtection(to: url)
    }

    #if os(iOS)
    private static func applyDataProtection(to url: URL) throws {
        try FileManager.default.setAttributes(
            [.protectionKey: FileProtectionType.complete],
            ofItemAtPath: url.path
        )
    }
    #else
    private static func applyDataProtection(to url: URL) throws {}
    #endif
}

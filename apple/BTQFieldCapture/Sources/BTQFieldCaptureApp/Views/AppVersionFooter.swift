import Foundation
import SwiftUI

struct AppVersionFooter: View {
    var body: some View {
        Text(AppVersion.current.displayString)
            .font(.caption)
            .foregroundStyle(.secondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .accessibilityIdentifier("app.version.footer")
    }
}

struct AppVersion: Equatable, Sendable {
    var shortVersion: String
    var build: String

    static var current: AppVersion {
        let info = Bundle.main.infoDictionary ?? [:]
        let shortVersion = info["CFBundleShortVersionString"] as? String
        let build = info["CFBundleVersion"] as? String
        return AppVersion(
            shortVersion: normalized(shortVersion, fallback: "dev"),
            build: normalized(build, fallback: "local")
        )
    }

    var displayString: String {
        "Version \(shortVersion) (\(build))"
    }

    private static func normalized(_ value: String?, fallback: String) -> String {
        guard let value = value?.trimmingCharacters(in: .whitespacesAndNewlines),
              !value.isEmpty else {
            return fallback
        }
        return value
    }
}

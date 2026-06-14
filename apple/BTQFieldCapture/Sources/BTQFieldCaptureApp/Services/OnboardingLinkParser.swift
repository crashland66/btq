import Foundation

public enum OnboardingLinkParser {
    public static func token(from url: URL) -> String? {
        if let components = URLComponents(url: url, resolvingAgainstBaseURL: false),
           let token = sanitizedToken(
            components.queryItems?.first(where: { $0.name == "token" || $0.name == "access" })?.value
           ) {
            return token
        }

        if let token = onboardingPathToken(from: url) {
            return token
        }

        guard let fragment = url.fragment, !fragment.isEmpty else {
            return nil
        }
        if fragment.contains("="),
           let components = URLComponents(string: "btq://fragment?\(fragment)"),
           let token = sanitizedToken(
            components.queryItems?.first(where: { $0.name == "token" || $0.name == "access" })?.value
           ) {
            return token
        }
        return sanitizedToken(fragment)
    }

    private static func onboardingPathToken(from url: URL) -> String? {
        let pathSegments = url.path.split(separator: "/").map(String.init)
        if let token = token(afterOnboardingSegmentIn: pathSegments) {
            return token
        }

        guard let host = url.host, isOnboardingSegment(host) else {
            return nil
        }
        return sanitizedToken(pathSegments.first)
    }

    private static func token(afterOnboardingSegmentIn segments: [String]) -> String? {
        for index in segments.indices where isOnboardingSegment(segments[index]) {
            let nextIndex = segments.index(after: index)
            guard nextIndex < segments.endIndex else {
                return nil
            }
            return sanitizedToken(segments[nextIndex])
        }
        return nil
    }

    private static func isOnboardingSegment(_ value: String) -> Bool {
        let normalized = value.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        return normalized == "onboard" || normalized == "native-onboard"
    }

    private static func sanitizedToken(_ value: String?) -> String? {
        guard let value else {
            return nil
        }
        let decoded = value.removingPercentEncoding ?? value
        let token = decoded.trimmingCharacters(in: .whitespacesAndNewlines)
        return token.isEmpty ? nil : token
    }
}

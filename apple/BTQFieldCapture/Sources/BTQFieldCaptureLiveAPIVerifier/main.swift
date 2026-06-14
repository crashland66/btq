import BTQFieldCaptureApp
import Foundation

@main
struct BTQFieldCaptureLiveAPIVerifier {
    static func main() async throws {
        let environment = ProcessInfo.processInfo.environment
        let baseURLText = environment["BTQ_LIVE_BASE_URL"] ?? "https://fc.gregstoltz.com"
        guard let baseURL = URL(string: baseURLText), baseURL.scheme != nil else {
            throw LiveVerifierError.invalidBaseURL(baseURLText)
        }
        guard let token = environment["BTQ_LIVE_TOKEN"]?.trimmingCharacters(in: .whitespacesAndNewlines),
              !token.isEmpty else {
            throw LiveVerifierError.missingToken
        }

        let submitEnabled = environment["BTQ_LIVE_SUBMIT"] == "1"
        let client = HTTPCaptureAPIClient()
        let session = try await client.session(baseURL: baseURL, token: token)
        try validate(session: session)

        print("live-api: session OK for \(session.person.name); sites=\(session.sites.count); can_submit=\(session.canSubmit)")

        let history = try await client.mySubmissions(baseURL: baseURL, token: token)
        try validate(history: history)
        print("live-api: submitted history OK; submissions=\(history.submissions.count); processed=\(history.qualitySummary?.totalProcessed ?? 0)")

        guard submitEnabled else {
            print("live-api: submit skipped; set BTQ_LIVE_SUBMIT=1 for deliberate text-only smoke submit")
            return
        }
        guard session.canSubmit else {
            throw LiveVerifierError.submitNotAllowed
        }
        guard let site = session.sites.first else {
            throw LiveVerifierError.noAssignedSites
        }

        let now = Date()
        let category = site.displayCategories.first?.value ?? "general_note"
        let suffix = String(UUID().uuidString.prefix(8)).lowercased()
        let capture = LocalCapture(
            captureID: BTQFormatting.makeCaptureID(capturedAt: now, suffix: "live-\(suffix)"),
            jobID: BTQFormatting.makeJobID(exportedAt: now, assetKind: .text, siteLabel: site.label, suffix: "live-\(suffix)"),
            visitID: nil,
            siteID: site.siteID,
            siteLabel: site.label,
            targetID: site.siteID,
            qcCategory: category,
            note: "Native Apple live API smoke. This text-only capture was created by a deliberate verifier run.",
            capturedAt: now,
            exportedAt: now
        )
        let response = try await client.submit(capture: capture, baseURL: baseURL, token: token)
        guard response.status == "submitted",
              response.captureID == capture.captureID else {
            throw LiveVerifierError.invalidSubmitResponse
        }
        print("live-api: text-only submit OK for \(response.captureID)")
    }

    private static func validate(session: BTQSession) throws {
        guard !session.person.personID.isEmpty,
              !session.person.name.isEmpty,
              !session.token.tokenID.isEmpty else {
            throw LiveVerifierError.invalidSession
        }
        guard !session.sites.isEmpty else {
            throw LiveVerifierError.noAssignedSites
        }
    }

    private static func validate(history: MySubmissionsResponse) throws {
        if let summary = history.qualitySummary {
            guard summary.totalProcessed >= 0,
                  summary.clear >= 0,
                  summary.clear <= summary.totalProcessed,
                  summary.flagCounts.values.allSatisfy({ $0 >= 0 }) else {
                throw LiveVerifierError.invalidSubmittedHistory
            }
        }

        for submission in history.submissions {
            guard !submission.captureID.isEmpty,
                  !submission.siteID.isEmpty,
                  !submission.siteName.isEmpty,
                  !submission.capturedAt.isEmpty,
                  submission.photoCount >= 0 else {
                throw LiveVerifierError.invalidSubmittedHistory
            }
        }
    }
}

enum LiveVerifierError: Error, CustomStringConvertible {
    case missingToken
    case invalidBaseURL(String)
    case invalidSession
    case invalidSubmittedHistory
    case noAssignedSites
    case submitNotAllowed
    case invalidSubmitResponse

    var description: String {
        switch self {
        case .missingToken:
            "BTQ_LIVE_TOKEN is required. Run with BTQ_LIVE_TOKEN set locally; do not commit tokens."
        case .invalidBaseURL(let value):
            "BTQ_LIVE_BASE_URL is not a valid URL: \(value)"
        case .invalidSession:
            "Live API returned an incomplete session payload"
        case .invalidSubmittedHistory:
            "Live API returned an invalid submitted history payload"
        case .noAssignedSites:
            "Live API session has no assigned sites to verify"
        case .submitNotAllowed:
            "Live API token cannot submit captures; session verification passed but submit smoke cannot run"
        case .invalidSubmitResponse:
            "Live API returned an invalid submit response"
        }
    }
}

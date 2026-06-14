import BTQFieldCaptureApp
import Foundation

@main
struct BTQFieldCaptureAPISmoke {
    static func main() async throws {
        guard CommandLine.arguments.count == 3,
              let baseURL = URL(string: CommandLine.arguments[1]) else {
            throw SmokeError.usage
        }
        let token = CommandLine.arguments[2]
        let scratch = FileManager.default.temporaryDirectory
            .appendingPathComponent("btq-api-smoke-\(UUID().uuidString)", isDirectory: true)
        try FileManager.default.createDirectory(at: scratch, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: scratch) }

        let photoURL = scratch.appendingPathComponent("smoke-photo.jpg")
        let audioURL = scratch.appendingPathComponent("smoke-voice.m4a")
        try Data("btq-smoke-photo".utf8).write(to: photoURL)
        try Data("btq-smoke-audio".utf8).write(to: audioURL)

        let client = HTTPCaptureAPIClient(uploadBodyDirectory: scratch)
        let session = try await client.session(baseURL: baseURL, token: token)
        guard session.canSubmit, session.sites.contains(where: { $0.siteID == "mock_site_1" }) else {
            throw SmokeError.invalidSession
        }
        let history = try await client.mySubmissions(baseURL: baseURL, token: token)
        guard history.submissions.first?.captureID == "cap-native-smoke-history",
              history.qualitySummary?.clear == 1 else {
            throw SmokeError.invalidSubmittedHistory
        }

        let capture = LocalCapture(
            captureID: "cap-native-smoke-\(UUID().uuidString)",
            jobID: "job-native-smoke",
            visitID: UUID(uuidString: "11111111-1111-1111-1111-111111111111"),
            siteID: "mock_site_1",
            siteLabel: "Mock Site One",
            targetID: "mock_site_1",
            qcCategory: "general_note",
            note: "Native mock API smoke",
            capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
            exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
            photos: [
                CapturePhoto(
                    filename: "smoke-photo.jpg",
                    mimeType: "image/jpeg",
                    fileURL: photoURL,
                    note: "Photo note from native smoke"
                )
            ],
            audio: CaptureAudio(
                filename: "smoke-voice.m4a",
                mimeType: "audio/mp4",
                fileURL: audioURL,
                durationSeconds: 3.5
            )
        )

        let response = try await client.submit(capture: capture, baseURL: baseURL, token: token)
        guard response.status == "submitted",
              response.captureID == capture.captureID,
              response.photoCount == 1,
              response.audioCount == 1 else {
            throw SmokeError.invalidSubmitResponse
        }

        print("mock-api-submit: session and submit smoke passed for \(response.captureID)")
    }
}

enum SmokeError: Error, CustomStringConvertible {
    case usage
    case invalidSession
    case invalidSubmittedHistory
    case invalidSubmitResponse

    var description: String {
        switch self {
        case .usage:
            "usage: BTQFieldCaptureAPISmoke <base-url> <token>"
        case .invalidSession:
            "mock API returned an invalid session payload"
        case .invalidSubmittedHistory:
            "mock API returned an invalid submitted history payload"
        case .invalidSubmitResponse:
            "mock API returned an invalid submit response"
        }
    }
}

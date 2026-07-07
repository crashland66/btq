import Foundation

public enum BackgroundUploadSupport {
    public static let sessionIdentifier = "com.btq.fieldcapture.upload.background"

    public static func backgroundConfiguration() -> URLSessionConfiguration {
        let configuration = URLSessionConfiguration.background(withIdentifier: sessionIdentifier)
        configuration.sessionSendsLaunchEvents = true
        configuration.isDiscretionary = false
        configuration.allowsExpensiveNetworkAccess = true
        configuration.allowsConstrainedNetworkAccess = true
        configuration.waitsForConnectivity = true
        configuration.httpMaximumConnectionsPerHost = 2
        // A big multipart capture on a field connection must not time out mid-transfer.
        configuration.timeoutIntervalForRequest = 300
        configuration.timeoutIntervalForResource = 7 * 24 * 60 * 60
        return configuration
    }

    public static func makeForegroundUploadSession() -> URLSession {
        let configuration = URLSessionConfiguration.default
        configuration.waitsForConnectivity = true
        configuration.allowsExpensiveNetworkAccess = true
        configuration.allowsConstrainedNetworkAccess = true
        configuration.httpMaximumConnectionsPerHost = 2
        return URLSession(configuration: configuration)
    }
}

/// Performs the capture upload. Abstracted so the API client stays testable: production uses
/// `BackgroundUploader` (a background `URLSession` that survives the phone being locked); tests
/// inject a `ForegroundUploader` over a stubbed session (background sessions ignore `URLProtocol`).
public protocol CaptureUploader: Sendable {
    func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse)
}

/// Uploads on a plain (foreground) session via the async convenience — used for tests (a stubbed
/// `URLProtocol` session) and any caller that doesn't need background survival.
public struct ForegroundUploader: CaptureUploader {
    let session: URLSession
    public init(session: URLSession) { self.session = session }
    public func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        try await session.upload(for: request, fromFile: file)
    }
}

/// Uploads a capture on a **background** `URLSession`, so a 20-photo/~50 MB multipart transfer
/// keeps going when the field worker locks the phone (a foreground session is suspended and the
/// transfer cancelled — the root cause of the partial/dropped uploads). Background sessions are
/// delegate-based and don't support the async `upload(for:)` convenience, so this bridges the
/// delegate callbacks back to `async/await` via a per-task continuation.
///
/// If iOS terminates the app mid-upload, the waiting continuation is lost; that edge case
/// self-heals on the next sync — a retry re-submits and the server merges/replays idempotently.
public final class BackgroundUploader: NSObject, CaptureUploader, URLSessionDataDelegate, @unchecked Sendable {
    /// The shared instance the app and the API client use, so the app's background-relaunch
    /// completion handler reaches the same session.
    public static let shared = BackgroundUploader()

    private var session: URLSession!
    private let lock = NSLock()
    private var pending: [Int: PendingUpload] = [:]
    /// Set by the app's `handleEventsForBackgroundURLSession` hook; called once the session has
    /// delivered all pending background events so iOS can suspend the app again.
    private var backgroundCompletionHandler: (@Sendable () -> Void)?

    private struct PendingUpload {
        let continuation: CheckedContinuation<(Data, URLResponse), Error>
        var data: Data
    }

    /// `configuration` defaults to the real background session; tests pass a foreground
    /// configuration (with a stub `URLProtocol`) to exercise the delegate/continuation bridge.
    public init(configuration: URLSessionConfiguration? = nil) {
        super.init()
        self.session = URLSession(
            configuration: configuration ?? BackgroundUploadSupport.backgroundConfiguration(),
            delegate: self,
            delegateQueue: nil
        )
    }

    public func setBackgroundCompletionHandler(_ handler: @escaping @Sendable () -> Void) {
        lock.lock(); backgroundCompletionHandler = handler; lock.unlock()
    }

    public func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        let task = session.uploadTask(with: request, fromFile: file)
        return try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            pending[task.taskIdentifier] = PendingUpload(continuation: continuation, data: Data())
            lock.unlock()
            task.resume()
        }
    }

    // MARK: URLSession delegate

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.lock()
        pending[dataTask.taskIdentifier]?.data.append(data)
        lock.unlock()
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        lock.lock()
        let entry = pending.removeValue(forKey: task.taskIdentifier)
        lock.unlock()
        guard let entry else { return } // no waiter (app was relaunched) — retry reconciles
        if let error {
            entry.continuation.resume(throwing: error)
        } else if let response = task.response {
            entry.continuation.resume(returning: (entry.data, response))
        } else {
            entry.continuation.resume(throwing: CaptureAPIError.invalidResponse)
        }
    }

    public func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        lock.lock()
        let handler = backgroundCompletionHandler
        backgroundCompletionHandler = nil
        lock.unlock()
        handler?()
    }
}

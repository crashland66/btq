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

    public static func defaultUploadRootDirectory() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("BackgroundUploads", isDirectory: true)
    }
}

public struct BackgroundUploadCompletion: Codable, Equatable, Sendable {
    public var captureID: String
    public var statusCode: Int?
    public var responseData: Data
    public var errorDescription: String?
    public var completedAt: Date

    public init(
        captureID: String,
        statusCode: Int?,
        responseData: Data,
        errorDescription: String?,
        completedAt: Date = .now
    ) {
        self.captureID = captureID
        self.statusCode = statusCode
        self.responseData = responseData
        self.errorDescription = errorDescription
        self.completedAt = completedAt
    }
}

public struct BackgroundUploadSnapshot: Equatable, Sendable {
    public var liveCaptureIDs: Set<String>
    public var completions: [BackgroundUploadCompletion]
    public var isAuthoritative: Bool

    public init(
        liveCaptureIDs: Set<String> = [],
        completions: [BackgroundUploadCompletion] = [],
        isAuthoritative: Bool = true
    ) {
        self.liveCaptureIDs = liveCaptureIDs
        self.completions = completions
        self.isAuthoritative = isAuthoritative
    }

    public static let unavailable = BackgroundUploadSnapshot(
        isAuthoritative: false
    )
}

/// Performs the capture upload. Abstracted so the API client stays testable: production uses
/// `BackgroundUploader` (a background `URLSession` that survives the phone being locked); tests
/// inject a `ForegroundUploader` over a stubbed session (background sessions ignore `URLProtocol`).
public protocol CaptureUploader: Sendable {
    func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse)
    func upload(_ request: URLRequest, fromFile file: URL, captureID: String) async throws -> (Data, URLResponse)
    func reconciliationSnapshot() async -> BackgroundUploadSnapshot
    func discardCompletion(for captureID: String) async
}

public extension CaptureUploader {
    func reconciliationSnapshot() async -> BackgroundUploadSnapshot { .unavailable }
    func discardCompletion(for captureID: String) async {}
}

/// Uploads on a plain (foreground) session via the async convenience — used for tests (a stubbed
/// `URLProtocol` session) and any caller that doesn't need background survival.
public struct ForegroundUploader: CaptureUploader {
    let session: URLSession
    public init(session: URLSession) { self.session = session }
    public func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        try await session.upload(for: request, fromFile: file)
    }

    public func upload(
        _ request: URLRequest,
        fromFile file: URL,
        captureID: String
    ) async throws -> (Data, URLResponse) {
        try await session.upload(for: request, fromFile: file)
    }
}

/// Uploads a capture on a **background** `URLSession`, so a large multipart transfer keeps going
/// while the app is suspended. Every production task stores its capture ID in `taskDescription`;
/// successful and failed delegate completions are also written to app-owned storage before an
/// in-memory waiter is resumed. That pair survives process death without relying on task IDs.
public final class BackgroundUploader: NSObject, CaptureUploader, URLSessionDataDelegate, @unchecked Sendable {
    /// The shared instance the app and the API client use, so the app's background-relaunch
    /// completion handler reaches the same session.
    public static let shared = BackgroundUploader()

    private var session: URLSession!
    private let lock = NSLock()
    private var pending: [Int: CheckedContinuation<(Data, URLResponse), Error>] = [:]
    private var responseData: [Int: Data] = [:]
    private let completionStore: BackgroundUploadCompletionStore
    /// Set by the app's `handleEventsForBackgroundURLSession` hook; called once the session has
    /// delivered all pending background events so iOS can suspend the app again.
    private var backgroundCompletionHandler: (@Sendable () -> Void)?

    /// `configuration` defaults to the real background session; tests pass a foreground
    /// configuration (with a stub `URLProtocol`) to exercise the delegate/continuation bridge.
    public init(
        configuration: URLSessionConfiguration? = nil,
        completionDirectory: URL = BackgroundUploadSupport.defaultUploadRootDirectory()
            .appendingPathComponent("Completions", isDirectory: true)
    ) {
        completionStore = BackgroundUploadCompletionStore(directory: completionDirectory)
        super.init()
        self.session = URLSession(
            configuration: configuration ?? BackgroundUploadSupport.backgroundConfiguration(),
            delegate: self,
            delegateQueue: nil
        )
    }

    public func setBackgroundCompletionHandler(_ handler: @escaping @Sendable () -> Void) {
        lock.withLock { backgroundCompletionHandler = handler }
    }

    public func upload(_ request: URLRequest, fromFile file: URL) async throws -> (Data, URLResponse) {
        try await startUpload(request, fromFile: file, captureID: nil)
    }

    public func upload(
        _ request: URLRequest,
        fromFile file: URL,
        captureID: String
    ) async throws -> (Data, URLResponse) {
        try await startUpload(request, fromFile: file, captureID: captureID)
    }

    public func reconciliationSnapshot() async -> BackgroundUploadSnapshot {
        let tasks = await allTasks()
        let liveCaptureIDs = Set(tasks.compactMap(\.taskDescription).filter { !$0.isEmpty })
        let completions = lock.withLock { completionStore.loadAll() }
        return BackgroundUploadSnapshot(
            liveCaptureIDs: liveCaptureIDs,
            completions: completions,
            isAuthoritative: true
        )
    }

    public func discardCompletion(for captureID: String) async {
        lock.withLock { completionStore.removeAll(for: captureID) }
    }

    private func startUpload(
        _ request: URLRequest,
        fromFile file: URL,
        captureID: String?
    ) async throws -> (Data, URLResponse) {
        let task = session.uploadTask(with: request, fromFile: file)
        task.taskDescription = captureID
        return try await withCheckedThrowingContinuation { continuation in
            lock.withLock {
                pending[task.taskIdentifier] = continuation
                responseData[task.taskIdentifier] = Data()
            }
            task.resume()
        }
    }

    private func allTasks() async -> [URLSessionTask] {
        await withCheckedContinuation { continuation in
            session.getAllTasks { tasks in
                continuation.resume(returning: tasks)
            }
        }
    }

    // MARK: URLSession delegate

    public func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        lock.withLock { responseData[dataTask.taskIdentifier, default: Data()].append(data) }
    }

    public func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        let result = lock.withLock { () -> (CheckedContinuation<(Data, URLResponse), Error>?, Data) in
            let continuation = pending.removeValue(forKey: task.taskIdentifier)
            let data = responseData.removeValue(forKey: task.taskIdentifier) ?? Data()
            if let captureID = task.taskDescription, !captureID.isEmpty {
                completionStore.save(
                    BackgroundUploadCompletion(
                        captureID: captureID,
                        statusCode: (task.response as? HTTPURLResponse)?.statusCode,
                        responseData: data,
                        errorDescription: error?.localizedDescription
                    )
                )
            }
            return (continuation, data)
        }
        guard let continuation = result.0 else {
            // A relaunched process has no continuation, but the completion above is durable and
            // will be consumed by `FieldCaptureModel` reconciliation.
            return
        }
        if let error {
            continuation.resume(throwing: error)
        } else if let response = task.response {
            continuation.resume(returning: (result.1, response))
        } else {
            continuation.resume(throwing: CaptureAPIError.invalidResponse)
        }
    }

    public func urlSessionDidFinishEvents(forBackgroundURLSession session: URLSession) {
        let handler = lock.withLock { () -> (@Sendable () -> Void)? in
            let handler = backgroundCompletionHandler
            backgroundCompletionHandler = nil
            return handler
        }
        handler?()
    }
}

private struct BackgroundUploadCompletionStore: Sendable {
    let directory: URL

    func save(_ completion: BackgroundUploadCompletion) {
        do {
            try LocalFilePrivacy.prepareDirectory(directory)
            let file = directory.appendingPathComponent("completion-\(UUID().uuidString).json")
            let data = try JSONEncoder().encode(completion)
            try data.write(to: file, options: [.atomic])
            try LocalFilePrivacy.protectExistingItem(file)
        } catch {
            // The local capture remains uploading and will safely fall back to a retry if this
            // resource-hygiene record cannot be written.
        }
    }

    func loadAll() -> [BackgroundUploadCompletion] {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return []
        }
        return files.compactMap { file in
            guard let data = try? Data(contentsOf: file) else { return nil }
            return try? JSONDecoder().decode(BackgroundUploadCompletion.self, from: data)
        }
    }

    func removeAll(for captureID: String) {
        guard let files = try? FileManager.default.contentsOfDirectory(
            at: directory,
            includingPropertiesForKeys: nil,
            options: [.skipsHiddenFiles]
        ) else {
            return
        }
        for file in files {
            guard let data = try? Data(contentsOf: file),
                  let completion = try? JSONDecoder().decode(BackgroundUploadCompletion.self, from: data),
                  completion.captureID == captureID else {
                continue
            }
            try? FileManager.default.removeItem(at: file)
        }
    }
}

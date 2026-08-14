import Foundation
import Testing

@testable import BTQFieldCaptureApp

// Independent verifier probes for the camera shutter-latency work (prompt 128 and its
// follow-on, which reverted prioritization to `.quality` behind zero-shutter-lag,
// responsive capture and prepared photo settings).
//
// `Views/CameraCaptureView.swift` is entirely `#if os(iOS) && canImport(UIKit)`, so
// `swift test` on macOS never COMPILES it and can never EXECUTE a capture. These are
// source-contract probes only. Behavioural claims about AVFoundation callback ordering,
// proxy delivery, capability support and shutter latency are NOT provable here and are
// reported as such.
//
// Note also that the iOS build of the `BTQFieldCaptureApp` SwiftPM target is compiled by
// Xcode with `-suppress-warnings`, so a green `xcodebuild` says nothing about the Swift 6
// actor-isolation diagnostics this file produces. See the review report.

private func prompt128Source() throws -> String {
    let testFile = URL(fileURLWithPath: #filePath)
    let packageRoot = testFile
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    return try String(
        contentsOf: packageRoot
            .appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CameraCaptureView.swift"),
        encoding: .utf8
    )
}

private func prompt128Slice(_ source: String, from start: String, to end: String) throws -> String {
    guard let startRange = source.range(of: start),
          let endRange = source.range(of: end, range: startRange.upperBound..<source.endIndex)
    else {
        throw Prompt128VerifierError.missingBoundary("\(start) ... \(end)")
    }
    return String(source[startRange.lowerBound..<endRange.lowerBound])
}

private func prompt128Occurrences(of needle: String, in source: String) -> Int {
    source.components(separatedBy: needle).count - 1
}

private func prompt128Ordered(_ needles: [String], in source: String) -> Bool {
    var remainder = source[source.startIndex...]
    for needle in needles {
        guard let range = remainder.range(of: needle) else { return false }
        remainder = remainder[range.upperBound...]
    }
    return true
}

private enum Prompt128VerifierError: Error {
    case missingBoundary(String)
}

/// The whole capability policy, which is now one helper rather than an inline block.
private func prompt128CapabilityPolicySlice() throws -> String {
    try prompt128Slice(
        prompt128Source(),
        from: "private nonisolated func applyPhotoOutputCapabilities",
        to: "private nonisolated func preparePhotoSettings"
    )
}

/// `capturePhoto()` through to the controller's enqueue hop.
private func prompt128CaptureSlice() throws -> String {
    try prompt128Slice(
        prompt128Source(),
        from: "func capturePhoto()",
        to: "private func enqueuePhotoResult"
    )
}

// MARK: - C1: prioritization is `.quality`, from ONE source, on both output and shot

@Test func prompt128VerifierQualityPrioritizationComesFromASingleSourceOnOutputAndShot() throws {
    let source = try prompt128Source()
    let policy = try prompt128CapabilityPolicySlice()

    // The single source of truth.
    #expect(source.contains(
        "static let qualityPrioritization: AVCapturePhotoOutput.QualityPrioritization = .quality"
    ))

    // STRENGTHENED over the previous revision: the output maximum used to be a bare
    // `.balanced` literal independent of the factory constant, so only this test held the
    // two together. It now DERIVES from the constant, which makes the drift structurally
    // impossible. Pin that derivation, and pin that no literal ever creeps back.
    #expect(policy.contains(
        "output.maxPhotoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization"
    ))
    #expect(source.contains(
        "settings.photoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization"
    ))
    #expect(!source.contains("maxPhotoQualityPrioritization = ."))
    #expect(!source.contains("photoQualityPrioritization = ."))

    // Exactly one enum-literal assignment of a prioritization value anywhere in the file —
    // the factory constant. A per-shot value greater than the output maximum makes
    // `capturePhoto(with:delegate:)` throw NSInvalidArgumentException at the shutter.
    #expect(prompt128Occurrences(of: "QualityPrioritization = .", in: source) == 1)
    #expect(prompt128Occurrences(of: "maxPhotoQualityPrioritization", in: source) == 1)
    #expect(prompt128Occurrences(of: "photoQualityPrioritization =", in: source) == 1)
}

// MARK: - C2: capability guards, dependency order, and fast-capture explicitly OFF

@Test func prompt128VerifierCapabilitiesAreGuardedAndFastCapturePrioritizationIsExplicitlyOff() throws {
    let source = try prompt128Source()
    let policy = try prompt128CapabilityPolicySlice()

    // Zero shutter lag, then responsive capture NESTED inside it. AVCapturePhotoOutput.h:
    // "Responsive capture is only supported when zero shutter lag is enabled."
    #expect(prompt128Ordered([
        "if output.isZeroShutterLagSupported {",
        "output.isZeroShutterLagEnabled = true",
        "if output.isResponsiveCaptureSupported {",
        "output.isResponsiveCaptureEnabled = true",
    ], in: policy))

    // Fast capture prioritization is now explicitly DISABLED, not omitted. The header says
    // it "allows capture quality to be automatically reduced from the selected
    // AVCapturePhotoQualityPrioritization ... when captures are requested in rapid
    // succession" — which would silently undercut the `.quality` decision during an
    // 18-20 shot visit. `false` is the whole point; `true` must never come back.
    #expect(prompt128Ordered([
        "if output.isFastCapturePrioritizationSupported {",
        "output.isFastCapturePrioritizationEnabled = false",
    ], in: policy))
    #expect(!source.contains("isFastCapturePrioritizationEnabled = true"))

    // Exactly one enable site per capability.
    for capability in [
        "isZeroShutterLagEnabled",
        "isResponsiveCaptureEnabled",
        "isFastCapturePrioritizationEnabled",
        "isAutoDeferredPhotoDeliveryEnabled",
    ] {
        #expect(prompt128Occurrences(of: capability, in: source) == 1)
    }

    // The policy refuses to touch an output the session does not own.
    #expect(policy.contains("guard session.outputs.contains(where: { $0 === output }) else { return }"))
}

@Test func prompt128VerifierCapabilityPolicyIsReAppliedAfterEveryConfigurationCommit() throws {
    let source = try prompt128Source()

    // The prompt-128 defect was that the capability block ran once, inside
    // `if !isConfigured`, so the camera-flip reconfiguration silently dropped the opt-ins.
    // The invariant that closes it: EVERY `commitConfiguration()` is followed by the
    // policy helper. Counting both is what makes a future third commit site fail here.
    #expect(prompt128Occurrences(of: "commitConfiguration()", in: source) == 2)
    #expect(prompt128Occurrences(of: "applyPhotoOutputCapabilities(", in: source) == 3) // decl + 2 calls

    // Initial configuration: on the session queue, inside begin/commit, policy applied
    // after the commit and BEFORE the session starts running.
    let configure = try prompt128Slice(
        source,
        from: "private func configureAndStart()",
        to: "/// Swap the video input"
    )
    #expect(prompt128Ordered([
        "sessionQueue.async",
        "self.session.beginConfiguration()",
        "self.session.addOutput(self.photoOutput)",
        "self.session.commitConfiguration()",
        "self.applyPhotoOutputCapabilities(to: self.photoOutput, in: self.session)",
        "self.session.startRunning()",
    ], in: configure))

    // Camera flip: same pairing on the input-swap path.
    let applyInput = try prompt128Slice(
        source,
        from: "private func applyInput(for target: CameraFacing",
        to: "/// Camera/format changes may reset these opt-ins"
    )
    #expect(prompt128Ordered([
        "session.commitConfiguration()",
        "applyPhotoOutputCapabilities(to: photoOutput, in: session)",
    ], in: applyInput))

    // And the flip itself is dispatched to the session queue, never run on the main actor.
    #expect(prompt128Ordered([
        "func toggleFacing()",
        "sessionQueue.async",
        "self?.applyInput(for: next)",
    ], in: source))
}

// MARK: - C3: no proxy image can become the evidence file

@Test func prompt128VerifierAutoDeferredDeliveryCannotFileAProxyAsTheBusinessRecord() throws {
    let source = try prompt128Source()

    #expect(source.contains("output.isAutoDeferredPhotoDeliveryEnabled = false"))
    #expect(!source.contains("isAutoDeferredPhotoDeliveryEnabled = true"))

    // The durable form: auto-deferred delivery may only be enabled if the proxy callback
    // is actually handled. Per AVCapturePhotoOutput.h, when a proxy is appropriate the
    // output invokes `didFinishCapturingDeferredPhotoProxy:` INSTEAD OF
    // `didFinishProcessingPhoto:`, so an unhandled proxy would silently drop a photo — and
    // a handled-but-stored proxy would file a low-cost preview as a site's QC record.
    let handlesProxy = source.contains("didFinishCapturingDeferredPhotoProxy")
    let enablesDeferral = source.contains("isAutoDeferredPhotoDeliveryEnabled = true")
    #expect(handlesProxy || !enablesDeferral)

    // The evidence file has exactly one producer. (The file's doc comment also names
    // `fileDataRepresentation()`, so match the call form.)
    #expect(prompt128Occurrences(of: "photo.fileDataRepresentation()", in: source) == 1)
}

// MARK: - C4 (prepared settings): one funnel, so prepared and live cannot diverge

@Test func prompt128VerifierPreparedAndLiveSettingsShareOneFunnelAndCannotDiverge() throws {
    let source = try prompt128Source()

    // Preparation is requested as part of the capability policy, so it is refreshed on
    // every reconfiguration alongside the opt-ins it depends on.
    let policy = try prompt128CapabilityPolicySlice()
    #expect(policy.contains("preparePhotoSettings(for: output)"))

    let prepare = try prompt128Slice(
        source,
        from: "private nonisolated func preparePhotoSettings",
        to: "private nonisolated func applyPhotoSettings"
    )
    #expect(prepare.contains("output.setPreparedPhotoSettingsArray(settings, completionHandler: nil)"))
    #expect(prepare.contains("applyPhotoSettings(settings, flashMode: flashMode)"))
    // Only flash modes this UI can actually request, and only ones the device supports.
    #expect(prepare.contains("let requestedModes: [AVCaptureDevice.FlashMode] = [.off, .on]"))
    #expect(prepare.contains("requestedModes.filter(output.supportedFlashModes.contains)"))

    // The funnel: exactly one declaration and exactly two call sites (prepare + live), and
    // every field either path sets is set ONLY inside the funnel. That is what makes
    // "prepared and requested settings cannot drift" a structural fact rather than a claim.
    #expect(prompt128Occurrences(of: "applyPhotoSettings(", in: source) == 3)
    let funnel = try prompt128Slice(
        source,
        from: "private nonisolated func applyPhotoSettings",
        to: "private static func wideCamera"
    )
    #expect(funnel.contains("settings.photoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization"))
    #expect(funnel.contains("settings.flashMode = flashMode"))
    #expect(prompt128Occurrences(of: "settings.flashMode =", in: source) == 1)

    // The live shot builds a bare settings object and routes it through the same funnel,
    // on the session queue, immediately before the one capture.
    let capture = try prompt128CaptureSlice()
    #expect(prompt128Ordered([
        "sessionQueue.async",
        "let settings = AVCapturePhotoSettings()",
        "self.applyPhotoSettings(settings, flashMode: supportedFlashMode)",
        "self.photoOutput.capturePhoto(with: settings, delegate: delegate)",
    ], in: capture))
    #expect(prompt128Occurrences(of: "AVCapturePhotoSettings()", in: source) == 2) // prepared + live
}

// MARK: - C4 (ordering): FIFO hop, single-flight drain, bounded delegate lifetime

@Test func prompt128VerifierPhotoResultsCrossToTheControllerOnTheMainQueueFIFO() throws {
    let source = try prompt128Source()
    let capture = try prompt128CaptureSlice()

    // AVCapturePhotoOutput.h: callbacks arrive "on a common dispatch queue", and
    // "Processed photos continue to be delivered in the order they were captured."
    // The hop must therefore be a FIFO serial queue. `DispatchQueue.main.async` is
    // documented FIFO; unstructured `Task {}` enqueueing is not.
    #expect(prompt128Ordered([
        "let delegate = PhotoCaptureDelegate { [weak self] result in",
        "DispatchQueue.main.async",
        "self?.enqueuePhotoResult(result)",
    ], in: capture))
    #expect(prompt128Occurrences(of: "DispatchQueue.main.async", in: source) == 1)

    // No unstructured task anywhere between the capture callback and the controller —
    // reintroducing one would reintroduce an unordered enqueue.
    #expect(!capture.contains("Task {"))

    // The controller-side enqueue is a plain FIFO append. Nothing reorders or reindexes.
    #expect(source.contains("pendingPhotoResults.append(result)"))
    #expect(!source.contains("pendingPhotoResults.insert"))
    #expect(!source.contains("pendingPhotoResults.sort"))
    #expect(!source.contains("pendingPhotoResults.removeAll"))
}

@Test func prompt128VerifierDrainIsSingleFlightFIFOAndTheSoleCallerOfOnPhoto() throws {
    let source = try prompt128Source()
    let drain = try prompt128Slice(
        source,
        from: "private func deliverReadyPhotosInOrder",
        to: "// MARK: - Preview layer bridge"
    )

    // Only the drain hands bytes to the draft, and only under a single-flight guard.
    // Without the guard, two drains would interleave across `await onPhoto` — the exact
    // bug that predated this work, when every callback spawned its own awaiting task.
    #expect(prompt128Occurrences(of: "onPhoto?(data)", in: source) == 1)
    #expect(prompt128Ordered([
        "guard !isDeliveringPhotos else { return }",
        "isDeliveringPhotos = true",
        "while !pendingPhotoResults.isEmpty {",
        "let result = pendingPhotoResults.removeFirst()",
        "capturedCount += 1",
        "await onPhoto?(data)",
        "isDeliveringPhotos = false",
    ], in: drain))
    #expect(prompt128Occurrences(of: "removeFirst()", in: source) == 1)
    #expect(prompt128Occurrences(of: "isDeliveringPhotos", in: source) == 4) // decl + guard + set + clear
}

@Test func prompt128VerifierEveryShotHasATerminalDelegateCallbackAndABoundedLifetime() throws {
    let source = try prompt128Source()
    let delegate = try prompt128Slice(
        source,
        from: "private final class PhotoCaptureDelegate",
        to: "// MARK: - Session controller"
    )

    // `didFinishCaptureFor` is documented to always come last for a capture, and is the
    // only thing that turns a capture which never produced a photo into a surfaced error
    // rather than a silently missing shot. It must complete the shot and release the
    // delegate's self-retain.
    #expect(prompt128Ordered([
        "didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings",
        "if !didDeliverResult {",
        "didDeliverResult = true",
        "completion(.failure(",
        "keepAlive = nil",
    ], in: delegate))

    // Each shot's result is delivered at most once.
    #expect(delegate.contains("guard !didDeliverResult else { return }"))
    #expect(prompt128Occurrences(of: "didDeliverResult = true", in: delegate) == 2)

    // The self-retain that keeps the per-shot delegate alive is taken once and released
    // once. If `didFinishCaptureFor` never arrives, this delegate leaks for the process
    // lifetime, holding its captured completion closure.
    #expect(prompt128Occurrences(of: "keepAlive = self", in: delegate) == 1)
    #expect(prompt128Occurrences(of: "keepAlive = nil", in: delegate) == 1)

    // The delegate is per-shot and carries no controller state of its own.
    #expect(delegate.contains("private let completion: (PhotoCaptureResult) -> Void"))
    #expect(!delegate.contains("weak var"))
}

// MARK: - Concurrency: no escape hatches, configuration confined to the session queue

@Test func prompt128VerifierNoConcurrencyEscapeHatchesAndConfigurationStaysOnSessionQueue() throws {
    let source = try prompt128Source()

    // None of the usual ways to silence the isolation checker rather than satisfy it.
    for escape in [
        "@unchecked Sendable",
        "nonisolated(unsafe)",
        "assumeIsolated",
        "@preconcurrency",
        "unsafeBitCast",
    ] {
        #expect(!source.contains(escape))
    }

    // Exactly three `nonisolated` helpers, and each takes the AVFoundation objects it
    // touches as parameters rather than reaching through main-actor state.
    #expect(prompt128Occurrences(of: "nonisolated", in: source) == 3)
    #expect(source.contains("private nonisolated func applyPhotoOutputCapabilities(\n        to output: AVCapturePhotoOutput,\n        in session: AVCaptureSession\n    )"))
    #expect(source.contains("private nonisolated func preparePhotoSettings(for output: AVCapturePhotoOutput)"))
    #expect(source.contains("private nonisolated func applyPhotoSettings(\n        _ settings: AVCapturePhotoSettings,\n        flashMode: AVCaptureDevice.FlashMode?\n    )"))

    // Every session mutation is dispatched to the session queue, never performed inline on
    // the main actor.
    #expect(prompt128Occurrences(of: "beginConfiguration()", in: source) == 2)
    #expect(prompt128Occurrences(of: "startRunning()", in: source) == 1)
    #expect(prompt128Occurrences(of: "stopRunning()", in: source) == 1)
    for dispatched in [
        "private func configureAndStart() {\n        sessionQueue.async",
        "func stop() {\n        sessionQueue.async",
        "func toggleFacing() {\n        let next = facing.toggled\n        sessionQueue.async",
    ] {
        #expect(source.contains(dispatched))
    }
}

// MARK: - C5 / I1: the evidence bytes and what the camera captures

@Test func prompt128VerifierNoReEncodeAnywhereInTheCameraFile() throws {
    let source = try prompt128Source()

    // Whole-file, not a slice: an earlier test migration split the byte contract into two
    // disjoint slices and left the middle of the evidence path uncovered. The camera file
    // has no legitimate use of any of these.
    for reEncode in [
        "UIImage(data:",
        "jpegData(",
        "pngData(",
        "cgImageRepresentation",
        "CGImageDestination",
        "CIImage(",
        "previewPixelBuffer",
    ] {
        #expect(!source.contains(reEncode))
    }

    // The bytes handed to the draft are the same value the delegate produced: the `Data`
    // passes only through the result enum, the FIFO buffer and the drain.
    #expect(source.contains("case success(Data)"))
    #expect(source.contains("completion(.success(data))"))
    #expect(source.contains("case .success(let data):"))
    #expect(source.contains("await onPhoto?(data)"))
}

@Test func prompt128VerifierCaptureResolutionAndPhotoCountAreUntouched() throws {
    let source = try prompt128Source()

    // I1: nothing in this work constrains or alters what the camera captures.
    #expect(source.contains("self.session.sessionPreset = .photo"))
    #expect(!source.contains("maxPhotoDimensions"))
    #expect(!source.contains("photoSettingsForSceneMonitoring"))
    #expect(source.contains("let settings = AVCapturePhotoSettings()"))
    #expect(prompt128Occurrences(of: "capturePhoto(with:", in: source) == 1)
    #expect(prompt128Occurrences(of: "capturedCount += 1", in: source) == 1)
}

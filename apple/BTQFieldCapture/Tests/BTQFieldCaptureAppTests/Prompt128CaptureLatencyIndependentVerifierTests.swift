import Foundation
import Testing

@testable import BTQFieldCaptureApp

// Independent verifier probes for prompt 128 (shutter-latency change).
//
// `Views/CameraCaptureView.swift` is entirely `#if os(iOS) && canImport(UIKit)`, so
// `swift test` on macOS never COMPILES it and can never EXECUTE a capture. These are
// source-contract probes only. Behavioural claims about AVFoundation callback ordering,
// proxy delivery and capability support are NOT provable here and are reported as such.

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

private func prompt128OutputConfigurationSlice() throws -> String {
    try prompt128Slice(
        prompt128Source(),
        from: "if self.session.canAddOutput(self.photoOutput) {",
        to: "self.session.commitConfiguration()"
    )
}

// MARK: - C1: quality prioritization is balanced, and the two sites cannot drift apart

@Test func prompt128VerifierQualityPrioritizationIsBalancedOnBothOutputMaximumAndEachShot() throws {
    let source = try prompt128Source()
    let configuration = try prompt128OutputConfigurationSlice()

    // The output maximum.
    #expect(configuration.contains("self.photoOutput.maxPhotoQualityPrioritization = .balanced"))
    #expect(prompt128Occurrences(of: "maxPhotoQualityPrioritization", in: source) == 1)

    // The per-shot value, and the single constant it comes from.
    #expect(source.contains(
        "static let qualityPrioritization: AVCapturePhotoOutput.QualityPrioritization = .balanced"
    ))
    #expect(source.contains(
        "settings.photoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization"
    ))
    #expect(prompt128Occurrences(of: "settings.photoQualityPrioritization =", in: source) == 1)

    // Nothing anywhere still asks for `.quality` prioritization. A per-shot value greater
    // than the output maximum makes `capturePhoto(with:delegate:)` throw
    // NSInvalidArgumentException, which crashes the app at the shutter.
    #expect(!source.contains("QualityPrioritization = .quality"))
    #expect(!source.contains("photoQualityPrioritization = .quality"))

    // VERIFIER NOTE: the output maximum is a bare `.balanced` literal, NOT
    // `CameraCaptureSettingsFactory.qualityPrioritization`. The two sites are
    // independent, so raising the factory constant alone silently violates
    // "per-shot may not exceed the output maximum". This test is the only thing
    // holding them together. See the review report.
}

// MARK: - C2: every adopted capability is support-guarded, in dependency order

@Test func prompt128VerifierEveryShutterLagCapabilityIsGuardedByItsSupportCompanion() throws {
    let configuration = try prompt128OutputConfigurationSlice()

    // Zero shutter lag, then responsive capture NESTED inside it. AVCapturePhotoOutput.h:
    // "Responsive capture is only supported when zero shutter lag is enabled."
    #expect(prompt128Ordered([
        "if self.photoOutput.isZeroShutterLagSupported {",
        "self.photoOutput.isZeroShutterLagEnabled = true",
        "if self.photoOutput.isResponsiveCaptureSupported {",
        "self.photoOutput.isResponsiveCaptureEnabled = true",
    ], in: configuration))

    // Fast capture prioritization. The header says setting it to YES when
    // `fastCapturePrioritizationSupported` is NO throws NSInvalidArgumentException,
    // and that the property is only supported while responsive capture is enabled —
    // so the support guard is load-bearing, not decorative.
    #expect(prompt128Ordered([
        "if self.photoOutput.isFastCapturePrioritizationSupported {",
        "self.photoOutput.isFastCapturePrioritizationEnabled = true",
    ], in: configuration))

    // Exactly one enable site per capability: an unguarded second assignment elsewhere
    // would be an exception at configuration time on unsupported hardware.
    for capability in [
        "isZeroShutterLagEnabled",
        "isResponsiveCaptureEnabled",
        "isFastCapturePrioritizationEnabled",
        "isAutoDeferredPhotoDeliveryEnabled",
    ] {
        #expect(prompt128Occurrences(of: capability, in: try prompt128Source()) == 1)
    }

    // Every capability is set inside the session's own begin/commit transaction.
    let transaction = try prompt128Slice(
        prompt128Source(),
        from: "self.session.beginConfiguration()",
        to: "self.session.commitConfiguration()"
    )
    for capability in [
        "isZeroShutterLagEnabled = true",
        "isResponsiveCaptureEnabled = true",
        "isFastCapturePrioritizationEnabled = true",
        "isAutoDeferredPhotoDeliveryEnabled = false",
    ] {
        #expect(transaction.contains(capability))
    }

    // VERIFIER NOTE (open defect): this transaction runs ONLY inside `if !self.isConfigured`.
    // `applyInput` — reached by the on-screen camera-flip button — reconfigures the session
    // without re-applying any of these. AVCapturePhotoOutput.h: "When switching cameras or
    // formats this property may change... If you've previously opted in for fast capture
    // prioritization and then change configurations, you may need to set
    // fastCapturePrioritizationEnabled = YES again." See the review report.
    #expect(prompt128Occurrences(of: "if !self.isConfigured {", in: try prompt128Source()) == 1)
}

// MARK: - C3: no proxy image can become the evidence file

@Test func prompt128VerifierAutoDeferredDeliveryCannotFileAProxyAsTheBusinessRecord() throws {
    let source = try prompt128Source()

    #expect(source.contains("self.photoOutput.isAutoDeferredPhotoDeliveryEnabled = false"))
    #expect(!source.contains("isAutoDeferredPhotoDeliveryEnabled = true"))

    // The real invariant, stated so it survives a future change of heart: auto-deferred
    // delivery may only be enabled if the proxy callback is actually handled. Per
    // AVCapturePhotoOutput.h, when a proxy is appropriate the output invokes
    // `didFinishCapturingDeferredPhotoProxy:` INSTEAD OF `didFinishProcessingPhoto:`,
    // and enabling deferral without that delegate method is a documented capture error.
    let handlesProxy = source.contains("didFinishCapturingDeferredPhotoProxy")
    let enablesDeferral = source.contains("isAutoDeferredPhotoDeliveryEnabled = true")
    #expect(handlesProxy || !enablesDeferral)

    // The evidence file has exactly one producer. (The doc comment at the top of the file
    // also names `fileDataRepresentation()`, so match the call form.)
    #expect(prompt128Occurrences(of: "photo.fileDataRepresentation()", in: source) == 1)
}

// MARK: - C4: the shutter sequence cannot skip a number

@Test func prompt128VerifierShutterSequenceIsAllocatedOnceBeforeTheSessionQueueHop() throws {
    let source = try prompt128Source()
    let capture = try prompt128Slice(
        source,
        from: "func capturePhoto()",
        to: "private func enqueuePhotoResult"
    )

    // The sequence is taken on the MainActor, in shutter-tap order, BEFORE the hop to the
    // session queue — that is what makes the sequence equal the order the operator tapped.
    #expect(prompt128Ordered([
        "let captureSequence = nextCaptureSequence",
        "nextCaptureSequence += 1",
        "sessionQueue.async",
        "SequencedPhotoCaptureDelegate(sequence: captureSequence)",
    ], in: capture))

    // Exactly one allocation site and one increment. The ordered drain waits for
    // CONTIGUOUS sequence numbers, so any sequence that is allocated but never delivered
    // strands every later photo permanently — there is no gap recovery and no timeout.
    #expect(prompt128Occurrences(of: "nextCaptureSequence += 1", in: source) == 1)
    #expect(prompt128Occurrences(of: "nextCaptureSequence", in: source) == 3) // decl + read + increment

    // The delivery cursor advances in exactly one place, inside the drain.
    #expect(prompt128Occurrences(of: "nextPhotoDeliverySequence += 1", in: source) == 1)
}

@Test func prompt128VerifierEveryShotHasATerminalDelegateCallbackThatClosesItsSequence() throws {
    let source = try prompt128Source()
    let delegate = try prompt128Slice(
        source,
        from: "private final class SequencedPhotoCaptureDelegate",
        to: "// MARK: - Session controller"
    )

    // `didFinishCaptureFor` is the ONLY backstop that stops a capture which never produced
    // a photo from stranding every later shot behind a hole in the sequence. It must both
    // complete the sequence and release the delegate's self-retain.
    #expect(prompt128Ordered([
        "didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings",
        "if !didDeliverResult {",
        "didDeliverResult = true",
        "completion(sequence, .failure(",
        "keepAlive = nil",
    ], in: delegate))

    // Each shot's result is delivered at most once.
    #expect(delegate.contains("guard !didDeliverResult else { return }"))
    #expect(prompt128Occurrences(of: "didDeliverResult = true", in: delegate) == 2)

    // The self-retain that keeps the per-shot delegate alive is taken once and released
    // once. If `didFinishCaptureFor` never arrives, this delegate leaks for the process
    // lifetime, holding its captured photo closure.
    #expect(prompt128Occurrences(of: "keepAlive = self", in: delegate) == 1)
    #expect(prompt128Occurrences(of: "keepAlive = nil", in: delegate) == 1)
}

@Test func prompt128VerifierOrderedDrainIsTheSoleCallerOfOnPhotoAndIsSingleFlight() throws {
    let source = try prompt128Source()
    let drain = try prompt128Slice(
        source,
        from: "private func deliverReadyPhotosInOrder",
        to: "// MARK: - Preview layer bridge"
    )

    // Only the ordered drain hands bytes to the draft, and only under a single-flight
    // guard — otherwise two concurrent drains could interleave across `await onPhoto`.
    #expect(prompt128Occurrences(of: "onPhoto?(data)", in: source) == 1)
    #expect(prompt128Ordered([
        "guard !isDeliveringPhotos else { return }",
        "isDeliveringPhotos = true",
        "while let result = pendingPhotoResults.removeValue(forKey: nextPhotoDeliverySequence)",
        "await onPhoto?(data)",
        "isDeliveringPhotos = false",
    ], in: drain))

    // Delivery only ever consumes the NEXT contiguous sequence — never an arbitrary key.
    #expect(prompt128Occurrences(of: "pendingPhotoResults.removeValue", in: source) == 1)
    #expect(!source.contains("pendingPhotoResults.removeAll"))
    #expect(!source.contains("pendingPhotoResults.keys"))
    #expect(!source.contains("pendingPhotoResults.sorted"))
}

// MARK: - C5 / C6: the evidence bytes and everything downstream of them

@Test func prompt128VerifierNoReEncodeAnywhereInTheCameraFile() throws {
    let source = try prompt128Source()

    // Whole-file, not a slice: the prompt-128 test edit split the byte contract into two
    // disjoint slices and left the middle of the path uncovered. The camera file has no
    // legitimate use of any of these.
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

    // The bytes handed to the draft are the same value produced by the delegate: the
    // `Data` never passes through anything but the sequenced result and the drain.
    #expect(source.contains("case success(Data)"))
    #expect(source.contains("completion(sequence, .success(data))"))
    #expect(source.contains("case .success(let data):"))
    #expect(source.contains("await onPhoto?(data)"))
}

@Test func prompt128VerifierCaptureResolutionAndPhotoCountAreUntouched() throws {
    let source = try prompt128Source()

    // I1: nothing in this change constrains or alters what the camera captures.
    #expect(source.contains("self.session.sessionPreset = .photo"))
    #expect(!source.contains("maxPhotoDimensions"))
    #expect(!source.contains("photoSettingsForSceneMonitoring"))
    #expect(!source.contains("maxPhotoQualityPrioritization = .speed"))
    #expect(source.contains("let settings = AVCapturePhotoSettings()"))
    #expect(prompt128Occurrences(of: "capturePhoto(with:", in: source) == 1)
}

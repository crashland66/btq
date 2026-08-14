import Foundation
import Testing

@testable import BTQFieldCaptureApp

// Verifier-owned source-contract probes for prompt 485. These intentionally live
// apart from the implementation-authored ContractTests additions and exercise the
// iOS-only paths even when `swift test` is compiling the shared package for macOS.

@Test func prompt485VerifierRotationAssignmentsRequireAVFoundationSupport() throws {
    let source = try prompt485VerifierSource("Views/CaptureRotation.swift")

    #expect(prompt485Occurrences(of: "connection.videoRotationAngle = angle", in: source) == 2)
    #expect(prompt485Occurrences(of: "connection.isVideoRotationAngleSupported(angle)", in: source) == 2)

    let preview = try prompt485Slice(
        source,
        from: "private func applyPreviewAngle(_ angle: CGFloat)",
        to: "/// Applies the current physical-device angle"
    )
    #expect(prompt485Ordered([
        "connection.isVideoRotationAngleSupported(angle)",
        "connection.videoRotationAngle = angle",
    ], in: preview))

    let capture = try prompt485Slice(
        source,
        from: "func applyCaptureRotation(_ angle: CGFloat?",
        to: "#endif"
    )
    #expect(prompt485Ordered([
        "connection.isVideoRotationAngleSupported(angle)",
        "connection.videoRotationAngle = angle",
    ], in: capture))
}

@Test func prompt485VerifierCaptureStampsOutputOnSessionQueueBeforePhotoCapture() throws {
    let source = try prompt485VerifierSource("Views/CameraCaptureView.swift")
    // Prompt 128 moved the capture delegate out of `CameraSessionController` into a
    // per-shot `SequencedPhotoCaptureDelegate`, so the old `// MARK: - Capture delegate`
    // boundary and the `delegate: self` argument are gone. The invariant this test
    // guards — rotation stamped on the session queue exactly once, before exactly one
    // capture — is unchanged.
    let capture = try prompt485Slice(
        source,
        from: "func capturePhoto()",
        to: "private func enqueuePhotoResult"
    )

    #expect(prompt485Ordered([
        "let rotationAngle = rotation.captureAngle",
        "sessionQueue.async",
        "applyCaptureRotation(rotationAngle, to: self.photoOutput)",
        "let settings = AVCapturePhotoSettings()",
        "self.photoOutput.capturePhoto(with: settings, delegate: delegate)",
    ], in: capture))
    #expect(prompt485Occurrences(of: "applyCaptureRotation(", in: capture) == 1)
    #expect(prompt485Occurrences(of: "capturePhoto(with:", in: capture) == 1)
}

@Test func prompt485VerifierCameraReplacementRebindsRotationAndKeepsInputFallback() throws {
    let source = try prompt485VerifierSource("Views/CameraCaptureView.swift")
    let switchInput = try prompt485Slice(
        source,
        from: "private func applyInput(for target:",
        to: "private static func wideCamera"
    )

    #expect(prompt485Ordered([
        "if let existing = currentInput { session.removeInput(existing) }",
        "if session.canAddInput(input)",
        "session.addInput(input)",
        "currentInput = input",
        "self.rotation.setDevice(device)",
        "else if let existing = currentInput",
        "session.addInput(existing)",
    ], in: switchInput))
    #expect(switchInput.contains("never leave the session with no input"))
}

@Test func prompt485VerifierIOSViewerCoversLocalRemoteStateZoomAndAccessibility() throws {
    let source = try prompt485VerifierSource("Views/CapturePhotoThumbnail.swift")

    let iosPresentation = try prompt485Slice(
        source,
        from: "#if os(iOS)\n        Button",
        to: "#else\n        thumbnail"
    )
    #expect(iosPresentation.contains("selectedPhoto = photo"))
    #expect(iosPresentation.contains(".fullScreenCover(item: $selectedPhoto)"))
    #expect(iosPresentation.contains("CapturePhotoLightbox("))
    #expect(iosPresentation.contains(".accessibilityLabel(\"View full photo\")"))

    let lightbox = try prompt485Slice(
        source,
        from: "private struct CapturePhotoLightbox",
        to: "#endif"
    )
    #expect(lightbox.contains("UIImage(contentsOfFile: fileURL.path)"))
    #expect(prompt485Ordered([
        "if let fileURL = photo.fileURL",
        "UIImage(contentsOfFile: fileURL.path)",
        "guard let remoteURL else { return }",
        "var request = URLRequest(url: remoteURL)",
        "request.setValue(\"Bearer \\(token)\", forHTTPHeaderField: \"Authorization\")",
    ], in: lightbox))
    #expect(lightbox.contains("ProgressView()"))
    #expect(lightbox.contains("Loading full photo"))
    #expect(lightbox.contains("ContentUnavailableView("))
    #expect(lightbox.contains("Photo unavailable"))
    #expect(lightbox.contains("MagnificationGesture()"))
    #expect(lightbox.contains("min(max(scale, 1), 4)"))
    #expect(lightbox.contains(".accessibilityLabel(\"Full photo\")"))
    #expect(lightbox.contains(".accessibilityLabel(\"Close full photo\")"))
}

@Test func prompt485VerifierBearerCredentialsStayInHeadersAndMacThumbnailContractIsUnchanged() throws {
    let source = try prompt485VerifierSource("Views/CapturePhotoThumbnail.swift")

    #expect(prompt485Occurrences(of: "Bearer \\(token)", in: source) == 2)
    #expect(prompt485Occurrences(
        of: "request.setValue(\"Bearer \\(token)\", forHTTPHeaderField: \"Authorization\")",
        in: source
    ) == 2)
    #expect(!source.contains("print("))
    #expect(!source.contains("Logger("))
    #expect(!source.contains("os_log"))

    let urlResolver = try prompt485Slice(
        source,
        from: "private func resolvedRemoteURL(for photo:",
        to: "private func thumbnailImage(from data:"
    )
    #expect(!urlResolver.contains("token"))
    #expect(!urlResolver.contains("Authorization"))

    let nonIOSBody = try prompt485Slice(
        source,
        from: "#else\n        thumbnail",
        to: "#endif\n    }"
    )
    #expect(nonIOSBody.contains("thumbnail"))
    #expect(nonIOSBody.contains(".accessibilityLabel(\"Photo thumbnail\")"))
    #expect(!nonIOSBody.contains("Button"))
    #expect(!nonIOSBody.contains("fullScreenCover"))
}

@Test func prompt485VerifierPrompt480OriginalEncodedByteContractRemainsIntact() throws {
    let source = try prompt485VerifierSource("Views/CameraCaptureView.swift")
    // Prompt 128 replaced the controller-as-delegate extension with a per-shot capture
    // delegate plus a FIFO delivery drain; its follow-on renamed that delegate to
    // `PhotoCaptureDelegate` when the sequence numbers were dropped.
    //
    // VERIFIER CORRECTION (prompt 128 review): the prompt-128 edit split this into two
    // DISJOINT slices — the delegate class and `deliverReadyPhotosInOrder` — which left
    // the middle of the evidence-byte path (the capture completion closure and
    // `enqueuePhotoResult`) covered by NO re-encode assertion, and dropped the
    // `prompt485Ordered` relationship between producing the bytes and handing them off.
    // The original assertion covered the whole path as ONE contiguous span. Restored to
    // one span with the same shape: from the delegate that produces the bytes through to
    // the `// MARK: - Preview layer bridge` boundary. Keep it contiguous.
    let evidencePath = try prompt485Slice(
        source,
        from: "private final class PhotoCaptureDelegate",
        to: "// MARK: - Preview layer bridge"
    )

    #expect(source.contains("import AVFoundation"))
    #expect(source.contains("settings.photoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization"))
    // Prioritization returned to `.quality` (operator decision: evidence sharpness wins),
    // now offset by zero-shutter-lag, responsive capture and prepared photo settings, and
    // with fast-capture prioritization explicitly OFF so nothing may silently drop below
    // the requested level. None of that changes what is STORED —
    // `fileDataRepresentation()` still yields the full original encode, and auto-deferred
    // delivery is disabled so it can never be a proxy image.
    #expect(source.contains("static let qualityPrioritization: AVCapturePhotoOutput.QualityPrioritization = .quality"))
    #expect(source.contains("isAutoDeferredPhotoDeliveryEnabled = false"))
    #expect(source.contains("isFastCapturePrioritizationEnabled = false"))
    #expect(prompt485Ordered([
        "photo.fileDataRepresentation()",
        "await onPhoto?(data)",
    ], in: evidencePath))
    #expect(!evidencePath.contains("UIImage(data:"))
    #expect(!evidencePath.contains("jpegData("))
    #expect(!evidencePath.contains("pngData("))
}

private func prompt485VerifierSource(_ relativePath: String) throws -> String {
    let testFile = URL(fileURLWithPath: #filePath)
    let packageRoot = testFile
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
    return try String(
        contentsOf: packageRoot
            .appendingPathComponent("Sources/BTQFieldCaptureApp")
            .appendingPathComponent(relativePath),
        encoding: .utf8
    )
}

private func prompt485Slice(_ source: String, from start: String, to end: String) throws -> String {
    guard let startRange = source.range(of: start),
          let endRange = source.range(of: end, range: startRange.upperBound..<source.endIndex)
    else {
        throw Prompt485VerifierError.missingBoundary("\(start) ... \(end)")
    }
    return String(source[startRange.lowerBound..<endRange.lowerBound])
}

private func prompt485Occurrences(of needle: String, in source: String) -> Int {
    source.components(separatedBy: needle).count - 1
}

private func prompt485Ordered(_ needles: [String], in source: String) -> Bool {
    var remainder = source[source.startIndex...]
    for needle in needles {
        guard let range = remainder.range(of: needle) else { return false }
        remainder = remainder[range.upperBound...]
    }
    return true
}

private enum Prompt485VerifierError: Error {
    case missingBoundary(String)
}

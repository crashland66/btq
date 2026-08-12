#if os(iOS) && canImport(UIKit)
import AVFoundation
import SwiftUI
import UIKit

// MARK: - Pure capture configuration

/// Which physical camera the session is using. Back wide is the default.
enum CameraFacing: Sendable {
    case back
    case front

    var toggled: CameraFacing { self == .back ? .front : .back }
    var avPosition: AVCaptureDevice.Position { self == .back ? .back : .front }
}

/// Pure capture-settings helpers. Balanced prioritization avoids pictorial-grade
/// pre-capture latency while `fileDataRepresentation()` still yields the full original
/// encode (HEIC/JPEG + EXIF/GPS). The field-capture upload path (`ImageNormalizer`)
/// downsizes for the wire.
enum CameraCaptureSettingsFactory {
    static let qualityPrioritization: AVCapturePhotoOutput.QualityPrioritization = .balanced

    /// The flash mode to request given the user's toggle and whether the active device
    /// actually has a flash (front cameras usually don't).
    static func flashMode(flashOn: Bool, deviceHasFlash: Bool) -> AVCaptureDevice.FlashMode {
        (flashOn && deviceHasFlash) ? .on : .off
    }
}

private enum SequencedPhotoResult {
    case success(Data)
    case failure(String)
}

/// Responsive capture permits photo-processing callbacks to arrive out of shutter order.
/// A delegate per shot carries the shutter sequence into the controller's ordered buffer.
private final class SequencedPhotoCaptureDelegate: NSObject, AVCapturePhotoCaptureDelegate {
    let sequence: UInt64

    private let completion: (UInt64, SequencedPhotoResult) -> Void
    private var keepAlive: SequencedPhotoCaptureDelegate?
    private var didDeliverResult = false

    init(
        sequence: UInt64,
        completion: @escaping (UInt64, SequencedPhotoResult) -> Void
    ) {
        self.sequence = sequence
        self.completion = completion
    }

    func retainUntilCaptureFinishes() {
        keepAlive = self
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishProcessingPhoto photo: AVCapturePhoto,
        error: Error?
    ) {
        guard !didDeliverResult else { return }
        didDeliverResult = true

        if let error {
            completion(sequence, .failure("Couldn't capture the photo: \(error.localizedDescription)"))
            return
        }

        // Preserve the ORIGINAL encoded file (HEIC on modern devices, else JPEG) WITH
        // EXIF/GPS. Auto-deferred delivery is disabled, so this can never be a proxy.
        guard let data = photo.fileDataRepresentation() else {
            completion(sequence, .failure("The camera returned no photo data. Please try again."))
            return
        }
        completion(sequence, .success(data))
    }

    func photoOutput(
        _ output: AVCapturePhotoOutput,
        didFinishCaptureFor resolvedSettings: AVCaptureResolvedPhotoSettings,
        error: Error?
    ) {
        if !didDeliverResult {
            didDeliverResult = true
            let detail = error?.localizedDescription ?? "The camera did not finish processing the photo."
            completion(sequence, .failure("Couldn't capture the photo: \(detail)"))
        }
        keepAlive = nil
    }
}

// MARK: - Session controller (AVFoundation, device-only)

/// Owns the `AVCaptureSession`, the `AVCapturePhotoOutput`, and the capture delegate.
/// Publishes just enough for the SwiftUI view: authorization, current facing, capture
/// count, and a surfaced error. Live capture only runs on a real device — the simulator
/// has no camera, so setup simply finds no device and reports it (never hangs).
@MainActor
final class CameraSessionController: NSObject, ObservableObject {
    enum Authorization {
        case unknown
        case authorized
        case denied
    }

    let session = AVCaptureSession()

    @Published private(set) var authorization: Authorization = .unknown
    @Published private(set) var facing: CameraFacing = .back
    @Published var flashOn: Bool = false
    /// How many photos have been captured this camera session. Drives the on-screen
    /// capture feedback (shutter flash + stamped counter) so each shot is unmistakable.
    @Published private(set) var capturedCount: Int = 0
    /// Surfaced to an in-view alert on any failure, so nothing is silently dropped.
    @Published var captureError: String?

    private let photoOutput = AVCapturePhotoOutput()
    private var currentInput: AVCaptureDeviceInput?
    private let sessionQueue = DispatchQueue(label: "com.gregstoltz.btqfieldcapture.camera.session")
    private var isConfigured = false
    private var nextCaptureSequence: UInt64 = 0
    private var nextPhotoDeliverySequence: UInt64 = 0
    private var pendingPhotoResults: [UInt64: SequencedPhotoResult] = [:]
    private var isDeliveringPhotos = false
    /// Keeps both the live preview and encoded photos upright as the device rotates.
    let rotation = CaptureRotation()

    /// Delivered per successful capture — the ORIGINAL encoded bytes. Async so the
    /// call site can save + append to the draft before the next shot.
    var onPhoto: ((Data) async -> Void)?

    // MARK: Authorization + lifecycle

    func requestAccessAndStart() {
        switch AVCaptureDevice.authorizationStatus(for: .video) {
        case .authorized:
            authorization = .authorized
            configureAndStart()
        case .notDetermined:
            AVCaptureDevice.requestAccess(for: .video) { [weak self] granted in
                Task { @MainActor in
                    guard let self else { return }
                    if granted {
                        self.authorization = .authorized
                        self.configureAndStart()
                    } else {
                        self.authorization = .denied
                    }
                }
            }
        case .denied, .restricted:
            authorization = .denied
        @unknown default:
            authorization = .denied
        }
    }

    func stop() {
        sessionQueue.async { [session] in
            if session.isRunning { session.stopRunning() }
        }
    }

    /// Does the active device have a usable flash? Drives the flash toggle + mode.
    var activeDeviceHasFlash: Bool {
        currentInput?.device.hasFlash ?? false
    }

    func toggleFacing() {
        let next = facing.toggled
        sessionQueue.async { [weak self] in
            self?.applyInput(for: next)
        }
    }

    // MARK: Configuration

    private func configureAndStart() {
        sessionQueue.async { [weak self] in
            guard let self else { return }
            if !self.isConfigured {
                self.session.beginConfiguration()
                self.session.sessionPreset = .photo
                self.applyInput(for: .back, insideExistingConfiguration: true)
                if self.session.canAddOutput(self.photoOutput) {
                    self.session.addOutput(self.photoOutput)
                    self.photoOutput.maxPhotoQualityPrioritization = .balanced
                    if self.photoOutput.isZeroShutterLagSupported {
                        self.photoOutput.isZeroShutterLagEnabled = true
                        if self.photoOutput.isResponsiveCaptureSupported {
                            self.photoOutput.isResponsiveCaptureEnabled = true
                        }
                    }
                    if self.photoOutput.isFastCapturePrioritizationSupported {
                        self.photoOutput.isFastCapturePrioritizationEnabled = true
                    }
                    if self.photoOutput.isAutoDeferredPhotoDeliverySupported {
                        self.photoOutput.isAutoDeferredPhotoDeliveryEnabled = false
                    }
                }
                self.session.commitConfiguration()
                self.isConfigured = true
            }
            if self.currentInput == nil {
                Task { @MainActor in
                    self.captureError = "No camera is available on this device."
                }
                return
            }
            if !self.session.isRunning { self.session.startRunning() }
        }
    }

    /// Swap the video input to the wide camera for `target`. Runs on the session queue.
    private func applyInput(for target: CameraFacing, insideExistingConfiguration: Bool = false) {
        guard let device = Self.wideCamera(for: target.avPosition),
              let input = try? AVCaptureDeviceInput(device: device) else {
            Task { @MainActor in
                self.captureError = "The \(target == .back ? "back" : "front") camera is unavailable."
            }
            return
        }
        if !insideExistingConfiguration { session.beginConfiguration() }
        if let existing = currentInput { session.removeInput(existing) }
        if session.canAddInput(input) {
            session.addInput(input)
            currentInput = input
            Task { @MainActor in
                self.facing = target
                self.rotation.setDevice(device)
            }
        } else if let existing = currentInput {
            session.addInput(existing) // never leave the session with no input
        }
        if !insideExistingConfiguration { session.commitConfiguration() }
    }

    private static func wideCamera(for position: AVCaptureDevice.Position) -> AVCaptureDevice? {
        AVCaptureDevice.default(.builtInWideAngleCamera, for: .video, position: position)
            ?? AVCaptureDevice.default(for: .video)
    }

    // MARK: Capture

    func capturePhoto() {
        guard authorization == .authorized, currentInput != nil else {
            captureError = "The camera is not ready."
            return
        }
        let flashMode = CameraCaptureSettingsFactory.flashMode(
            flashOn: flashOn,
            deviceHasFlash: activeDeviceHasFlash
        )
        let rotationAngle = rotation.captureAngle
        let captureSequence = nextCaptureSequence
        nextCaptureSequence += 1
        sessionQueue.async { [weak self] in
            guard let self else { return }
            applyCaptureRotation(rotationAngle, to: self.photoOutput)
            let settings = AVCapturePhotoSettings()
            settings.photoQualityPrioritization = CameraCaptureSettingsFactory.qualityPrioritization
            if self.photoOutput.supportedFlashModes.contains(flashMode) {
                settings.flashMode = flashMode
            }
            let delegate = SequencedPhotoCaptureDelegate(sequence: captureSequence) { [weak self] sequence, result in
                Task { @MainActor [weak self] in
                    self?.enqueuePhotoResult(result, sequence: sequence)
                }
            }
            delegate.retainUntilCaptureFinishes()
            self.photoOutput.capturePhoto(with: settings, delegate: delegate)
        }
    }

    private func enqueuePhotoResult(_ result: SequencedPhotoResult, sequence: UInt64) {
        pendingPhotoResults[sequence] = result
        deliverReadyPhotosInOrder()
    }

    /// A single delivery task drains contiguous results in shutter order. It remains the
    /// sole caller of `onPhoto` even while that async append is suspended.
    private func deliverReadyPhotosInOrder() {
        guard !isDeliveringPhotos else { return }
        isDeliveringPhotos = true

        Task { @MainActor in
            while let result = pendingPhotoResults.removeValue(forKey: nextPhotoDeliverySequence) {
                nextPhotoDeliverySequence += 1
                switch result {
                case .success(let data):
                    capturedCount += 1
                    await onPhoto?(data)
                case .failure(let message):
                    captureError = message
                }
            }
            isDeliveringPhotos = false

            // A result may have arrived while the delivery task was suspended in
            // `onPhoto`; start another drain if it is now the next contiguous shot.
            if pendingPhotoResults[nextPhotoDeliverySequence] != nil {
                deliverReadyPhotosInOrder()
            }
        }
    }
}

// MARK: - Preview layer bridge

/// Hosts an `AVCaptureVideoPreviewLayer` filling the sheet.
private struct CameraPreview: UIViewRepresentable {
    let session: AVCaptureSession
    let rotation: CaptureRotation

    func makeUIView(context: Context) -> PreviewView {
        let view = PreviewView()
        view.videoPreviewLayer.session = session
        view.videoPreviewLayer.videoGravity = .resizeAspectFill
        rotation.setPreviewLayer(view.videoPreviewLayer)
        return view
    }

    func updateUIView(_ uiView: PreviewView, context: Context) {}

    final class PreviewView: UIView {
        override class var layerClass: AnyClass { AVCaptureVideoPreviewLayer.self }
        var videoPreviewLayer: AVCaptureVideoPreviewLayer { layer as! AVCaptureVideoPreviewLayer }
    }
}

// MARK: - The camera sheet

/// Multi-shot, original-quality field capture. Same public shape as before —
/// `CameraCaptureView(onPhoto:)` shown in a `.sheet`, calling `onPhoto` once per photo —
/// but the preview now STAYS UP so you can rattle off several shots, each confirmed by a
/// stamped running count and a haptic tap. Tap Done to finish.
public struct CameraCaptureView: View {
    public var onPhoto: (Data) async -> Void
    @Environment(\.dismiss) private var dismiss
    @StateObject private var controller = CameraSessionController()

    // Capture-feedback animation state (see `.onChange(of: capturedCount)`).
    @State private var countPopScale: CGFloat = 1
    @State private var countPopOpacity: Double = 0

    public static var isCameraAvailable: Bool {
        UIImagePickerController.isSourceTypeAvailable(.camera)
    }

    public init(onPhoto: @escaping (Data) async -> Void) {
        self.onPhoto = onPhoto
    }

    public var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            switch controller.authorization {
            case .authorized:
                cameraUI
            case .denied:
                deniedUI
            case .unknown:
                ProgressView().tint(.white)
            }
        }
        .onAppear {
            controller.onPhoto = onPhoto
            controller.requestAccessAndStart()
        }
        .onDisappear { controller.stop() }
        .alert(
            "Capture problem",
            isPresented: Binding(
                get: { controller.captureError != nil },
                set: { if !$0 { controller.captureError = nil } }
            )
        ) {
            Button("OK", role: .cancel) { controller.captureError = nil }
        } message: {
            Text(controller.captureError ?? "")
        }
    }

    private var cameraUI: some View {
        ZStack {
            CameraPreview(session: controller.session, rotation: controller.rotation)
                .ignoresSafeArea()

            // The running count, stamped large on each shot then fading — the
            // unmistakable "yes, it captured" cue.
            Text("\(controller.capturedCount)")
                .font(.system(size: 130, weight: .bold, design: .rounded))
                .foregroundStyle(.white)
                .shadow(color: .black.opacity(0.4), radius: 12)
                .scaleEffect(countPopScale)
                .opacity(countPopOpacity)
                .allowsHitTesting(false)

            VStack {
                topControls
                Spacer()
                if controller.capturedCount > 0 { countPill }
                shutterRow
            }
        }
        .onChange(of: controller.capturedCount) { _, newValue in
            guard newValue > 0 else { return }
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            // Number stamp: appear big, then shrink + fade.
            countPopScale = 1.5
            countPopOpacity = 1
            withAnimation(.easeOut(duration: 0.55)) {
                countPopScale = 0.85
                countPopOpacity = 0
            }
        }
    }

    /// A persistent running-total pill so the user always knows how many they've taken.
    private var countPill: some View {
        Label(
            "\(controller.capturedCount) photo\(controller.capturedCount == 1 ? "" : "s")",
            systemImage: "photo.stack.fill"
        )
        .font(.subheadline.weight(.semibold))
        .foregroundStyle(.white)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
        .background(Capsule().fill(.ultraThinMaterial))
        .padding(.bottom, 12)
        .accessibilityLabel("\(controller.capturedCount) photos taken")
    }

    private var topControls: some View {
        HStack {
            Spacer()

            if controller.activeDeviceHasFlash {
                Button {
                    controller.flashOn.toggle()
                } label: {
                    Image(systemName: controller.flashOn ? "bolt.fill" : "bolt.slash.fill")
                        .font(.title3)
                        .foregroundStyle(.white)
                }
                .accessibilityLabel(controller.flashOn ? "Flash on" : "Flash off")
            }
        }
        .frame(minHeight: 28)
        .padding(.horizontal, 22)
        .padding(.top, 12)
    }

    /// Bottom control bar: Done sits beside the shutter so it's reachable with the same
    /// thumb that fires the camera — no hand-shuffle to finish. Done left, shutter center,
    /// camera-flip right, all balanced so the shutter stays centered.
    private var shutterRow: some View {
        HStack {
            Button("Done") { dismiss() }
                .font(.headline.weight(.semibold))
                .foregroundStyle(.white)
                .frame(minWidth: 66, minHeight: 44, alignment: .leading)
                .accessibilityLabel("Done taking photos")

            Spacer()

            Button {
                controller.capturePhoto()
            } label: {
                ZStack {
                    Circle().stroke(Color.white, lineWidth: 4).frame(width: 74, height: 74)
                    Circle().fill(Color.white).frame(width: 60, height: 60)
                }
            }
            .accessibilityLabel("Take photo")

            Spacer()

            Button {
                controller.toggleFacing()
            } label: {
                Image(systemName: "arrow.triangle.2.circlepath.camera")
                    .font(.title2)
                    .foregroundStyle(.white)
                    .padding(14)
                    .background(Circle().fill(Color.white.opacity(0.15)))
            }
            .frame(minWidth: 66, alignment: .trailing)
            .accessibilityLabel("Switch camera")
        }
        .padding(.horizontal, 24)
        .padding(.bottom, 34)
    }

    private var deniedUI: some View {
        VStack(spacing: 18) {
            Image(systemName: "camera.fill")
                .font(.system(size: 44))
                .foregroundStyle(.white.opacity(0.8))
            Text("Camera access is off")
                .font(.title3.weight(.semibold))
                .foregroundStyle(.white)
            Text("Turn on camera access for BTQ Field Capture in Settings to take photos.")
                .font(.subheadline)
                .foregroundStyle(.white.opacity(0.8))
                .multilineTextAlignment(.center)
            if let url = URL(string: UIApplication.openSettingsURLString) {
                Button("Open Settings") { UIApplication.shared.open(url) }
                    .buttonStyle(.borderedProminent)
            }
            Button("Done") { dismiss() }
                .foregroundStyle(.white)
                .padding(.top, 4)
        }
        .padding(32)
    }
}
#endif

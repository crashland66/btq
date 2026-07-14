import AVFoundation
import SwiftUI

#if os(iOS)
import UIKit

/// Keeps a capture session's video oriented to the physical device, via
/// `AVCaptureDevice.RotationCoordinator`. The coordinator is bound to one device
/// and one preview layer, so it is rebuilt whenever either changes.
@MainActor
final class CaptureRotation {
    private(set) var device: AVCaptureDevice?
    private weak var previewLayer: AVCaptureVideoPreviewLayer?
    private var coordinator: AVCaptureDevice.RotationCoordinator?
    private var previewAngleObservation: NSKeyValueObservation?

    /// The angle to stamp on the output connection when a photo is captured.
    var captureAngle: CGFloat? {
        coordinator?.videoRotationAngleForHorizonLevelCapture
    }

    func setDevice(_ device: AVCaptureDevice?) {
        self.device = device
        rebuild()
    }

    func setPreviewLayer(_ layer: AVCaptureVideoPreviewLayer) {
        previewLayer = layer
        rebuild()
    }

    private func rebuild() {
        previewAngleObservation = nil
        coordinator = nil
        guard let device else { return }
        let coordinator = AVCaptureDevice.RotationCoordinator(
            device: device,
            previewLayer: previewLayer
        )
        self.coordinator = coordinator
        previewAngleObservation = coordinator.observe(
            \.videoRotationAngleForHorizonLevelPreview,
            options: [.initial, .new]
        ) { coordinator, _ in
            let angle = coordinator.videoRotationAngleForHorizonLevelPreview
            Task { @MainActor [weak self] in
                self?.applyPreviewAngle(angle)
            }
        }
    }

    private func applyPreviewAngle(_ angle: CGFloat) {
        guard let connection = previewLayer?.connection,
              connection.isVideoRotationAngleSupported(angle) else { return }
        connection.videoRotationAngle = angle
    }
}

/// Applies the current physical-device angle to an output immediately before a
/// capture so the encoded media is upright even when the interface stays locked.
func applyCaptureRotation(_ angle: CGFloat?, to output: AVCaptureOutput) {
    guard let angle,
          let connection = output.connection(with: .video),
          connection.isVideoRotationAngleSupported(angle) else { return }
    connection.videoRotationAngle = angle
}
#endif

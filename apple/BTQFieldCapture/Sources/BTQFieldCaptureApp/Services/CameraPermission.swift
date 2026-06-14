import AVFoundation
import Foundation

public enum CameraCapturePermissionStatus: Equatable, Sendable {
    case notDetermined
    case granted
    case denied
    case restricted
    case unknown

    public var allowsCapture: Bool {
        self == .granted
    }
}

public enum CameraCaptureStartDecision: Equatable, Sendable {
    case requestPermission
    case presentCamera
    case showMessage(String)
}

public enum CameraCapturePermissionGate {
    public static let unavailableMessage = "Camera is not available on this device. Use Photos instead."
    public static let deniedMessage = "Camera access is required to take photos."
    public static let restrictedMessage = "Camera access is restricted on this device."

    public static func decision(
        status: CameraCapturePermissionStatus,
        cameraAvailable: Bool
    ) -> CameraCaptureStartDecision {
        guard cameraAvailable else {
            return .showMessage(unavailableMessage)
        }
        switch status {
        case .notDetermined:
            return .requestPermission
        case .granted:
            return .presentCamera
        case .denied:
            return .showMessage(deniedMessage)
        case .restricted:
            return .showMessage(restrictedMessage)
        case .unknown:
            return .showMessage(deniedMessage)
        }
    }
}

public protocol CameraCapturePermissionChecking: Sendable {
    func authorizationStatus() async -> CameraCapturePermissionStatus
    func requestAuthorization() async -> CameraCapturePermissionStatus
}

public struct SystemCameraCapturePermissionChecker: CameraCapturePermissionChecking {
    public init() {}

    public func authorizationStatus() async -> CameraCapturePermissionStatus {
        CameraCapturePermissionStatus(AVCaptureDevice.authorizationStatus(for: .video))
    }

    public func requestAuthorization() async -> CameraCapturePermissionStatus {
        let status = await authorizationStatus()
        switch status {
        case .granted, .denied, .restricted:
            return status
        case .notDetermined, .unknown:
            let granted = await AVCaptureDevice.requestAccess(for: .video)
            return granted ? .granted : .denied
        }
    }
}

private extension CameraCapturePermissionStatus {
    init(_ status: AVAuthorizationStatus) {
        switch status {
        case .notDetermined:
            self = .notDetermined
        case .restricted:
            self = .restricted
        case .denied:
            self = .denied
        case .authorized:
            self = .granted
        @unknown default:
            self = .unknown
        }
    }
}

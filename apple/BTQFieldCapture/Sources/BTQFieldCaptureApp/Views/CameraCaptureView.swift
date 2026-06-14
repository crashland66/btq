#if os(iOS) && canImport(UIKit)
import SwiftUI
import UIKit

public struct CameraCaptureView: UIViewControllerRepresentable {
    public var onPhoto: (Data) -> Void
    @Environment(\.dismiss) private var dismiss

    public static var isCameraAvailable: Bool {
        UIImagePickerController.isSourceTypeAvailable(.camera)
    }

    public init(onPhoto: @escaping (Data) -> Void) {
        self.onPhoto = onPhoto
    }

    public func makeCoordinator() -> Coordinator {
        Coordinator(onPhoto: onPhoto, dismiss: dismiss)
    }

    public func makeUIViewController(context: Context) -> UIImagePickerController {
        let controller = UIImagePickerController()
        controller.sourceType = .camera
        controller.mediaTypes = ["public.image"]
        controller.delegate = context.coordinator
        return controller
    }

    public func updateUIViewController(_ uiViewController: UIImagePickerController, context: Context) {}

    public final class Coordinator: NSObject, UINavigationControllerDelegate, UIImagePickerControllerDelegate {
        private let onPhoto: (Data) -> Void
        private let dismiss: DismissAction

        init(onPhoto: @escaping (Data) -> Void, dismiss: DismissAction) {
            self.onPhoto = onPhoto
            self.dismiss = dismiss
        }

        public func imagePickerController(_ picker: UIImagePickerController, didFinishPickingMediaWithInfo info: [UIImagePickerController.InfoKey: Any]) {
            if let image = info[.originalImage] as? UIImage,
               let data = image.jpegData(compressionQuality: 0.86) {
                onPhoto(data)
            }
            dismiss()
        }

        public func imagePickerControllerDidCancel(_ picker: UIImagePickerController) {
            dismiss()
        }
    }
}
#endif

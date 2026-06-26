import SwiftUI
import ImageIO
#if os(iOS)
import UIKit
#elseif os(macOS)
import AppKit
#endif

struct CapturePhotoThumbnail: View {
    let photo: CapturePhoto
    var size: CGFloat = 64

    var body: some View {
        Group {
            if let image = thumbnailImage {
                image
                    .resizable()
                    .scaledToFill()
            } else {
                Image(systemName: "photo")
                    .font(.title2)
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(width: size, height: size)
        .background(Color.secondary.opacity(0.10))
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .overlay(
            RoundedRectangle(cornerRadius: 8)
                .stroke(Color.secondary.opacity(0.25), lineWidth: 1)
        )
        .accessibilityLabel("Photo thumbnail")
    }

    private var thumbnailImage: Image? {
        guard let fileURL = photo.fileURL,
              let source = CGImageSourceCreateWithURL(
                fileURL as CFURL,
                [
                    kCGImageSourceShouldCache: false,
                ] as CFDictionary
              ) else {
            return nil
        }
        let maxPixelSize = max(128, Int(size * 3))
        guard let thumbnail = CGImageSourceCreateThumbnailAtIndex(
            source,
            0,
            [
                kCGImageSourceCreateThumbnailFromImageAlways: true,
                kCGImageSourceCreateThumbnailWithTransform: true,
                kCGImageSourceThumbnailMaxPixelSize: maxPixelSize,
                kCGImageSourceShouldCacheImmediately: true,
            ] as CFDictionary
        ) else {
            return nil
        }

        #if os(iOS)
        return Image(uiImage: UIImage(cgImage: thumbnail))
        #elseif os(macOS)
        return Image(nsImage: NSImage(cgImage: thumbnail, size: NSSize(width: size, height: size)))
        #else
        return nil
        #endif
    }
}

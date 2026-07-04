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
    var remoteBaseURL: URL?
    var authorizationToken: (() async -> String?)?
    @State private var remoteImage: Image?

    var body: some View {
        Group {
            if let image = thumbnailImage {
                image
                    .resizable()
                    .scaledToFill()
            } else if let remoteImage {
                remoteImage
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
        .task(id: remoteImageTaskID) {
            await loadRemoteImageIfNeeded()
        }
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

    private var remoteImageTaskID: String {
        "\(photo.remoteURL ?? "")-\(remoteBaseURL?.absoluteString ?? "")-\(Int(size))"
    }

    private func loadRemoteImageIfNeeded() async {
        guard thumbnailImage == nil, remoteImage == nil, let remoteURL else { return }
        var request = URLRequest(url: remoteURL)
        request.setValue("image/*", forHTTPHeaderField: "Accept")
        if let token = await authorizationToken?(), !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode),
              let image = thumbnailImage(from: data)
        else {
            return
        }
        remoteImage = image
    }

    private var remoteURL: URL? {
        guard let raw = photo.remoteURL?.trimmingCharacters(in: .whitespacesAndNewlines), !raw.isEmpty else {
            return nil
        }
        if let absoluteURL = URL(string: raw), absoluteURL.scheme != nil {
            return absoluteURL
        }
        guard let remoteBaseURL else { return nil }
        return URL(string: raw, relativeTo: remoteBaseURL)?.absoluteURL
    }

    private func thumbnailImage(from data: Data) -> Image? {
        guard let source = CGImageSourceCreateWithData(
            data as CFData,
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

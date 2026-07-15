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
    #if os(iOS)
    @State private var selectedPhoto: CapturePhoto?
    #endif

    @ViewBuilder
    var body: some View {
        #if os(iOS)
        Button {
            selectedPhoto = photo
        } label: {
            thumbnail
                .overlay(alignment: .bottomTrailing) {
                    Image(systemName: "arrow.up.left.and.arrow.down.right")
                        .font(.caption2.weight(.bold))
                        .foregroundStyle(.white)
                        .padding(3)
                        .background(.black.opacity(0.45), in: Circle())
                        .padding(3)
                        .accessibilityHidden(true)
                }
        }
        .buttonStyle(.plain)
        .accessibilityLabel("View full photo")
        .fullScreenCover(item: $selectedPhoto) { selectedPhoto in
            CapturePhotoLightbox(
                photo: selectedPhoto,
                remoteURL: resolvedRemoteURL(for: selectedPhoto),
                authorizationToken: authorizationToken
            )
        }
        #else
        thumbnail
            .accessibilityLabel("Photo thumbnail")
        #endif
    }

    private var thumbnail: some View {
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
        resolvedRemoteURL(for: photo)
    }

    private func resolvedRemoteURL(for photo: CapturePhoto) -> URL? {
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

#if os(iOS)
/// Full-resolution viewer shared by draft, queued, and submitted photo thumbnails.
/// Local photos open from app-owned storage; remote photos reuse the thumbnail's
/// authenticated media request contract.
private struct CapturePhotoLightbox: View {
    let photo: CapturePhoto
    let remoteURL: URL?
    let authorizationToken: (() async -> String?)?

    @Environment(\.dismiss) private var dismiss
    @State private var image: UIImage?
    @State private var scale: CGFloat = 1
    @State private var isLoading = true

    var body: some View {
        ZStack {
            Color.black.ignoresSafeArea()

            if let image {
                Image(uiImage: image)
                    .resizable()
                    .scaledToFit()
                    .scaleEffect(scale)
                    .gesture(
                        MagnificationGesture()
                            .onChanged { scale = max(1, $0) }
                            .onEnded { _ in
                                withAnimation(.spring(response: 0.3)) {
                                    scale = min(max(scale, 1), 4)
                                }
                            }
                    )
                    .accessibilityLabel("Full photo")
            } else if isLoading {
                ProgressView()
                    .tint(.white)
                    .accessibilityLabel("Loading full photo")
            } else {
                ContentUnavailableView(
                    "Photo unavailable",
                    systemImage: "photo",
                    description: Text(photo.filename)
                )
                .foregroundStyle(.white)
            }

            VStack {
                HStack {
                    Spacer()
                    Button { dismiss() } label: {
                        Image(systemName: "xmark.circle.fill")
                            .font(.title)
                            .foregroundStyle(.white.opacity(0.85))
                            .padding()
                    }
                    .accessibilityLabel("Close full photo")
                }
                Spacer()
            }
        }
        .contentShape(Rectangle())
        .onTapGesture {
            if scale <= 1.01 {
                dismiss()
            } else {
                withAnimation(.spring(response: 0.3)) { scale = 1 }
            }
        }
        .task(id: photo.id) {
            await loadImage()
        }
    }

    private func loadImage() async {
        isLoading = true
        defer { isLoading = false }

        if let fileURL = photo.fileURL,
           let localImage = UIImage(contentsOfFile: fileURL.path) {
            image = localImage
            return
        }

        guard let remoteURL else { return }
        var request = URLRequest(url: remoteURL)
        request.setValue("image/*", forHTTPHeaderField: "Accept")
        if let token = await authorizationToken?(), !token.isEmpty {
            request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              let http = response as? HTTPURLResponse,
              (200..<300).contains(http.statusCode)
        else {
            return
        }
        image = UIImage(data: data)
    }
}
#endif

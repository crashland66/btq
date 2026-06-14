import CoreGraphics
import Foundation
import ImageIO
import UniformTypeIdentifiers

public enum ImageNormalizerError: Error, Equatable {
    case decodeFailed
    case encodeFailed
}

public enum ImageUploadFormat: Equatable, Sendable {
    case jpeg

    public var fileExtension: String {
        switch self {
        case .jpeg: "jpg"
        }
    }

    public var mimeType: String {
        switch self {
        case .jpeg: "image/jpeg"
        }
    }

    var uniformTypeIdentifier: String {
        switch self {
        case .jpeg: UTType.jpeg.identifier
        }
    }
}

public struct ImageUploadPolicy: Equatable, Sendable {
    public static let fieldCapture = ImageUploadPolicy(format: .jpeg, quality: 0.86, maxPixelDimension: 2_048)

    public var format: ImageUploadFormat
    public var quality: Double
    public var maxPixelDimension: Double

    public init(format: ImageUploadFormat, quality: Double, maxPixelDimension: Double) {
        self.format = format
        self.quality = quality
        self.maxPixelDimension = maxPixelDimension
    }
}

public enum ImageNormalizer {
    public static func normalizedData(from fileURL: URL, policy: ImageUploadPolicy = .fieldCapture) throws -> Data {
        switch policy.format {
        case .jpeg:
            try jpegData(from: fileURL, quality: policy.quality, maxPixelDimension: CGFloat(policy.maxPixelDimension))
        }
    }

    public static func normalizedData(from data: Data, policy: ImageUploadPolicy = .fieldCapture) throws -> Data {
        switch policy.format {
        case .jpeg:
            try jpegData(from: data, quality: policy.quality, maxPixelDimension: CGFloat(policy.maxPixelDimension))
        }
    }

    public static func jpegData(from fileURL: URL, quality: Double = 0.86, maxPixelDimension: CGFloat = 2_048) throws -> Data {
        guard let source = CGImageSourceCreateWithURL(fileURL as CFURL, nil) else {
            throw ImageNormalizerError.decodeFailed
        }

        return try jpegData(from: source, quality: quality, maxPixelDimension: maxPixelDimension)
    }

    public static func jpegData(from data: Data, quality: Double = 0.86, maxPixelDimension: CGFloat = 2_048) throws -> Data {
        guard let source = CGImageSourceCreateWithData(data as CFData, nil) else {
            throw ImageNormalizerError.decodeFailed
        }

        return try jpegData(from: source, quality: quality, maxPixelDimension: maxPixelDimension)
    }

    private static func jpegData(from source: CGImageSource, quality: Double, maxPixelDimension: CGFloat) throws -> Data {
        guard let image = CGImageSourceCreateThumbnailAtIndex(
                source,
                0,
                [
                    kCGImageSourceCreateThumbnailFromImageAlways: true,
                    kCGImageSourceThumbnailMaxPixelSize: maxPixelDimension,
                    kCGImageSourceCreateThumbnailWithTransform: true,
                ] as CFDictionary
              ) else {
            throw ImageNormalizerError.decodeFailed
        }

        let output = NSMutableData()
        guard let destination = CGImageDestinationCreateWithData(output, ImageUploadFormat.jpeg.uniformTypeIdentifier as CFString, 1, nil) else {
            throw ImageNormalizerError.encodeFailed
        }
        CGImageDestinationAddImage(
            destination,
            image,
            [
                kCGImageDestinationLossyCompressionQuality: quality,
            ] as CFDictionary
        )
        guard CGImageDestinationFinalize(destination) else {
            throw ImageNormalizerError.encodeFailed
        }
        return output as Data
    }
}

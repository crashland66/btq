// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "BTQFieldCapture",
    platforms: [
        .iOS(.v18),
        .macOS(.v15),
    ],
    products: [
        .library(
            name: "BTQFieldCaptureApp",
            targets: ["BTQFieldCaptureApp"]
        ),
        .executable(
            name: "BTQFieldCaptureMac",
            targets: ["BTQFieldCaptureMac"]
        ),
        .executable(
            name: "BTQFieldCaptureAPISmoke",
            targets: ["BTQFieldCaptureAPISmoke"]
        ),
        .executable(
            name: "BTQFieldCaptureLiveAPIVerifier",
            targets: ["BTQFieldCaptureLiveAPIVerifier"]
        ),
    ],
    targets: [
        .target(
            name: "BTQFieldCaptureApp",
            linkerSettings: [
                .linkedLibrary("sqlite3"),
            ]
        ),
        .executableTarget(
            name: "BTQFieldCaptureMac",
            dependencies: ["BTQFieldCaptureApp"]
        ),
        .executableTarget(
            name: "BTQFieldCaptureAPISmoke",
            dependencies: ["BTQFieldCaptureApp"]
        ),
        .executableTarget(
            name: "BTQFieldCaptureLiveAPIVerifier",
            dependencies: ["BTQFieldCaptureApp"]
        ),
        .testTarget(
            name: "BTQFieldCaptureAppTests",
            dependencies: ["BTQFieldCaptureApp"]
        ),
    ]
)

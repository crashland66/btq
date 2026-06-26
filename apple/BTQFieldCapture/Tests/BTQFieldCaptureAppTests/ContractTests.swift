import CoreGraphics
import Foundation
import ImageIO
import Testing
import UniformTypeIdentifiers
@testable import BTQFieldCaptureApp

@Test func sessionDecodesBackendShape() throws {
    let json = """
    {
      "person": {"person_id": "employee_1", "name": "Field Person"},
      "token": {"token_id": "token_1", "label": "Pilot", "role": "site_admin", "token_type": "capture"},
      "sites": [
        {
          "site_id": "site_1",
          "label": "Site One",
          "capture_guidance": "Look around.",
          "display_categories": [{"value": "supplies", "label": "Supplies"}]
        }
      ],
      "can_submit": true,
      "can_review": false,
      "max_images": 6,
      "inbox_count": 0
    }
    """.data(using: .utf8)!

    let session = try JSONDecoder().decode(BTQSession.self, from: json)

    #expect(session.person.personID == "employee_1")
    #expect(session.token.role == "site_admin")
    #expect(session.token.tokenType == "capture")
    #expect(session.sites.first?.displayCategories.first?.value == "supplies")
    #expect(session.maxImages == 6)
}

@Test func sessionDecodesLiveDisplayCategoryVariants() throws {
    let json = """
    {
      "person": {"person_id": "employee_1", "name": "Field Person"},
      "token": {"token_id": "token_1", "label": "Pilot"},
      "sites": [
        {
          "site_id": "site_1",
          "label": "Site One",
          "display_categories": [
            {"canonical": "general_note", "label": "General note"},
            {"label": "Supply request"},
            "incident"
          ]
        }
      ],
      "can_submit": true,
      "can_review": false,
      "max_images": 6,
      "inbox_count": 0
    }
    """.data(using: .utf8)!

    let session = try JSONDecoder().decode(BTQSession.self, from: json)
    let categories = try #require(session.sites.first?.displayCategories)

    #expect(categories.map(\.value) == ["general_note", "Supply request", "incident"])
    #expect(categories.map(\.label) == ["General note", "Supply request", "incident"])
}

@Test func sessionDecodesCanonicalCategoryAsSubmissionValue() throws {
    let json = """
    {
      "person": {"person_id": "operator_1", "name": "Operator"},
      "token": {"token_id": "token_operator", "label": "Operator"},
      "sites": [
        {
          "site_id": "site_qc",
          "label": "QC Site",
          "display_categories": [
            {"value": "legacy_qc", "canonical": "qc", "label": "QC"}
          ]
        }
      ],
      "can_submit": true,
      "can_review": true,
      "max_images": 100,
      "inbox_count": 0
    }
    """.data(using: .utf8)!

    let session = try JSONDecoder().decode(BTQSession.self, from: json)
    let category = try #require(session.sites.first?.displayCategories.first)

    #expect(category.value == "qc")
    #expect(category.label == "QC")
    #expect(session.maxImages == 100)
}

@Test func mySubmissionsDecodesBackendShape() throws {
    let json = """
    {
      "submissions": [
        {
          "capture_id": "cap_1",
          "site_id": "site_1",
          "site_name": "Site One",
          "target_type": "location",
          "target_id": "site_1",
          "captured_at": "2026-06-14T10:15:00Z",
          "photo_count": 1,
          "has_audio": true,
          "has_text_note": true,
          "note_text": "Lobby needs towels",
          "photo_urls": ["/media/photo_1"],
          "track": "B",
          "stage": "reviewed",
          "retargetable": false,
          "outcome_label": "No action needed",
          "per_photo_quality": [
            {
              "severity": "degraded",
              "flags": ["too_dark"],
              "description": "Dark hallway",
              "possible_issues": ["lights off"]
            }
          ]
        }
      ],
      "quality_summary": {
        "total_processed": 5,
        "clear": 4,
        "flag_counts": {"too_dark": 1}
      }
    }
    """.data(using: .utf8)!

    let response = try JSONDecoder().decode(MySubmissionsResponse.self, from: json)

    #expect(response.submissions.first?.captureID == "cap_1")
    #expect(response.submissions.first?.siteName == "Site One")
    #expect(response.submissions.first?.hasAudio == true)
    #expect(response.submissions.first?.perPhotoQuality.first?.possibleIssues == ["lights off"])
    #expect(response.qualitySummary?.flagCounts["too_dark"] == 1)
}

@Test func inboxDecodesBackendShape() throws {
    let json = """
    {
      "count": 2,
      "items": [
        {
          "draft_id": "jd_1",
          "_rev": "1-abc",
          "source_capture_id": "cap_1",
          "source": "voice",
          "message": "Supply need: paper towels out.",
          "evidence": "Operator said east restroom is out.",
          "site": "Sandbox Site",
          "site_id": "SANDBOX",
          "group_id": "grp_1",
          "submitter_name": "Gregory Stoltz",
          "created_at": "Today",
          "job_type": "log_supply_need",
          "payload": {"site_id": "SANDBOX", "item_name": "Paper towels", "requested_by": "operator"}
        }
      ]
    }
    """.data(using: .utf8)!

    let response = try JSONDecoder().decode(InboxResponse.self, from: json)

    #expect(response.count == 2)
    #expect(response.items.first?.draftID == "jd_1")
    #expect(response.items.first?.revision == "1-abc")
    #expect(response.items.first?.payload["item_name"]?.description == "Paper towels")
}

@Test func onboardingParserFindsQueryAndFragmentTokens() {
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/?token=abc")!) == "abc")
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/#token=def")!) == "def")
    #expect(OnboardingLinkParser.token(from: URL(string: "btq://onboard#ghi")!) == "ghi")
}

@Test func onboardingParserFindsUniversalLinkPathTokens() {
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/onboard/path-token")!) == "path-token")
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/onboard/fct_%20encoded")!) == "fct_ encoded")
    #expect(OnboardingLinkParser.token(from: URL(string: "btq://onboard/custom-scheme-token")!) == "custom-scheme-token")
    #expect(OnboardingLinkParser.token(from: URL(string: "btq-field-capture://onboard/app-scheme-token")!) == "app-scheme-token")
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/app.js")!) == nil)
    #expect(OnboardingLinkParser.token(from: URL(string: "https://fc.gregstoltz.com/onboard/")!) == nil)
}

@Test func appMetadataDeclaresCapturePermissionsAndOnboardingLinks() throws {
    let iOSInfo = try loadProjectPlist("AppResources/iOS/Info.plist")
    let macInfo = try loadProjectPlist("AppResources/macOS/Info.plist")
    let iOSEntitlements = try loadProjectPlist("AppResources/iOS/BTQFieldCapture.entitlements")
    let macEntitlements = try loadProjectPlist("AppResources/BTQFieldCapture.entitlements")
    let privacyManifest = try loadProjectPlist("AppResources/PrivacyInfo.xcprivacy")

    for info in [iOSInfo, macInfo] {
        let schemes = urlSchemes(in: info)
        #expect(schemes.contains("btq-field-capture"))
        #expect(schemes.contains("btq"))
        #expect(info["CFBundleExecutable"] as? String == "$(EXECUTABLE_NAME)")
        #expect(info["CFBundleShortVersionString"] as? String == "$(MARKETING_VERSION)")
        #expect(info["CFBundleVersion"] as? String == "$(CURRENT_PROJECT_VERSION)")
        #expect(nonEmptyString(info["NSCameraUsageDescription"]))
        #expect(nonEmptyString(info["NSMicrophoneUsageDescription"]))
        #expect(nonEmptyString(info["NSPhotoLibraryUsageDescription"]))
        #expect(nonEmptyString(info["NSUserNotificationsUsageDescription"]))
    }

    let backgroundModes = try #require(iOSInfo["UIBackgroundModes"] as? [String])
    #expect(backgroundModes.contains("audio"))
    #expect(backgroundModes.contains("fetch"))
    #expect(backgroundModes.contains("processing"))

    let permittedIdentifiers = try #require(iOSInfo["BGTaskSchedulerPermittedIdentifiers"] as? [String])
    #expect(permittedIdentifiers.contains(BackgroundUploadSupport.sessionIdentifier))

    let backgroundSyncScheduler = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Services/BackgroundSyncScheduler.swift"),
        encoding: .utf8
    )
    #expect(backgroundSyncScheduler.contains("BGTaskScheduler.shared.register"))
    #expect(backgroundSyncScheduler.contains("BGProcessingTaskRequest"))
    #expect(backgroundSyncScheduler.contains("BackgroundUploadSupport.sessionIdentifier"))
    #expect(backgroundSyncScheduler.contains("requiresNetworkConnectivity = true"))
    #expect(backgroundSyncScheduler.contains("beginExpiringSyncIfNeeded"))
    #expect(backgroundSyncScheduler.contains("beginBackgroundTask(withName: \"BTQ Capture Sync\")"))
    #expect(backgroundSyncScheduler.contains("endBackgroundTask(backgroundTask)"))

    let iOSApp = try String(
        contentsOf: packageRoot().appendingPathComponent("AppTargets/iOS/BTQFieldCaptureiOSApp.swift"),
        encoding: .utf8
    )
    #expect(iOSApp.contains("IOSBackgroundSyncTaskHandler.register"))
    #expect(iOSApp.contains("IOSBackgroundSyncScheduler()"))
    #expect(iOSApp.contains("UNUserNotificationCenter.current().delegate"))
    #expect(iOSApp.contains("willPresent notification"))
    #expect(iOSApp.contains("[.banner, .list, .sound]"))

    let associatedDomains = try #require(iOSEntitlements["com.apple.developer.associated-domains"] as? [String])
    #expect(associatedDomains.contains("applinks:fc.gregstoltz.com"))
    #expect(macEntitlements["com.apple.security.app-sandbox"] as? Bool == true)
    #expect(macEntitlements["com.apple.security.network.client"] as? Bool == true)
    #expect(macEntitlements["com.apple.security.device.camera"] as? Bool == true)
    #expect(macEntitlements["com.apple.security.device.audio-input"] as? Bool == true)
    #expect(privacyManifest["NSPrivacyTracking"] as? Bool == false)
    #expect((privacyManifest["NSPrivacyTrackingDomains"] as? [String])?.isEmpty == true)
    #expect((privacyManifest["NSPrivacyAccessedAPITypes"] as? [Any])?.isEmpty == true)

    let projectFile = try String(
        contentsOf: packageRoot().appendingPathComponent("BTQFieldCapture.xcodeproj/project.pbxproj"),
        encoding: .utf8
    )
    let iOSEntitlementReferences = projectFile.components(separatedBy: "CODE_SIGN_ENTITLEMENTS = AppResources/iOS/BTQFieldCapture.entitlements;").count - 1
    let macEntitlementReferences = projectFile.components(separatedBy: "CODE_SIGN_ENTITLEMENTS = AppResources/BTQFieldCapture.entitlements;").count - 1
    #expect(iOSEntitlementReferences == 2)
    #expect(macEntitlementReferences == 2)
    #expect(projectFile.contains("PrivacyInfo.xcprivacy in Resources"))
    #expect(projectFile.contains("Assets.xcassets in Resources"))
    #expect(projectFile.components(separatedBy: "ASSETCATALOG_COMPILER_APPICON_NAME = AppIcon;").count - 1 == 4)
    #expect(projectFile.contains("PRODUCT_BUNDLE_IDENTIFIER = com.btq.fieldcapture;"))
    #expect(projectFile.contains("PRODUCT_BUNDLE_IDENTIFIER = com.btq.fieldcapture.mac;"))
    #expect(projectFile.components(separatedBy: "MARKETING_VERSION = 1.0;").count - 1 == 4)
    #expect(projectFile.contains("SUPPORTED_PLATFORMS = \"iphoneos iphonesimulator\";"))
    #expect(projectFile.components(separatedBy: "baseConfigurationReference = 10A000000000000000000019 /* Signing.xcconfig */;").count - 1 == 4)
    #expect(!projectFile.contains("DEVELOPMENT_TEAM = "))

    let signingConfig = try String(
        contentsOf: packageRoot().appendingPathComponent("Signing.xcconfig"),
        encoding: .utf8
    )
    #expect(signingConfig.contains("DEVELOPMENT_TEAM =\n"))
    #expect(signingConfig.contains("#include? \"Local.xcconfig\""))

    let localSigningExample = try String(
        contentsOf: packageRoot().appendingPathComponent("Local.xcconfig.example"),
        encoding: .utf8
    )
    #expect(localSigningExample.contains("DEVELOPMENT_TEAM = <team id>"))
    #expect(localSigningExample.range(of: #"DEVELOPMENT_TEAM = [A-Z0-9]{10}"#, options: .regularExpression) == nil)

    let testFlightBuilder = try String(
        contentsOf: packageRoot().appendingPathComponent("script/build_testflight.sh"),
        encoding: .utf8
    )
    let testFlightEnvExample = try String(
        contentsOf: packageRoot().appendingPathComponent("script/testflight.env.example"),
        encoding: .utf8
    )
    let exportOptionsExample = try String(
        contentsOf: packageRoot().appendingPathComponent("Release/ExportOptions.plist.example"),
        encoding: .utf8
    )
    #expect(testFlightBuilder.contains("xcodebuild"))
    #expect(testFlightBuilder.contains("ARCHIVE_CMD=("))
    #expect(testFlightBuilder.contains("EXPORT_CMD=("))
    #expect(testFlightBuilder.contains("-exportArchive"))
    #expect(testFlightBuilder.contains("--upload"))
    #expect(testFlightBuilder.contains("BTQ_ASC_KEY_ID"))
    #expect(testFlightBuilder.contains("BTQ_ASC_ISSUER_ID"))
    #expect(testFlightBuilder.contains("BTQ_ASC_KEY_PATH"))
    #expect(testFlightBuilder.contains("CURRENT_PROJECT_VERSION=\"$BUILD_NUMBER\""))
    #expect(testFlightBuilder.contains("MARKETING_VERSION=\"$MARKETING_VERSION\""))
    #expect(testFlightEnvExample.contains("BTQ_ASC_KEY_ID"))
    #expect(testFlightEnvExample.contains("BTQ_ASC_ISSUER_ID"))
    #expect(testFlightEnvExample.contains("BTQ_ASC_KEY_PATH"))
    #expect(testFlightEnvExample.contains("BTQ_TESTFLIGHT_INTERNAL_ONLY"))
    #expect(exportOptionsExample.contains("<string>app-store-connect</string>"))
    #expect(exportOptionsExample.contains("<string>export</string>"))

    let packageManifest = try String(
        contentsOf: packageRoot().appendingPathComponent("Package.swift"),
        encoding: .utf8
    )
    #expect(packageManifest.contains("BTQFieldCaptureAPISmoke"))
    #expect(packageManifest.contains("BTQFieldCaptureLiveAPIVerifier"))
    #expect(packageManifest.contains(".process(\"Resources\")"))

    let brandHeaderView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/FieldCaptureBrandHeader.swift"),
        encoding: .utf8
    )
    #expect(brandHeaderView.contains("Image(\"FieldCaptureHeader\", bundle: .module)"))
    #expect(brandHeaderView.contains(".frame(width: 276, height: 56"))
    #expect(brandHeaderView.contains("\"brand.field-capture.header\""))

    let captureNotebookView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift"),
        encoding: .utf8
    )
    #expect(captureNotebookView.contains("FieldCaptureBrandHeader()"))
    #expect(captureNotebookView.contains("private var captureHeader"))
    #expect(captureNotebookView.contains("\"capture.header.sync\""))
    #expect(captureNotebookView.contains(".toolbar(.hidden, for: .navigationBar)"))
    #expect(captureNotebookView.contains(".padding(.top, 8)"))
    #expect(captureNotebookView.contains(".safeAreaInset(edge: .bottom)"))
    #expect(captureNotebookView.contains("TextField(\"Observation note\", text: $model.observationText, axis: .vertical)"))
    #expect(captureNotebookView.contains(".lineLimit(3...5)"))

    let paletteSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Support/BTQPalette.swift"),
        encoding: .utf8
    )
    #expect(paletteSource.contains("static let btqAccent"))
    #expect(paletteSource.contains("static let btqNavy"))
    #expect(paletteSource.contains("static let btqUploading"))

    let brandHeaderCatalog = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Resources/Brand.xcassets/FieldCaptureHeader.imageset/Contents.json"),
        encoding: .utf8
    )
    #expect(brandHeaderCatalog.contains("field-capture-header-light@3x.png"))
    #expect(brandHeaderCatalog.contains("field-capture-header-dark@3x.png"))
    #expect(brandHeaderCatalog.contains("\"appearance\": \"luminosity\""))

    let appIconCatalog = try String(
        contentsOf: packageRoot().appendingPathComponent("AppResources/Assets.xcassets/AppIcon.appiconset/Contents.json"),
        encoding: .utf8
    )
    #expect(appIconCatalog.contains("app-icon-1024.png"))
    #expect(appIconCatalog.contains("\"idiom\": \"ios-marketing\""))
    #expect(appIconCatalog.contains("\"idiom\": \"mac\""))

    #expect(FileManager.default.fileExists(atPath: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Resources/Brand.xcassets/FieldCaptureHeader.imageset/field-capture-header-light@3x.png").path))
    #expect(try imagePixelSize(at: "Sources/BTQFieldCaptureApp/Resources/Brand.xcassets/FieldCaptureHeader.imageset/field-capture-header-light@3x.png") == CGSize(width: 828, height: 168))
    #expect(try imagePixelSize(at: "AppResources/Assets.xcassets/AppIcon.appiconset/app-icon-1024.png") == CGSize(width: 1_024, height: 1_024))
    #expect(try !imageHasAlpha(at: "AppResources/Assets.xcassets/AppIcon.appiconset/app-icon-1024.png"))

    let simulatorVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_ios_simulator.sh"),
        encoding: .utf8
    )
    #expect(simulatorVerifier.contains("xcrun simctl bootstatus"))
    #expect(simulatorVerifier.contains("xcrun simctl install"))
    #expect(simulatorVerifier.contains("xcrun simctl launch"))
    #expect(simulatorVerifier.contains("CODE_SIGNING_ALLOWED=NO"))
    #expect(simulatorVerifier.contains("BTQ_SIMULATOR_FAMILY"))
    #expect(simulatorVerifier.contains("BTQ_SIMULATOR_NAME"))

    let deviceVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_ios_device.sh"),
        encoding: .utf8
    )
    #expect(deviceVerifier.contains("BTQ_DEVELOPMENT_TEAM"))
    #expect(deviceVerifier.contains("BTQ_DEVICE_NAME"))
    #expect(deviceVerifier.contains("ios_device.env.example"))
    #expect(deviceVerifier.contains("load_ios_device_env.sh"))
    #expect(deviceVerifier.contains("-allowProvisioningUpdates"))
    #expect(deviceVerifier.contains("xcrun devicectl device install app"))
    #expect(deviceVerifier.contains("xcrun devicectl device process launch"))
    #expect(deviceVerifier.contains("xcrun devicectl device info details"))
    #expect(deviceVerifier.contains("developerModeStatus: disabled"))
    #expect(deviceVerifier.contains("Developer Mode"))
    #expect(deviceVerifier.range(of: #"DEVELOPMENT_TEAM=[A-Z0-9]{10}"#, options: .regularExpression) == nil)

    let deviceEnvExample = try String(
        contentsOf: packageRoot().appendingPathComponent("script/ios_device.env.example"),
        encoding: .utf8
    )
    #expect(deviceEnvExample.contains("BTQ_DEVELOPMENT_TEAM=\"<team id>\""))
    #expect(deviceEnvExample.contains("BTQ_DEVICE_NAME=\"<device name>\""))
    #expect(deviceEnvExample.range(of: #"BTQ_DEVELOPMENT_TEAM=\"[A-Z0-9]{10}\""#, options: .regularExpression) == nil)

    let gitignore = try String(
        contentsOf: packageRoot().appendingPathComponent(".gitignore"),
        encoding: .utf8
    )
    #expect(gitignore.contains("script/ios_device.env"))
    #expect(gitignore.contains("Local.xcconfig"))

    let macOSVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_macos_app.sh"),
        encoding: .utf8
    )
    #expect(macOSVerifier.contains("SCHEME=\"BTQ Capture Mac\""))
    #expect(macOSVerifier.contains("platform=macOS"))
    #expect(macOSVerifier.contains("com.btq.fieldcapture.mac"))
    #expect(macOSVerifier.contains("PrivacyInfo.xcprivacy"))
    #expect(macOSVerifier.contains("--launch"))

    let mockAPIVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_mock_api_submit.sh"),
        encoding: .utf8
    )
    #expect(mockAPIVerifier.contains("mock_capture_api_server.py"))
    #expect(mockAPIVerifier.contains("BTQFieldCaptureAPISmoke"))
    #expect(mockAPIVerifier.contains("btq-smoke-token"))

    let mockAPIServer = try String(
        contentsOf: packageRoot().appendingPathComponent("script/mock_capture_api_server.py"),
        encoding: .utf8
    )
    #expect(mockAPIServer.contains("/api/session"))
    #expect(mockAPIServer.contains("/api/my-submissions"))
    #expect(mockAPIServer.contains("/api/submit"))
    #expect(mockAPIServer.contains("metadata_json"))
    #expect(mockAPIServer.contains("photo_notes_json"))

    let liveAPIVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_live_api.sh"),
        encoding: .utf8
    )
    #expect(liveAPIVerifier.contains("BTQ_LIVE_TOKEN"))
    #expect(liveAPIVerifier.contains("BTQ_LIVE_SUBMIT=1"))
    #expect(liveAPIVerifier.contains("BTQFieldCaptureLiveAPIVerifier"))
    #expect(liveAPIVerifier.contains("mySubmissions"))

    let liveAPISource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureLiveAPIVerifier/main.swift"),
        encoding: .utf8
    )
    #expect(liveAPISource.contains("BTQ_LIVE_TOKEN"))
    #expect(liveAPISource.contains("BTQ_LIVE_SUBMIT"))
    #expect(liveAPISource.contains("HTTPCaptureAPIClient"))
    #expect(liveAPISource.contains("client.mySubmissions"))
    #expect(liveAPISource.contains("validate(history:"))
    #expect(liveAPISource.contains("invalidSubmittedHistory"))
    #expect(!liveAPISource.contains("btq-smoke-token"))

    let universalLinkVerifier = try String(
        contentsOf: packageRoot().appendingPathComponent("script/verify_universal_links.sh"),
        encoding: .utf8
    )
    #expect(universalLinkVerifier.contains("applinks:fc.gregstoltz.com"))
    #expect(universalLinkVerifier.contains("apple_app_site_association_payload"))
    #expect(universalLinkVerifier.contains("/.well-known/apple-app-site-association"))
    #expect(universalLinkVerifier.contains("BTQ_AASA_BASE_URL"))
    #expect(universalLinkVerifier.contains(".com.btq.fieldcapture"))

    let fieldPilotReadiness = try String(
        contentsOf: packageRoot().appendingPathComponent("script/field_pilot_readiness.sh"),
        encoding: .utf8
    )
    #expect(fieldPilotReadiness.contains("swift test"))
    #expect(fieldPilotReadiness.contains("script/check_release_readiness.sh"))
    #expect(fieldPilotReadiness.contains("script/verify_live_api.sh --check"))
    #expect(fieldPilotReadiness.contains("script/verify_universal_links.sh --check"))
    #expect(fieldPilotReadiness.contains("script/verify_mock_api_submit.sh"))
    #expect(fieldPilotReadiness.contains("script/verify_macos_app.sh"))
    #expect(fieldPilotReadiness.contains("script/verify_ios_simulator.sh"))
    #expect(fieldPilotReadiness.contains("BTQ_SIMULATOR_FAMILY=iPad"))
    #expect(fieldPilotReadiness.contains("BTQ_LIVE_TOKEN"))
    #expect(fieldPilotReadiness.contains("script/verify_ios_device.sh"))
    #expect(fieldPilotReadiness.contains("skipped optional gates"))
    #expect(!fieldPilotReadiness.contains("sh script/verify_"))

    let releaseReadiness = try String(
        contentsOf: packageRoot().appendingPathComponent("Release/AppStoreReadiness.md"),
        encoding: .utf8
    )
    #expect(!releaseReadiness.contains("sh script/verify_"))
    #expect(releaseReadiness.contains("./script/verify_live_api.sh"))
    #expect(releaseReadiness.contains("./script/verify_universal_links.sh"))
    #expect(releaseReadiness.contains("Local.xcconfig.example"))
    #expect(releaseReadiness.contains("DEVELOPMENT_TEAM"))
    #expect(releaseReadiness.contains("App Store Connect And TestFlight"))
    #expect(releaseReadiness.contains("script/testflight.env.example"))
    #expect(releaseReadiness.contains("./script/build_testflight.sh --check"))
    #expect(releaseReadiness.contains("./script/build_testflight.sh --upload"))
    #expect(releaseReadiness.contains("App Store Connect API key"))
    #expect(releaseReadiness.contains("missingApp(bundleId: \"com.btq.fieldcapture\")"))
    #expect(releaseReadiness.contains("Internal testers do"))
    #expect(releaseReadiness.contains("duplicate build numbers"))
    #expect(!releaseReadiness.contains("set the Development Team"))
    #expect(releaseReadiness.contains("Do not store a personal Team ID by editing"))
    #expect(releaseReadiness.contains("Physical Device Validation Log"))
    #expect(releaseReadiness.contains("Live image capture submitted from the native iPhone app"))
    #expect(releaseReadiness.contains("Live audio recording submitted from the native iPhone app"))
    #expect(releaseReadiness.contains("Settings test alert displayed successfully"))
    #expect(releaseReadiness.contains("Heavy Photos picker capture succeeded on the iPhone"))
    #expect(releaseReadiness.contains("TestFlight build `202606141343` installed successfully on both"))
    #expect(releaseReadiness.contains("Remaining Physical Device Checks"))

    let captureView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift"),
        encoding: .utf8
    )
    #expect(captureView.contains("\"capture.status.pending\""))
    #expect(captureView.contains("\"capture.observation.text\""))
    #expect(captureView.contains("import UIKit"))
    #expect(captureView.contains("ToolbarItemGroup(placement: .keyboard)"))
    #expect(captureView.contains("dismissKeyboard()"))
    #expect(captureView.contains("UIApplication.shared.sendAction"))
    #expect(captureView.contains(".navigationTitle(captureNavigationTitle)"))
    #expect(captureView.contains("private var captureNavigationTitle: String"))
    #expect(captureView.contains(".navigationBarTitleDisplayMode(.inline)"))
    #expect(captureView.contains(".toolbar(.hidden, for: .navigationBar)"))
    #expect(captureView.contains("HeaderIconButtonStyle"))
    #expect(captureView.contains("\"capture.header.sync\""))
    #expect(captureView.contains(".padding(.top, 8)"))
    #expect(captureView.contains(".safeAreaInset(edge: .bottom)"))
    #expect(captureView.contains("TextField(\"Observation note\", text: $model.observationText, axis: .vertical)"))
    #expect(captureView.contains(".lineLimit(3...5)"))
    #expect(captureView.contains("\"capture.save.local\""))
    #expect(captureView.contains("isImportingPhotos"))
    #expect(captureView.contains("photoImportMessage"))
    #expect(captureView.contains("PickedPhotoFile"))
    #expect(captureView.contains("FileRepresentation(importedContentType: .image)"))
    #expect(captureView.contains("savePhoto(fileURL: pickedPhoto.fileURL, prefix: \"photo\")"))
    #expect(!captureView.contains("item.loadTransferable(type: Data.self)"))
    #expect(captureView.contains("\"capture.photos.picker\""))
    #expect(captureView.contains("\"capture.photos.importing\""))
    #expect(captureView.contains("\"capture.photos.import.message\""))
    #expect(captureView.contains("Importing selected photos..."))
    #expect(captureView.contains("await Task.yield()"))
    #expect(captureView.contains("Clear pending media"))
    #expect(captureView.contains("showingClearDraftMediaConfirmation"))
    #expect(captureView.contains("Clear pending media?"))
    #expect(captureView.contains("Clear Media\", role: .destructive"))
    #expect(captureView.contains("unsaved photos and voice memo from the current draft"))
    #expect(captureView.contains("Photo note for"))
    #expect(captureView.contains("Starts recording a voice memo."))
    #expect(captureView.contains("\"voice.record\""))
    #expect(captureView.contains("\"voice.pause\""))
    #expect(captureView.contains("\"voice.resume\""))
    #expect(captureView.contains("\"voice.stop\""))
    #expect(captureView.contains("\"voice.clear\""))
    #expect(captureView.contains("\"voice.playback.play\""))
    #expect(captureView.contains("Play Voice Memo"))
    #expect(captureView.contains("Text(\"Photos\")"))
    #expect(captureView.contains("\"capture.photos.count\""))
    #expect(captureView.contains("Text(\"Voice Note\")"))
    #expect(captureView.contains("Text(\"Optional \\(voiceDurationLabel)\")"))
    #expect(captureView.contains("\"voice.duration\""))
    #expect(captureView.contains("CaptureToolLabel(title: \"Take Photo\", icon: .camera)"))
    #expect(captureView.contains("CaptureToolLabel(title: photoPickerTitle, icon: .library)"))
    #expect(captureView.contains("\"Camera Roll\""))
    #expect(captureView.contains("PWACameraGlyph"))
    #expect(captureView.contains("PWAPhotoLibraryGlyph"))
    #expect(captureView.contains("TapeRecordButtonStyle"))
    #expect(captureView.contains("TapeStopButtonStyle"))
    #expect(captureView.contains("TapeClearButtonStyle"))
    #expect(captureView.contains(".tint(.btqAccent)"))
    #expect(!captureView.contains("case .uploading: .blue"))
    #expect(captureView.contains("Text(\"●\")"))
    #expect(captureView.contains("Text(\"■\")"))
    #expect(captureView.contains("Text(\"×\")"))
    #expect(captureView.contains("Voice memo ready"))
    #expect(captureView.contains("\"voice.status\""))

    let queueView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/QueueView.swift"),
        encoding: .utf8
    )
    let appVersionFooter = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/AppVersionFooter.swift"),
        encoding: .utf8
    )
    let photoThumbnail = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CapturePhotoThumbnail.swift"),
        encoding: .utf8
    )
    let inboxView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/InboxView.swift"),
        encoding: .utf8
    )
    #expect(queueView.contains("\"queue.summary.\\(label.lowercased())\""))
    #expect(queueView.contains("\"queue.capture.\\(capture.captureID)\""))
    #expect(queueView.contains("\"queue.server.refresh\""))
    #expect(queueView.contains("\"queue.server.capture.\\(submission.captureID)\""))
    #expect(queueView.contains("case .done: .btqAccent"))
    #expect(queueView.contains("case .uploading: .btqUploading"))
    #expect(queueView.contains("DisclosureGroup(isExpanded: $isExpanded)"))
    #expect(queueView.contains("QueueCaptureDetailSheet(detail: detail)"))
    #expect(queueView.contains("selectedDetail = .local(capture)"))
    #expect(queueView.contains("selectedDetail = .submitted(submission)"))
    #expect(queueView.contains("LocalCaptureDetailContent(capture: capture)"))
    #expect(queueView.contains("SubmittedCaptureDetailContent(submission: submission)"))
    #expect(queueView.contains("CapturePhotoThumbnail(photo: photo)"))
    #expect(queueView.contains("Retry upload for"))
    #expect(queueView.contains("Moves this failed capture back to pending."))
    #expect(queueView.contains("failureRecoveryHint"))
    #expect(queueView.contains("Recovery guidance:"))
    #expect(queueView.contains(".disabled(model.isSyncing || !model.canSubmitCaptures)"))
    #expect(queueView.contains("accessibilitySummary"))
    #expect(queueView.contains("Flags:"))
    #expect(queueView.contains("showingDeleteConfirmation"))
    #expect(queueView.contains("Delete this local capture?"))
    #expect(queueView.contains("Delete Capture\", role: .destructive"))
    #expect(queueView.contains("queued capture and any app-owned photo or voice memo files"))
    #expect(queueView.contains("AppVersionFooter()"))
    #expect(appVersionFooter.contains("CFBundleShortVersionString"))
    #expect(appVersionFooter.contains("CFBundleVersion"))
    #expect(appVersionFooter.contains("\"app.version.footer\""))
    #expect(photoThumbnail.contains("struct CapturePhotoThumbnail"))
    #expect(photoThumbnail.contains("Data(contentsOf: fileURL)"))
    #expect(inboxView.contains("struct InboxView"))
    #expect(inboxView.contains("Approval Inbox"))
    #expect(inboxView.contains("model.refreshInbox()"))
    #expect(inboxView.contains("\"inbox.refresh\""))
    #expect(inboxView.contains("model.reviewInboxItem(item, action: .reject)"))
    #expect(inboxView.contains("model.reviewInboxItem(item, action: .approve)"))
    #expect(inboxView.contains("model.reviewInboxSet(group, approvedDraftIDs: approvedDraftIDs)"))
    #expect(inboxView.contains("PayloadRows(payload: item.payload)"))
    #expect(inboxView.contains(".disabled(model.isReviewingInboxItem || model.isOfflineMode)"))

    let sitesView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/SitesView.swift"),
        encoding: .utf8
    )
    #expect(sitesView.contains("Select site"))
    #expect(sitesView.contains("var onSiteSelected: () -> Void = {}"))
    #expect(sitesView.contains("onSiteSelected()"))
    #expect(sitesView.contains("site.siteID == model.selectedSiteID"))
    #expect(sitesView.contains(".accessibilityValue(site.siteID == model.selectedSiteID ? \"Selected\" : \"Not selected\")"))
    #expect(sitesView.contains("returns to Capture"))
    #expect(sitesView.contains("Add \\(site.label) to favorites"))
    #expect(sitesView.contains("Remove \\(site.label) from favorites"))
    #expect(sitesView.contains("Favorites appear first in the site list."))

    let settingsView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/SettingsView.swift"),
        encoding: .utf8
    )
    let rootView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/BTQFieldCaptureRootView.swift"),
        encoding: .utf8
    )
    let initialSetupView = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/InitialSetupView.swift"),
        encoding: .utf8
    )
    let modelSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Stores/FieldCaptureModel.swift"),
        encoding: .utf8
    )
    #expect(settingsView.contains("\"settings.notifications.status\""))
    #expect(settingsView.contains("\"settings.notifications.enable\""))
    #expect(settingsView.contains("\"settings.notifications.test\""))
    #expect(settingsView.contains("Send Test Alert"))
    #expect(settingsView.contains("model.sendTestNotification()"))
    #expect(settingsView.contains("Send Failure Alert"))
    #expect(settingsView.contains("model.sendTestUploadFailureNotification()"))
    #expect(settingsView.contains("\"settings.notifications.test.failure\""))
    #expect(settingsView.contains(".disabled(!model.notificationPermissionStatus.allowsScheduling)"))
    #expect(settingsView.contains("Section(\"Screen mode\")"))
    #expect(settingsView.contains("Picker(\"Screen mode\", selection: $screenMode)"))
    #expect(settingsView.contains("ForEach(ScreenMode.allCases)"))
    #expect(settingsView.contains("\"settings.screen.mode\""))
    #expect(settingsView.contains("showingRemoveAccountConfirmation"))
    #expect(settingsView.contains(".confirmationDialog("))
    #expect(settingsView.contains("Remove this account?"))
    #expect(settingsView.contains("Remove Account\", role: .destructive"))
    #expect(settingsView.contains("cached workspace and stored token"))
    #expect(settingsView.contains(".textInputAutocapitalization(.never)"))
    #expect(settingsView.contains(".autocorrectionDisabled()"))
    #expect(settingsView.contains(".privacySensitive()"))
    #expect(settingsView.contains("@FocusState private var isTokenInputFocused"))
    #expect(settingsView.contains("ToolbarItemGroup(placement: .keyboard)"))
    #expect(settingsView.contains(".focused($isTokenInputFocused)"))
    #expect(settingsView.contains("isTokenInputFocused = false"))
    #expect(settingsView.contains("if didConnect"))
    #expect(settingsView.contains("tokenOrLink = \"\""))
    #expect(settingsView.contains("onConnected()"))
    let screenModeSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Support/ScreenMode.swift"),
        encoding: .utf8
    )
    #expect(screenModeSource.contains("case system"))
    #expect(screenModeSource.contains("case light"))
    #expect(screenModeSource.contains("case dark"))
    #expect(screenModeSource.contains("preferredColorScheme"))
    #expect(screenModeSource.contains("ScreenMode(rawValue: rawValue) ?? .system"))
    #expect(rootView.contains("@AppStorage(\"btq.screenMode\")"))
    #expect(rootView.contains(".preferredColorScheme(ScreenMode.normalized(screenModeRaw).preferredColorScheme)"))
    #expect(rootView.contains(".tint(.btqAccent)"))
    #expect(rootView.contains("BTQFieldCaptureShell(model: model, screenMode: screenMode)"))
    #expect(rootView.contains("case inbox"))
    #expect(rootView.contains("static func visible(canReview: Bool)"))
    #expect(rootView.contains("section != .inbox || canReview"))
    #expect(rootView.contains(".badge(item == .inbox ? model.inboxBadgeCount : 0)"))
    #expect(rootView.contains("InboxView(model: model)"))
    #expect(rootView.contains("if model.needsInitialSetup"))
    #expect(rootView.contains("InitialSetupView(model: model, onConnected: routeToCapture)"))
    #expect(rootView.contains(".onOpenURL { url in"))
    #expect(rootView.contains("Task { await handleOnboardingURL(url) }"))
    #expect(rootView.contains("SettingsView(model: model, screenMode: $screenMode, onConnected: routeToCapture)"))
    #expect(rootView.contains("SitesView(model: model, onSiteSelected: routeToCapture)"))
    #expect(rootView.contains("private func handleOnboardingURL(_ url: URL) async"))
    #expect(rootView.contains("if await model.connectWithOnboardingURL(url)"))
    #expect(rootView.contains("private func routeToCapture()"))
    #expect(rootView.contains("section = .capture"))
    #expect(modelSource.contains("public var needsInitialSetup: Bool"))
    #expect(modelSource.contains("account.tokenID == nil || requiresReconnect"))
    #expect(initialSetupView.contains("struct InitialSetupView"))
    #expect(initialSetupView.contains("FieldCaptureBrandHeader()"))
    #expect(initialSetupView.contains("Paste the setup token from your TestFlight notes."))
    #expect(initialSetupView.contains("TextField(\"Paste setup token\""))
    #expect(initialSetupView.contains(".privacySensitive()"))
    #expect(initialSetupView.contains("model.connectWithOnboardingURL(url)"))
    #expect(initialSetupView.contains("model.connect(token: value)"))
    #expect(initialSetupView.contains("onConnected()"))
    #expect(initialSetupView.contains("\"initial.setup.token\""))
    #expect(initialSetupView.contains("\"initial.setup.connect\""))
    #expect(settingsView.contains("connectButtonLabel"))
    #expect(settingsView.contains("model.isConnecting ? \"Connecting\" : \"Connect\""))
    #expect(settingsView.contains(".disabled(model.isConnecting || tokenOrLink.trimmingCharacters"))
    #expect(settingsView.contains(".disabled(model.isSyncing || model.isConnecting)"))
    #expect(settingsView.contains("guard !isSelected else { return }"))
    #expect(settingsView.contains(".buttonStyle(.plain)"))
    #expect(settingsView.contains(".accessibilityValue(isSelected ? \"Active account\" : \"Inactive account\")"))
    #expect(settingsView.contains("Label(\"Active\", systemImage: \"checkmark.circle.fill\")"))
}

@Test func multipartFieldsMatchSubmitContract() {
    let capturedAt = Date(timeIntervalSince1970: 1_800_000_000)
    let capture = LocalCapture(
        captureID: "cap-unified-test",
        jobID: "job-test",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "supplies",
        note: "Need towels",
        capturedAt: capturedAt,
        exportedAt: capturedAt
    )

    let fields = Dictionary(uniqueKeysWithValues: MultipartCaptureBuilder.fields(for: capture))

    #expect(fields["job_id"] == "job-test")
    #expect(fields["capture_id"] == "cap-unified-test")
    #expect(fields["site"] == "Site One")
    #expect(fields["site_id"] == "site_1")
    #expect(fields["target_type"] == "location")
    #expect(fields["target_id"] == "site_1")
    #expect(fields["qc_category"] == "supplies")
    #expect(fields["note"] == "Need towels")
    #expect(fields["captured_at"] != nil)
    #expect(fields["exported_at"] != nil)
}

@Test func multipartFieldsIncludePhotoNotesWhenPresent() throws {
    let capture = LocalCapture(
        captureID: "cap-unified-photo-note",
        jobID: "job-photo-note",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "cleaning_quality",
        note: "Two photos",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [
            CapturePhoto(filename: "before.jpg", note: "Before cleanup"),
            CapturePhoto(filename: "after.jpg"),
        ]
    )

    let fields = Dictionary(uniqueKeysWithValues: MultipartCaptureBuilder.fields(for: capture))
    let rawNotes = try #require(fields["photo_notes_json"])
    let notes = try JSONDecoder().decode([PhotoNoteExpectation].self, from: Data(rawNotes.utf8))

    #expect(notes.count == 1)
    #expect(notes.first?.index == 0)
    #expect(notes.first?.filename == "before.jpg")
    #expect(notes.first?.note == "Before cleanup")
}

@Test func multipartFieldsIncludeNativeClientMetadata() throws {
    let visitID = UUID(uuidString: "00000000-0000-0000-0000-000000000123")!
    let capture = LocalCapture(
        captureID: "cap-unified-metadata",
        jobID: "job-metadata",
        visitID: visitID,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Metadata test",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [CapturePhoto(filename: "photo.jpg")],
        audio: CaptureAudio(filename: "voice.m4a", durationSeconds: 12)
    )

    let fields = Dictionary(uniqueKeysWithValues: MultipartCaptureBuilder.fields(for: capture))
    let rawMetadata = try #require(fields["metadata_json"])
    let metadata = try JSONDecoder().decode(ClientMetadataExpectation.self, from: Data(rawMetadata.utf8))

    #expect(metadata.schemaVersion == 1)
    #expect(metadata.client == "btq_native_apple")
    #expect(metadata.visitID == visitID.uuidString)
    #expect(metadata.siteID == "site_1")
    #expect(metadata.assetKind == "photo-voice")
    #expect(metadata.photoCount == 1)
    #expect(metadata.hasAudio)
    #expect(metadata.audioDurationSeconds == 12)
}

@Test func fileBackedMultipartBodyContainsFieldsAndMedia() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-test-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let photoURL = temp.appendingPathComponent("photo.jpg")
    let audioURL = temp.appendingPathComponent("voice.m4a")
    try Data("fake-photo".utf8).write(to: photoURL)
    try Data("fake-audio".utf8).write(to: audioURL)

    let capture = LocalCapture(
        captureID: "cap-unified-file-test",
        jobID: "job-file-test",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "cleaning_quality",
        note: "Photo and voice",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [CapturePhoto(filename: "photo.jpg", fileURL: photoURL)],
        audio: CaptureAudio(filename: "voice.m4a", fileURL: audioURL, durationSeconds: 4)
    )

    let bodyURL = temp.appendingPathComponent("body.multipart")
    try MultipartCaptureBuilder.writeBody(for: capture, boundary: "TestBoundary", to: bodyURL)
    let body = try String(contentsOf: bodyURL, encoding: .utf8)

    #expect(body.contains("name=\"capture_id\""))
    #expect(body.contains("cap-unified-file-test"))
    #expect(body.contains("name=\"photos\"; filename=\"photo.jpg\""))
    #expect(body.contains("fake-photo"))
    #expect(body.contains("name=\"audio\"; filename=\"voice.m4a\""))
    #expect(body.contains("fake-audio"))
    #expect(body.contains("name=\"audio_duration_seconds\""))

    let apiClientSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Services/CaptureAPIClient.swift"),
        encoding: .utf8
    )
    #expect(apiClientSource.contains("writeFileContents(from: fileURL, to: handle)"))
    #expect(apiClientSource.contains("read(upToCount: 256 * 1024)"))
}

@Test func fileBackedMultipartBodyCarriesOneHundredPhotoParts() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-hundred-photo-test-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let photos = try (1...100).map { index in
        let filename = "qc-\(index).jpg"
        let photoURL = temp.appendingPathComponent(filename)
        try Data("fake-photo-\(index)".utf8).write(to: photoURL)
        return CapturePhoto(filename: filename, fileURL: photoURL)
    }

    let capture = LocalCapture(
        captureID: "cap-qc-hundred-photo-test",
        jobID: "job-qc-hundred-photo-test",
        visitID: nil,
        siteID: "site_qc",
        siteLabel: "QC Site",
        targetID: "site_qc",
        qcCategory: "qc",
        note: "Large QC walk",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: photos
    )

    let bodyURL = temp.appendingPathComponent("body.multipart")
    try MultipartCaptureBuilder.writeBody(for: capture, boundary: "TestBoundary", to: bodyURL)
    let body = try String(contentsOf: bodyURL, encoding: .utf8)

    let photoPartCount = body.components(separatedBy: "name=\"photos\"; filename=").count - 1
    #expect(photoPartCount == 100)
    #expect(body.contains("name=\"qc_category\""))
    #expect(body.contains("qc"))
    #expect(body.contains("qc-1.jpg"))
    #expect(body.contains("qc-100.jpg"))
}

@Test func sqliteStoreRoundTripsOfflineSnapshot() async throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-sqlite-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let store = SQLiteFieldCaptureStore(fileURL: temp.appendingPathComponent("field_capture.sqlite3"))
    let capture = LocalCapture(
        captureID: "cap-unified-sqlite",
        jobID: "job-sqlite",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Stored offline",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        retryAfter: Date(timeIntervalSince1970: 1_800_000_010)
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: .demo,
        sites: BTQSession.demo.sites,
        visits: [Visit(siteID: "site_1", siteLabel: "Site One")],
        captures: [capture]
    )

    try await store.save(snapshot)
    let loaded = try await store.load()

    #expect(loaded.session?.person.name == "Demo Employee")
    #expect(loaded.sites.count == BTQSession.demo.sites.count)
    #expect(loaded.sites.first?.siteID == "site_sandy_sandbox")
    #expect(loaded.sites.first?.label == "Sandy Sandbox")
    #expect(!loaded.sites.contains { $0.label == "Dickinson Center" })
    #expect(loaded.visits.count == 1)
    #expect(loaded.captures.first?.captureID == "cap-unified-sqlite")
    #expect(loaded.captures.first?.retryAfter != nil)
}

@Test func sqliteStoreDeclaresDurabilityPragmas() throws {
    let source = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Stores/SQLiteFieldCaptureStore.swift"),
        encoding: .utf8
    )
    let migrateRange = try #require(source.range(of: "private func migrate(_ database: OpaquePointer) throws"))
    let tableRange = try #require(source.range(of: "CREATE TABLE IF NOT EXISTS metadata", range: migrateRange.lowerBound..<source.endIndex))
    let migrationBlock = source[migrateRange.lowerBound..<tableRange.lowerBound]

    #expect(migrationBlock.contains("PRAGMA busy_timeout=5000;"))
    #expect(migrationBlock.contains("PRAGMA journal_mode=WAL;"))
    #expect(migrationBlock.contains("PRAGMA synchronous=FULL;"))
}

@Test func localStoresApplyBackupExclusionAndFileProtectionHooks() throws {
    let privacySource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Support/LocalFilePrivacy.swift"),
        encoding: .utf8
    )
    let mediaStoreSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Services/LocalMediaStore.swift"),
        encoding: .utf8
    )
    let sqliteStoreSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Stores/SQLiteFieldCaptureStore.swift"),
        encoding: .utf8
    )

    #expect(privacySource.contains("setResourceValue(true, forKey: .isExcludedFromBackupKey)"))
    #expect(privacySource.contains("#if os(iOS)"))
    #expect(privacySource.contains(".protectionKey: FileProtectionType.complete"))
    #expect(mediaStoreSource.contains("try LocalFilePrivacy.prepareDirectory(rootDirectory)"))
    #expect(mediaStoreSource.contains("try LocalFilePrivacy.protectExistingItem(url)"))
    #expect(mediaStoreSource.contains("try LocalFilePrivacy.protectExistingItem(destination)"))
    #expect(sqliteStoreSource.contains("try LocalFilePrivacy.prepareDirectory(fileURL.deletingLastPathComponent())"))
    #expect(sqliteStoreSource.contains("try LocalFilePrivacy.protectExistingItem(fileURL)"))
    #expect(sqliteStoreSource.contains("fileURL.path + \"-wal\""))
    #expect(sqliteStoreSource.contains("fileURL.path + \"-shm\""))
}

@Test func localMediaStorePersistsPhotosAndAudioOutsideTemporaryInputs() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-media-\(UUID().uuidString)", isDirectory: true)
    let source = FileManager.default.temporaryDirectory.appendingPathComponent("btq-media-source-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: temp)
        try? FileManager.default.removeItem(at: source)
    }

    let store = LocalMediaStore(rootDirectory: temp)
    let photo = try store.savePhotoData(makeTestImageData(type: .png), preferredStem: "Camera Photo", bucketID: "visit/one")

    #expect(photo.fileURL?.path.hasPrefix(temp.path) == true)
    #expect(photo.filename.hasSuffix(".jpg"))
    #expect(photo.mimeType == "image/jpeg")
    #expect(CGImageSourceCreateWithData(try Data(contentsOf: photo.fileURL!) as CFData, nil) != nil)

    let sourceAudioURL = source.appendingPathComponent("voice memo.m4a")
    try Data("audio-bytes".utf8).write(to: sourceAudioURL)
    let audio = CaptureAudio(filename: "voice memo.m4a", fileURL: sourceAudioURL, durationSeconds: 7)
    let persistedAudio = try store.persistAudio(audio, bucketID: "visit/one")

    #expect(persistedAudio.fileURL?.path.hasPrefix(temp.path) == true)
    #expect(persistedAudio.fileURL != sourceAudioURL)
    #expect(try Data(contentsOf: persistedAudio.fileURL!) == Data("audio-bytes".utf8))
}

@Test func localMediaStoreCanRemoveTemporaryAudioAfterCopy() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-audio-cleanup-\(UUID().uuidString)", isDirectory: true)
    let source = FileManager.default.temporaryDirectory.appendingPathComponent("btq-audio-cleanup-source-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: source, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: temp)
        try? FileManager.default.removeItem(at: source)
    }

    let sourceAudioURL = source.appendingPathComponent("voice memo.m4a")
    try Data("audio-bytes".utf8).write(to: sourceAudioURL)
    let store = LocalMediaStore(rootDirectory: temp)
    let audio = CaptureAudio(filename: "voice memo.m4a", fileURL: sourceAudioURL, durationSeconds: 7)

    let persistedAudio = try store.persistAudio(audio, bucketID: "visit-one", removeSourceAfterCopy: true)

    #expect(FileManager.default.fileExists(atPath: sourceAudioURL.path) == false)
    #expect(persistedAudio.fileURL?.path.hasPrefix(temp.path) == true)
    #expect(try Data(contentsOf: persistedAudio.fileURL!) == Data("audio-bytes".utf8))
}

@Test func localMediaStoreDeletesDiscardedPendingManagedMediaOnly() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-pending-media-\(UUID().uuidString)", isDirectory: true)
    let external = FileManager.default.temporaryDirectory.appendingPathComponent("btq-pending-media-external-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: external, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: temp)
        try? FileManager.default.removeItem(at: external)
    }

    let store = LocalMediaStore(rootDirectory: temp)
    let managedPhoto = try store.savePhotoData(makeTestImageData(type: .png), preferredStem: "pending", bucketID: "draft")
    let managedAudioURL = store.mediaDirectory(bucketID: "draft").appendingPathComponent("voice.m4a")
    try FileManager.default.createDirectory(at: managedAudioURL.deletingLastPathComponent(), withIntermediateDirectories: true)
    try Data("managed-audio".utf8).write(to: managedAudioURL)
    let managedAudio = CaptureAudio(filename: "voice.m4a", fileURL: managedAudioURL, durationSeconds: 3)

    let externalPhotoURL = external.appendingPathComponent("external.jpg")
    try Data("external-photo".utf8).write(to: externalPhotoURL)
    let externalAudioURL = external.appendingPathComponent("external.m4a")
    try Data("external-audio".utf8).write(to: externalAudioURL)

    store.deletePendingMedia(
        photos: [
            managedPhoto,
            CapturePhoto(filename: "external.jpg", fileURL: externalPhotoURL),
        ],
        audio: managedAudio
    )
    store.deletePendingMedia(
        photos: [],
        audio: CaptureAudio(filename: "external.m4a", fileURL: externalAudioURL, durationSeconds: 3)
    )

    #expect(FileManager.default.fileExists(atPath: managedPhoto.fileURL!.path) == false)
    #expect(FileManager.default.fileExists(atPath: managedAudioURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: externalPhotoURL.path) == true)
    #expect(FileManager.default.fileExists(atPath: externalAudioURL.path) == true)
}

@Test func imageNormalizerConvertsPngToJpeg() throws {
    let jpeg = try ImageNormalizer.normalizedData(from: makeTestImageData(type: .png), policy: .fieldCapture)
    let source = CGImageSourceCreateWithData(jpeg as CFData, nil)
    let type = source.flatMap { CGImageSourceGetType($0) as String? }

    #expect(type == UTType.jpeg.identifier)
}

@Test func imageNormalizerConvertsFileURLToJpeg() throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-image-url-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let sourceURL = temp.appendingPathComponent("picker-source.png")
    try makeTestImageData(type: .png).write(to: sourceURL)

    let jpeg = try ImageNormalizer.normalizedData(from: sourceURL, policy: .fieldCapture)
    let source = CGImageSourceCreateWithData(jpeg as CFData, nil)
    let type = source.flatMap { CGImageSourceGetType($0) as String? }

    #expect(type == UTType.jpeg.identifier)
}

@Test func imageNormalizerBakesExifOrientationIntoPixels() throws {
    let jpeg = try ImageNormalizer.normalizedData(
        from: makeTestImageData(type: .jpeg, width: 2, height: 1, orientation: 6),
        policy: .fieldCapture
    )
    let source = try #require(CGImageSourceCreateWithData(jpeg as CFData, nil))
    let properties = try #require(CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any])

    #expect(properties[kCGImagePropertyPixelWidth] as? Int == 1)
    #expect(properties[kCGImagePropertyPixelHeight] as? Int == 2)
    #expect((properties[kCGImagePropertyOrientation] as? Int ?? 1) == 1)
}

@Test func fieldCaptureImagePolicyUploadsBackendCompatibleJpegs() throws {
    let policy = ImageUploadPolicy.fieldCapture
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-image-policy-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let store = LocalMediaStore(rootDirectory: temp, imagePolicy: policy)
    let sourceURL = temp.appendingPathComponent("heic-source.png")
    try makeTestImageData(type: .png).write(to: sourceURL)
    let photo = try store.savePhotoFile(sourceURL, preferredStem: "heic-source", bucketID: "visit-one")

    #expect(policy.format == .jpeg)
    #expect(policy.format.fileExtension == "jpg")
    #expect(policy.format.mimeType == "image/jpeg")
    #expect(policy.maxPixelDimension == 2_048)
    #expect(photo.filename.hasSuffix(".jpg"))
    #expect(photo.mimeType == "image/jpeg")

    let source = CGImageSourceCreateWithData(try Data(contentsOf: photo.fileURL!) as CFData, nil)
    let type = source.flatMap { CGImageSourceGetType($0) as String? }
    #expect(type == UTType.jpeg.identifier)
}

@Test func voiceRecorderFormatsDurationsForFieldUi() {
    #expect(VoiceRecorder.formatDuration(0) == "0:00")
    #expect(VoiceRecorder.formatDuration(65.2) == "1:05")
    #expect(VoiceRecorder.formatDuration(599.6) == "10:00")
}

@Test func voiceRecordingInterruptionPolicyResumesOnlyWhenItPausedActiveRecording() {
    var policy = VoiceRecordingInterruptionPolicy()

    #expect(policy.interruptionBegan(isRecording: true, isPaused: false) == .pause)
    #expect(policy.interruptionEnded(shouldResume: true) == .resume)

    #expect(policy.interruptionEnded(shouldResume: true) == .remainPaused)
}

@Test func voiceRecordingInterruptionPolicyToleratesDuplicateBeganEvents() {
    var policy = VoiceRecordingInterruptionPolicy()

    #expect(policy.interruptionBegan(isRecording: true, isPaused: false) == .pause)
    #expect(policy.interruptionBegan(isRecording: true, isPaused: true) == .remainPaused)
    #expect(policy.interruptionEnded(shouldResume: true) == .resume)
}

@Test func voiceRecordingInterruptionPolicyKeepsUserPausedRecordingPaused() {
    var policy = VoiceRecordingInterruptionPolicy()

    #expect(policy.interruptionBegan(isRecording: true, isPaused: true) == .remainPaused)
    #expect(policy.interruptionEnded(shouldResume: true) == .remainPaused)
}

@Test func voiceRecordingInterruptionPolicyIgnoresIdleInterruptions() {
    var policy = VoiceRecordingInterruptionPolicy()

    #expect(policy.interruptionBegan(isRecording: false, isPaused: false) == .ignore)
    #expect(policy.interruptionEnded(shouldResume: true) == .remainPaused)
}

@Test @MainActor func voiceRecorderDeniedMicrophonePermissionDoesNotStartRecording() async {
    let permissionChecker = RecordingVoicePermissionChecker(status: .notDetermined, requestedStatus: .denied)
    let recorder = VoiceRecorder(permissionChecker: permissionChecker)

    await recorder.refreshPermissionStatus()
    #expect(recorder.permissionStatus == .notDetermined)

    await recorder.start()

    #expect(recorder.permissionStatus == .denied)
    #expect(recorder.isRecording == false)
    #expect(recorder.lastAudio == nil)
    #expect(recorder.errorMessage == "Microphone access is required for voice notes.")
    #expect(await permissionChecker.authorizationRequestCount == 1)
}

@Test func cameraCapturePermissionGateHandlesUnavailableDeniedAndGrantedStates() {
    #expect(
        CameraCapturePermissionGate.decision(status: .granted, cameraAvailable: false)
            == .showMessage(CameraCapturePermissionGate.unavailableMessage)
    )
    #expect(
        CameraCapturePermissionGate.decision(status: .denied, cameraAvailable: true)
            == .showMessage(CameraCapturePermissionGate.deniedMessage)
    )
    #expect(
        CameraCapturePermissionGate.decision(status: .restricted, cameraAvailable: true)
            == .showMessage(CameraCapturePermissionGate.restrictedMessage)
    )
    #expect(CameraCapturePermissionGate.decision(status: .notDetermined, cameraAvailable: true) == .requestPermission)
    #expect(CameraCapturePermissionGate.decision(status: .granted, cameraAvailable: true) == .presentCamera)
}

@Test func voiceRecorderSubscribesToSystemInterruptionsAndKeepsBackgroundPolicyExplicit() throws {
    let recorderSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Services/VoiceRecorder.swift"),
        encoding: .utf8
    )
    let cameraPermissionSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Services/CameraPermission.swift"),
        encoding: .utf8
    )
    let cameraViewSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CameraCaptureView.swift"),
        encoding: .utf8
    )
    let rootViewSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/BTQFieldCaptureRootView.swift"),
        encoding: .utf8
    )
    let captureViewSource = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Views/CaptureNotebookView.swift"),
        encoding: .utf8
    )

    #expect(recorderSource.contains("AVAudioSession.interruptionNotification"))
    #expect(recorderSource.contains("NotificationCenter.default.notifications"))
    #expect(recorderSource.contains("handleAudioSessionInterruption(notification)"))
    #expect(recorderSource.contains("interruptionTask?.cancel()"))
    #expect(recorderSource.contains("AVAudioApplication.shared.recordPermission"))
    #expect(recorderSource.contains("AVAudioApplication.requestRecordPermission"))
    #expect(recorderSource.contains("Microphone access is required for voice notes."))
    #expect(recorderSource.contains("setCategory(.playAndRecord"))
    #expect(recorderSource.contains("public private(set) var elapsedSeconds"))
    #expect(recorderSource.contains("private var durationTask"))
    #expect(recorderSource.contains("private var playbackObserver"))
    #expect(recorderSource.contains("VoicePlaybackObserver"))
    #expect(recorderSource.contains("AVAudioPlayerDelegate"))
    #expect(recorderSource.contains("audioPlayerDidFinishPlaying"))
    #expect(recorderSource.contains("audioPlayerDecodeErrorDidOccur"))
    #expect(recorderSource.contains("finishPlaybackIfCompleted()"))
    #expect(recorderSource.contains("finishPlayback(errorMessage:"))
    #expect(recorderSource.contains("startDurationTimer()"))
    #expect(recorderSource.contains("stopDurationTimer()"))
    #expect(recorderSource.contains("recorder.currentTime"))
    #expect(!captureViewSource.contains("AVAudioSession.interruptionNotification"))
    #expect(!captureViewSource.contains("handleAudioSessionInterruption(notification)"))
    #expect(captureViewSource.contains("Task { await recorder.start() }"))
    #expect(captureViewSource.contains("recorder.elapsedSeconds"))
    #expect(captureViewSource.contains("Voice memo paused \\(VoiceRecorder.formatDuration(recorder.elapsedSeconds))"))
    #expect(captureViewSource.contains("Recording voice memo \\(VoiceRecorder.formatDuration(recorder.elapsedSeconds))"))
    let draftValidationRange = try #require(captureViewSource.range(of: "model.validateQuickObservationDraft(photoCount: pendingPhotos.count, hasAudio: hasPendingAudio)"))
    let pendingAudioRange = try #require(captureViewSource.range(of: "let hadPendingAudio = hasPendingAudio"))
    let persistAudioRange = try #require(captureViewSource.range(of: "let audio = persistPendingAudio()"))
    #expect(draftValidationRange.lowerBound < persistAudioRange.lowerBound)
    #expect(draftValidationRange.lowerBound < pendingAudioRange.lowerBound)
    #expect(pendingAudioRange.lowerBound < persistAudioRange.lowerBound)
    #expect(captureViewSource.contains("guard !hadPendingAudio || audio != nil else"))
    #expect(captureViewSource.contains("Could not save voice memo. Try recording again."))
    #expect(captureViewSource.contains("private var hasPendingAudio"))
    #expect(cameraPermissionSource.contains("AVCaptureDevice.authorizationStatus(for: .video)"))
    #expect(cameraPermissionSource.contains("AVCaptureDevice.requestAccess(for: .video)"))
    #expect(cameraPermissionSource.contains(CameraCapturePermissionGate.deniedMessage))
    #expect(cameraPermissionSource.contains(CameraCapturePermissionGate.unavailableMessage))
    #expect(cameraViewSource.contains("public static var isCameraAvailable"))
    #expect(cameraViewSource.contains("controller.sourceType = .camera"))
    #expect(!cameraViewSource.contains("? .camera : .photoLibrary"))
    #expect(captureViewSource.contains("Task { await openCameraIfAllowed() }"))
    #expect(captureViewSource.contains("CameraCapturePermissionGate.decision"))
    #expect(captureViewSource.contains("@State private var cameraDraftContext: DraftContext?"))
    #expect(captureViewSource.contains("cameraDraftContext = currentDraftContext"))
    #expect(captureViewSource.contains("guard let context = cameraDraftContext, canAttachMedia(to: context) else { return }"))
    #expect(captureViewSource.contains("cameraDraftContext = nil"))
    #expect(captureViewSource.contains("private func canAttachMedia(to context: DraftContext) -> Bool"))
    #expect(captureViewSource.contains("private func isActiveDraftContext(_ context: DraftContext) -> Bool"))
    #expect(captureViewSource.contains("context == currentDraftContext && model.canSubmitCaptures"))
    #expect(captureViewSource.contains("@State private var isSavingDraft = false"))
    #expect(captureViewSource.contains("private var canEditDraft"))
    #expect(captureViewSource.contains("model.canSubmitCaptures && !isSavingDraft"))
    #expect(captureViewSource.contains("Task { await saveCurrentDraft() }"))
    #expect(captureViewSource.contains("guard !isSavingDraft else { return }"))
    #expect(captureViewSource.contains("defer { isSavingDraft = false }"))
    #expect(captureViewSource.contains("let savedPhotos = pendingPhotos"))
    #expect(captureViewSource.contains("pendingPhotos.removeAll"))
    #expect(captureViewSource.contains("Draft changed before save completed. Review and save again."))
    #expect(captureViewSource.contains("Button(\"Clear\")"))
    #expect(!captureViewSource.contains("Start Visit"))
    #expect(!captureViewSource.contains("End Visit"))
    #expect(captureViewSource.contains("private var sitePicker"))
    #expect(captureViewSource.contains("Picker(selection: siteSelection)"))
    #expect(captureViewSource.contains(".accessibilityIdentifier(\"capture.site.picker\")"))
    #expect(captureViewSource.contains(".accessibilityValue(selectedSiteLabel)"))
    #expect(captureViewSource.contains("Picker(selection: categorySelection)"))
    #expect(captureViewSource.contains("} label: {"))
    #expect(captureViewSource.contains(".contentShape(RoundedRectangle(cornerRadius: 8))"))
    #expect(captureViewSource.contains(".accessibilityValue(selectedCategoryLabel)"))
    #expect(captureViewSource.contains("Text(\"Select category...\").tag(Optional<String>.none)"))
    #expect(captureViewSource.contains("CapturePhotoThumbnail(photo: photo)"))
    #expect(captureViewSource.contains("AppVersionFooter()"))
    #expect(!captureViewSource.contains("Text(\"Visit Timeline\")"))
    #expect(captureViewSource.contains(".disabled(!canEditDraft)\n                .accessibilityLabel(\"Clear pending media\")"))
    #expect(captureViewSource.contains(".disabled(!canEditDraft)\n                            .accessibilityLabel(\"Photo note for"))
    #expect(captureViewSource.contains("discardPendingMedia()"))
    #expect(captureViewSource.contains("mediaStore.deletePendingMedia(photos: pendingPhotos, audio: recorder.lastAudio)"))
    #expect(captureViewSource.contains(".onChange(of: model.selectedSiteID)"))
    #expect(captureViewSource.contains("discardDraftAfterSiteChange()"))
    #expect(captureViewSource.contains("model.observationText = \"\""))
    #expect(captureViewSource.contains("Draft cleared after site change."))
    #expect(captureViewSource.contains(".onChange(of: model.account.id)"))
    #expect(captureViewSource.contains("discardDraftAfterAccountChange()"))
    #expect(captureViewSource.contains("Draft cleared after account change."))
    #expect(captureViewSource.contains("private struct DraftContext: Equatable"))
    #expect(captureViewSource.contains("DraftContext(accountID: model.account.id, siteID: model.selectedSiteID)"))
    #expect(captureViewSource.contains("Task { await loadPhotos(items, context: context) }"))
    #expect(captureViewSource.contains("guard canAttachMedia(to: context) else { return }"))
    #expect(captureViewSource.contains("guard canAttachMedia(to: context) else { break }"))
    #expect(captureViewSource.contains("defer { selectedPhotoItems = [] }"))
    #expect(captureViewSource.contains(".onChange(of: model.canSubmitCaptures)"))
    #expect(captureViewSource.contains("discardDraftAfterSubmitPermissionRevoked()"))
    #expect(captureViewSource.contains(".disabled(!canEditDraft || isAtPhotoLimit)"))
    #expect(captureViewSource.contains("VoiceRecorderView(recorder: recorder)"))
    #expect(captureViewSource.contains(".disabled(!canEditDraft)"))
    #expect(captureViewSource.contains("Draft cleared because this account cannot submit captures."))
    #expect(rootViewSource.contains("case .background:"))
    #expect(rootViewSource.contains("beginExpiringSyncIfNeeded(pendingCount: model.queueSummary.pending)"))
    #expect(rootViewSource.contains("await model.syncPending()"))
    #expect(!rootViewSource.contains("recorder.pause()"))
    #expect(!rootViewSource.contains("VoiceRecorder().pause()"))
}

@Test @MainActor func capturesOnlyAttachToActiveVisitForSameSite() async throws {
    let siteOne = BTQSite(siteID: "site_1", label: "Site One")
    let siteTwo = BTQSite(siteID: "site_2", label: "Site Two")
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [siteOne, siteTwo],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [siteOne, siteTwo]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    await model.startVisit(site: siteOne)
    let visitID = try #require(model.activeVisit(forSiteID: siteOne.siteID)?.id)
    model.selectedSite = siteTwo
    model.observationText = "Observation at a different site"

    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.captures.first?.siteID == siteTwo.siteID)
    #expect(model.captures.first?.visitID == nil)
    #expect(model.activeVisit(forSiteID: siteOne.siteID)?.id == visitID)
    #expect(model.activeVisit(forSiteID: siteTwo.siteID) == nil)

    await model.endVisit(site: siteOne)

    let timelineTitles = model.timeline.map(\.title)
    #expect(timelineTitles.contains("Visit started"))
    #expect(timelineTitles.contains("Visit ended"))
}

@Test @MainActor func savingObservationMarksSiteRecentlyUsed() async throws {
    let oldRecent = Date(timeIntervalSince1970: 1_000)
    let siteOne = BTQSite(siteID: "site_1", label: "Site One", lastUsedAt: oldRecent)
    let siteTwo = BTQSite(siteID: "site_2", label: "Site Two")
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [siteOne, siteTwo],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [siteOne, siteTwo]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    model.selectedSite = siteTwo
    model.observationText = "New observation"

    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.sites.first(where: { $0.siteID == siteTwo.siteID })?.lastUsedAt != nil)
    #expect(model.prioritizedSites.first?.siteID == siteTwo.siteID)
}

@Test @MainActor func loadRefreshesStoredTokenIntoLiveSession() async {
    let account = BTQAccount.defaultProduction
    let liveSession = BTQSession(
        person: BTQPerson(personID: "person_saved", name: "Saved Token User"),
        token: BTQToken(tokenID: "token_saved", label: "Saved Token"),
        sites: [BTQSite(siteID: "site_saved", label: "Saved Site")],
        canSubmit: true,
        canReview: false,
        maxImages: 4
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: FieldCaptureSnapshot(account: account)),
        apiClient: SequencedSessionAPIClient(sessions: [liveSession]),
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("stored-token", accountID: account.id)

    await model.load()

    #expect(model.session?.person.name == "Saved Token User")
    #expect(model.sites.map(\.siteID) == ["site_saved"])
    #expect(model.selectedSiteID == "site_saved")
    #expect(model.selectedCategoryValue == nil)
    #expect(model.maxImagesPerCapture == 100)
    #expect(model.isOfflineMode == false)
    #expect(model.statusMessage == "Session refreshed")
}

@Test @MainActor func categoryStartsEmptyAndDoesNotFallBackToFirstSiteCategory() async {
    let site = BTQSite(
        siteID: "site_1",
        label: "Site One",
        displayCategories: [
            BTQDisplayCategory(value: "cleaning_quality", label: "Cleaning quality"),
            BTQDisplayCategory(value: "supplies", label: "Supplies"),
        ]
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [site],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [site]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()
    model.observationText = "Uncategorized field note"

    let didSave = await model.saveQuickObservation()

    #expect(model.selectedCategoryValue == nil)
    #expect(didSave)
    #expect(model.captures.first?.qcCategory == "general_note")
}

@Test @MainActor func legacySessionPhotoLimitIsFlooredToNativeHundredPhotoCap() async {
    let site = BTQSite(siteID: "site_legacy", label: "Legacy Limit Site")
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [site],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [site]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()

    #expect(model.session?.maxImages == 6)
    #expect(model.maxImagesPerCapture == 100)
}

@Test @MainActor func operatorQCCategoryAutoSelectsWhenServerOffersQC() async {
    let site = BTQSite(
        siteID: "site_qc",
        label: "QC Site",
        displayCategories: [
            BTQDisplayCategory(value: "qc", label: "QC"),
            BTQDisplayCategory(value: "baseline", label: "Baseline"),
        ]
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_operator", name: "Operator"),
            token: BTQToken(tokenID: "token_operator", label: "Operator"),
            sites: [site],
            canSubmit: true,
            canReview: true,
            maxImages: 100
        ),
        sites: [site]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()
    model.observationText = "QC walk photos"

    let didSave = await model.saveQuickObservation(
        photos: (1...100).map { CapturePhoto(filename: "qc-\($0).jpg") }
    )

    #expect(model.maxImagesPerCapture == 100)
    #expect(model.selectedCategoryValue == "qc")
    #expect(didSave)
    #expect(model.captures.first?.qcCategory == "qc")
    #expect(model.captures.first?.photos.count == 100)
}

@Test @MainActor func siteSelectionAppliesQCDefaultOnlyWhenNewSiteOffersQC() async {
    let ordinarySite = BTQSite(
        siteID: "site_ordinary",
        label: "Ordinary Site",
        displayCategories: [
            BTQDisplayCategory(value: "cleaning_quality", label: "Cleaning quality"),
        ]
    )
    let qcSite = BTQSite(
        siteID: "site_qc",
        label: "QC Site",
        displayCategories: [
            BTQDisplayCategory(value: "qc", label: "QC"),
            BTQDisplayCategory(value: "baseline", label: "Baseline"),
        ]
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_operator", name: "Operator"),
            token: BTQToken(tokenID: "token_operator", label: "Operator"),
            sites: [ordinarySite, qcSite],
            canSubmit: true,
            canReview: true,
            maxImages: 100
        ),
        sites: [ordinarySite, qcSite]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()
    #expect(model.selectedSiteID == "site_ordinary")
    #expect(model.selectedCategoryValue == nil)

    model.selectSite(id: "site_qc")
    #expect(model.selectedCategoryValue == "qc")

    model.selectSite(id: "site_ordinary")
    #expect(model.selectedCategoryValue == nil)
}

@Test @MainActor func sessionRefreshPreservesValidSelectedCategory() async {
    let site = BTQSite(
        siteID: "site_1",
        label: "Site One",
        displayCategories: [
            BTQDisplayCategory(value: "general_note", label: "General"),
            BTQDisplayCategory(value: "supplies", label: "Supplies"),
        ]
    )
    let account = BTQAccount.defaultProduction
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: FieldCaptureSnapshot(account: account)),
        apiClient: SequencedSessionAPIClient(sessions: [
            BTQSession(
                person: BTQPerson(personID: "person_field", name: "Field User"),
                token: BTQToken(tokenID: "token_field", label: "Pilot"),
                sites: [site],
                canSubmit: true,
                canReview: false,
                maxImages: 6
            ),
            BTQSession(
                person: BTQPerson(personID: "person_field", name: "Field User"),
                token: BTQToken(tokenID: "token_field", label: "Pilot"),
                sites: [site],
                canSubmit: true,
                canReview: false,
                maxImages: 6
            ),
        ]),
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("stored-token", accountID: account.id)

    await model.load()
    model.selectedCategoryValue = "supplies"
    model.observationText = "Need paper towels"

    let didRefresh = await model.refreshSessionIfPossible()

    #expect(didRefresh)
    #expect(model.selectedSiteID == site.siteID)
    #expect(model.selectedCategoryValue == "supplies")
    #expect(model.observationText == "Need paper towels")
}

@Test @MainActor func startupLoadIsOneShotAndDoesNotResetLiveSelection() async {
    let siteOne = BTQSite(siteID: "site_1", label: "Site One")
    let siteTwo = BTQSite(siteID: "site_2", label: "Site Two")
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [siteOne, siteTwo],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [siteOne, siteTwo]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()
    #expect(model.hasLoaded)
    model.selectedSite = siteTwo
    model.observationText = "Live draft that should survive root task restart"

    await model.load()

    #expect(model.selectedSiteID == siteTwo.siteID)
    #expect(model.observationText == "Live draft that should survive root task restart")
}

@Test @MainActor func connectivityRecoverySyncsPendingCapture() async {
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await model.handleConnectivityChange(.unsatisfied)

    model.observationText = "Lobby needs towels"
    await model.saveQuickObservation()

    #expect(model.isOfflineMode)
    #expect(model.queueSummary.pending == 1)

    await tokenStore.saveToken("token-123", accountID: model.account.id)
    await model.handleConnectivityChange(.satisfied)

    let submitted = await apiClient.submitted
    #expect(model.isOfflineMode == false)
    #expect(model.queueSummary.done == 1)
    #expect(submitted.count == 1)
    #expect(submitted.first?.note == "Lobby needs towels")
}

@Test @MainActor func saveQuickObservationQueuesWithoutUploadWhenAlreadyOffline() async {
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-123", accountID: model.account.id)

    model.observationText = "Offline field note"
    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.isOfflineMode)
    #expect(model.queueSummary.pending == 1)
    #expect(model.statusMessage == "Saved offline. Captures will sync when connection returns.")
    #expect(await apiClient.submitted.isEmpty)
}

@Test @MainActor func saveQuickObservationDoesNotReportSuccessWhenPersistenceFails() async {
    let site = BTQSite(siteID: "site_1", label: "Site One")
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [site],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [site]
    )
    let model = FieldCaptureModel(
        store: FailingSaveFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    model.observationText = "Do not lose this note"

    let didSave = await model.saveQuickObservation()

    #expect(!didSave)
    #expect(model.captures.isEmpty)
    #expect(model.observationText == "Do not lose this note")
    #expect(model.sites.first?.lastUsedAt == nil)
    #expect(model.statusMessage == "Could not save locally. Try again.")
}

@Test @MainActor func saveQuickObservationRejectsMoreThanNativeHundredPhotoCap() async {
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 1
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    let didSave = await model.saveQuickObservation(
        photos: (1...101).map { CapturePhoto(filename: "photo-\($0).jpg") }
    )

    #expect(didSave == false)
    #expect(model.captures.isEmpty)
    #expect(model.statusMessage == "Limit is 100 photos per capture.")
}

@Test @MainActor func quickObservationDraftPreflightMatchesSaveValidation() async {
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 1
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    #expect(model.validateQuickObservationDraft(photoCount: 0, hasAudio: false) == false)
    #expect(model.statusMessage == "Add a note, photo, or voice memo.")
    #expect(model.validateQuickObservationDraft(photoCount: 0, hasAudio: true))
    #expect(model.validateQuickObservationDraft(photoCount: 100, hasAudio: true))
    #expect(model.validateQuickObservationDraft(photoCount: 101, hasAudio: true) == false)
    #expect(model.statusMessage == "Limit is 100 photos per capture.")
}

@Test @MainActor func saveQuickObservationRejectsViewOnlyToken() async {
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: BTQSession(
            person: BTQPerson(personID: "person_view", name: "View Only"),
            token: BTQToken(tokenID: "token_view", label: "Viewer"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: false,
            canReview: true,
            maxImages: 6
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    model.observationText = "Should not save"

    let didSave = await model.saveQuickObservation()

    #expect(didSave == false)
    #expect(model.canSubmitCaptures == false)
    #expect(model.captures.isEmpty)
    #expect(model.statusMessage == "This account cannot submit captures.")
}

@Test @MainActor func syncPendingDoesNotUploadForViewOnlyToken() async {
    let account = BTQAccount.defaultProduction
    let capture = LocalCapture(
        captureID: "capture-view-only",
        jobID: "job-view-only",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Already pending",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let snapshot = FieldCaptureSnapshot(
        account: account,
        session: BTQSession(
            person: BTQPerson(personID: "person_view", name: "View Only"),
            token: BTQToken(tokenID: "token_view", label: "Viewer"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: false,
            canReview: true,
            maxImages: 6
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")],
        captures: [capture]
    )
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-view", accountID: account.id)

    await model.syncPending()

    #expect(model.captures.first?.status == .pending)
    #expect(model.captures.first?.attempts == 0)
    #expect(model.statusMessage == "This account cannot submit captures.")
    #expect(await apiClient.submitted.isEmpty)
}

@Test @MainActor func syncPendingFailsMissingPhotoWithoutNetworkAttempt() async {
    let account = BTQAccount.defaultProduction
    let missingPhotoURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-missing-photo-\(UUID().uuidString).jpg")
    let capture = LocalCapture(
        captureID: "capture-missing-photo",
        jobID: "job-missing-photo",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Missing photo",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [CapturePhoto(filename: "missing.jpg", fileURL: missingPhotoURL)]
    )
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.captures.first?.status == .failed)
    #expect(model.captures.first?.attempts == 0)
    #expect(model.captures.first?.lastError == "Missing photo file: missing.jpg")
    #expect(model.statusMessage == "Capture failed: Missing photo file: missing.jpg")
    #expect(await apiClient.submitted.isEmpty)
}

@Test @MainActor func syncPendingFailsMissingAudioWithoutNetworkAttempt() async {
    let account = BTQAccount.defaultProduction
    let missingAudioURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-missing-audio-\(UUID().uuidString).m4a")
    let capture = LocalCapture(
        captureID: "capture-missing-audio",
        jobID: "job-missing-audio",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Missing audio",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        audio: CaptureAudio(filename: "missing.m4a", fileURL: missingAudioURL, durationSeconds: 4)
    )
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.captures.first?.status == .failed)
    #expect(model.captures.first?.attempts == 0)
    #expect(model.captures.first?.lastError == "Missing audio file: missing.m4a")
    #expect(model.statusMessage == "Capture failed: Missing audio file: missing.m4a")
    #expect(await apiClient.submitted.isEmpty)
}

@Test @MainActor func syncCompleteNotificationRequiresCleanSuccessfulUpload() async {
    let account = BTQAccount.defaultProduction
    let capture = LocalCapture(
        captureID: "capture-notify-success",
        jobID: "job-notify-success",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Notify when clean",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let notificationScheduler = RecordingUploadNotificationScheduler()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: tokenStore,
        notificationScheduler: notificationScheduler
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.queueSummary.done == 1)
    #expect(await notificationScheduler.syncRecoveredPendingCounts == [0])
    #expect(await notificationScheduler.failedUploads.isEmpty)
}

@Test @MainActor func notificationPermissionRequestUpdatesModelStatus() async {
    let notificationScheduler = RecordingUploadNotificationScheduler(status: .notDetermined, requestedStatus: .authorized)
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: notificationScheduler
    )

    await model.refreshNotificationPermissionStatus()
    #expect(model.notificationPermissionStatus == .notDetermined)

    await model.requestNotificationPermission()

    #expect(model.notificationPermissionStatus == .authorized)
    #expect(model.statusMessage == "Sync alerts enabled.")
    #expect(await notificationScheduler.authorizationRequestCount == 1)
}

@Test @MainActor func testNotificationRequiresAndUsesPermission() async {
    let disabledScheduler = RecordingUploadNotificationScheduler(status: .notDetermined)
    let disabledModel = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: disabledScheduler
    )

    await disabledModel.sendTestNotification()

    #expect(disabledModel.statusMessage == "Enable sync alerts before sending a test.")
    #expect(await disabledScheduler.testAlertCount == 0)

    let enabledScheduler = RecordingUploadNotificationScheduler(status: .authorized)
    let enabledModel = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: enabledScheduler
    )

    await enabledModel.sendTestNotification()

    #expect(enabledModel.statusMessage == "Test sync alert sent.")
    #expect(await enabledScheduler.testAlertCount == 1)
}

@Test @MainActor func uploadFailureTestNotificationRequiresAndUsesPermission() async {
    let disabledScheduler = RecordingUploadNotificationScheduler(status: .notDetermined)
    let disabledModel = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: disabledScheduler
    )

    await disabledModel.sendTestUploadFailureNotification()

    #expect(disabledModel.statusMessage == "Enable sync alerts before sending a failure test.")
    #expect(await disabledScheduler.failedUploads.isEmpty)

    let enabledScheduler = RecordingUploadNotificationScheduler(status: .authorized)
    let enabledModel = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: enabledScheduler
    )
    await enabledModel.load()

    await enabledModel.sendTestUploadFailureNotification()

    #expect(enabledModel.statusMessage == "Test upload failure alert sent.")
    #expect(await enabledScheduler.failedUploads.count == 1)
    #expect(await enabledScheduler.failedUploads.first?.captureID.hasPrefix("test-upload-failure-") == true)
    #expect(await enabledScheduler.failedUploads.first?.siteLabel == "Sandy Sandbox")
    #expect(await enabledScheduler.failedUploads.first?.reason == "Test upload failure alert from Settings.")
}

@Test @MainActor func syncCompleteNotificationDoesNotFireWhenPendingBecomesFailed() async {
    let account = BTQAccount.defaultProduction
    let missingPhotoURL = FileManager.default.temporaryDirectory
        .appendingPathComponent("btq-notify-missing-photo-\(UUID().uuidString).jpg")
    let capture = LocalCapture(
        captureID: "capture-notify-missing-photo",
        jobID: "job-notify-missing-photo",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Do not notify complete",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [CapturePhoto(filename: "missing.jpg", fileURL: missingPhotoURL)]
    )
    let notificationScheduler = RecordingUploadNotificationScheduler()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: tokenStore,
        notificationScheduler: notificationScheduler
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.queueSummary.failed == 1)
    #expect(await notificationScheduler.syncRecoveredPendingCounts.isEmpty)
    #expect(await notificationScheduler.failedUploads == [
        RecordingUploadFailure(captureID: "capture-notify-missing-photo", reason: "Missing photo file: missing.jpg", siteLabel: "Site One")
    ])
}

@Test @MainActor func successfulSyncReleasesManagedMediaFiles() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-sync-media-release-\(UUID().uuidString)", isDirectory: true)
    let externalRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-sync-media-external-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: externalRoot, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: mediaRoot)
        try? FileManager.default.removeItem(at: externalRoot)
    }

    let account = BTQAccount.defaultProduction
    let mediaStore = LocalMediaStore(rootDirectory: mediaRoot)
    let bucketURL = mediaStore.mediaDirectory(bucketID: "capture-release")
    try FileManager.default.createDirectory(at: bucketURL, withIntermediateDirectories: true)
    let managedPhotoURL = bucketURL.appendingPathComponent("photo.jpg")
    let managedAudioURL = bucketURL.appendingPathComponent("voice.m4a")
    let externalPhotoURL = externalRoot.appendingPathComponent("external.jpg")
    try Data("managed-photo".utf8).write(to: managedPhotoURL)
    try Data("managed-audio".utf8).write(to: managedAudioURL)
    try Data("external-photo".utf8).write(to: externalPhotoURL)

    let capture = LocalCapture(
        captureID: "capture-release",
        jobID: "job-release",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Release media after upload",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [
            CapturePhoto(filename: "photo.jpg", fileURL: managedPhotoURL),
            CapturePhoto(filename: "external.jpg", fileURL: externalPhotoURL),
        ],
        audio: CaptureAudio(filename: "voice.m4a", fileURL: managedAudioURL, durationSeconds: 4)
    )
    let apiClient = MockCaptureAPIClient()
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: mediaStore
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    let submitted = await apiClient.submitted.first
    #expect(submitted?.photos.first?.fileURL == managedPhotoURL)
    #expect(submitted?.audio?.fileURL == managedAudioURL)
    #expect(model.captures.first?.status == .done)
    #expect(model.captures.first?.photos[0].fileURL == nil)
    #expect(model.captures.first?.photos[1].fileURL == externalPhotoURL)
    #expect(model.captures.first?.audio?.fileURL == nil)
    #expect(FileManager.default.fileExists(atPath: managedPhotoURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: managedAudioURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: externalPhotoURL.path) == true)
}

@Test @MainActor func syncFailureUsesNativePhotoLimitMessageForBackendPhotoLimitRejection() async {
    let apiClient = FailingSubmitAPIClient(
        error: CaptureAPIError.serverStatus(
            status: 400,
            code: "too_many_images",
            message: "At most 6 images may be submitted"
        )
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-123", accountID: model.account.id)
    await model.handleConnectivityChange(.satisfied)

    model.observationText = "Backend rejection test"
    let didSave = await model.saveQuickObservation()

    #expect(didSave)
    #expect(model.captures.first?.status == .failed)
    #expect(model.captures.first?.lastError == "Limit is 100 photos per capture.")
    #expect(model.statusMessage == "Capture failed: Limit is 100 photos per capture.")
    #expect(model.captures.first.map { model.displayError(for: $0) } == "Limit is 100 photos per capture.")
}

@Test @MainActor func unauthorizedSyncRequiresReconnectAndPreservesPendingCapture() async {
    let account = BTQAccount.defaultProduction
    let capture = LocalCapture(
        captureID: "capture-unauthorized",
        jobID: "job-unauthorized",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Keep this pending",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let apiClient = FailingSubmitAPIClient(error: CaptureAPIError.unauthorized)
    let tokenStore = MemoryTokenStore()
    let notificationScheduler = RecordingUploadNotificationScheduler()
    let store = MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [capture]))
    let model = FieldCaptureModel(
        store: store,
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: notificationScheduler
    )
    await model.load()
    await tokenStore.saveToken("revoked-token", accountID: account.id)

    await model.syncPending()

    #expect(model.requiresReconnect)
    #expect(model.canSubmitCaptures == false)
    #expect(model.session == nil)
    #expect(model.captures.first?.status == .pending)
    #expect(model.captures.first?.lastError == "Token is invalid, expired, or revoked")
    #expect(model.statusMessage == "Token expired or revoked. Reconnect this account to sync.")
    #expect(await tokenStore.loadToken(accountID: account.id) == nil)
    #expect(await notificationScheduler.failedUploads.isEmpty)

    let reloadedModel = FieldCaptureModel(
        store: store,
        apiClient: MockCaptureAPIClient(),
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await reloadedModel.load()

    #expect(reloadedModel.session == nil)
    #expect(reloadedModel.canSubmitCaptures == false)
    #expect(reloadedModel.captures.first?.status == .pending)
}

@Test @MainActor func permanentCaptureFailureDoesNotBlockLaterPendingUploads() async {
    let account = BTQAccount.defaultProduction
    let rejected = LocalCapture(
        captureID: "capture-rejected",
        jobID: "job-rejected",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Rejected capture",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let valid = LocalCapture(
        captureID: "capture-valid",
        jobID: "job-valid",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Valid capture",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_002),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_003)
    )
    let apiClient = FlakySubmitAPIClient(errors: [
        CaptureAPIError.serverStatus(
            status: 400,
            code: "invalid_capture",
            message: "Capture is not valid for this site"
        ),
    ])
    let tokenStore = MemoryTokenStore()
    let notificationScheduler = RecordingUploadNotificationScheduler()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [rejected, valid])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: notificationScheduler
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.captures.first(where: { $0.captureID == "capture-rejected" })?.status == .failed)
    #expect(model.captures.first(where: { $0.captureID == "capture-rejected" })?.lastError == "Capture is not valid for this site")
    #expect(model.captures.first(where: { $0.captureID == "capture-valid" })?.status == .done)
    #expect(model.queueSummary.failed == 1)
    #expect(model.queueSummary.done == 1)
    #expect(await apiClient.submittedCount == 1)
    #expect(await apiClient.submittedCaptureIDs == ["capture-valid"])
    #expect(await notificationScheduler.failedUploads == [
        RecordingUploadFailure(captureID: "capture-rejected", reason: "Capture is not valid for this site", siteLabel: "Site One")
    ])
    #expect(await notificationScheduler.syncRecoveredPendingCounts.isEmpty)
}

@Test @MainActor func transientCaptureFailureDoesNotBlockLaterPendingUploads() async {
    let account = BTQAccount.defaultProduction
    let transient = LocalCapture(
        captureID: "capture-transient",
        jobID: "job-transient",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Temporary transport failure",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let later = LocalCapture(
        captureID: "capture-later",
        jobID: "job-later",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Should still upload",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_002),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_003)
    )
    let apiClient = FlakySubmitAPIClient(errors: [URLError(.timedOut)])
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [transient, later])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    let transientCapture = model.captures.first { $0.captureID == "capture-transient" }
    let laterCapture = model.captures.first { $0.captureID == "capture-later" }
    #expect(transientCapture?.status == .pending)
    #expect(transientCapture?.attempts == 1)
    #expect(transientCapture?.retryAfter != nil)
    #expect(laterCapture?.status == .done)
    #expect(await apiClient.submittedCaptureIDs == ["capture-later"])
}

@Test @MainActor func syncUsesCaptureIDAfterUploadWhenQueueMutatesDuringAwait() async {
    let account = BTQAccount.defaultProduction
    let first = LocalCapture(
        captureID: "capture-first",
        jobID: "job-first",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "First capture",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001)
    )
    let second = LocalCapture(
        captureID: "capture-second",
        jobID: "job-second",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Second capture",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_002),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_003)
    )
    let tokenStore = MemoryTokenStore()
    let modelBox = FieldCaptureModelBox()
    let apiClient = ReentrantSubmitAPIClient { capture in
        if capture.captureID == "capture-second" {
            await modelBox.model?.deleteCapture("capture-first")
        }
    }
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [first, second])),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    modelBox.model = model
    await model.load()
    await tokenStore.saveToken("token-submit", accountID: account.id)

    await model.syncPending()

    #expect(model.captures.map(\.captureID) == ["capture-second"])
    #expect(model.captures.first?.status == .done)
    #expect(model.statusMessage == "Synced Site One")
    #expect(await apiClient.submittedCaptureIDs == ["capture-first", "capture-second"])
}

@Test @MainActor func uploadingCaptureCannotBeDeleted() async {
    let account = BTQAccount.defaultProduction
    let uploading = LocalCapture(
        captureID: "capture-uploading",
        jobID: "job-uploading",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Uploading",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        status: .uploading
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(account: account, captures: [uploading])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    await model.deleteCapture("capture-uploading")

    #expect(model.captures.map(\.captureID) == ["capture-uploading"])
    #expect(model.captures.first?.status == .uploading)
    #expect(model.statusMessage == "Capture is uploading and cannot be removed yet.")
}

@Test @MainActor func interruptedUploadingCaptureRecoversToPendingOnLoad() async {
    let staleUpload = LocalCapture(
        captureID: "capture-interrupted-upload",
        jobID: "job-interrupted-upload",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Recovered after app suspension",
        capturedAt: Date(timeIntervalSinceNow: -600),
        exportedAt: Date(timeIntervalSinceNow: -600),
        status: .uploading,
        lastTriedAt: Date(timeIntervalSinceNow: -300)
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(captures: [staleUpload])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()

    #expect(model.captures.first?.status == .pending)
    #expect(model.captures.first?.lastError == "Upload was interrupted. It will retry automatically.")
    #expect(model.captures.first?.retryAfter != nil)
}

@Test func resumeOnlineWorkRunsInterruptedUploadRecoveryBeforeSync() throws {
    let source = try String(
        contentsOf: packageRoot().appendingPathComponent("Sources/BTQFieldCaptureApp/Stores/FieldCaptureModel.swift"),
        encoding: .utf8
    )
    let resumeRange = try #require(source.range(of: "public func resumeOnlineWork() async"))
    let sessionRefreshRange = try #require(source.range(of: "guard await refreshSessionIfPossible() else { return }", range: resumeRange.lowerBound..<source.endIndex))
    let recoveryRange = try #require(source.range(of: "recoverInterruptedUploads()", range: resumeRange.lowerBound..<sessionRefreshRange.lowerBound))

    #expect(recoveryRange.lowerBound > resumeRange.lowerBound)
    #expect(recoveryRange.lowerBound < sessionRefreshRange.lowerBound)
}

@Test @MainActor func retryFailedCaptureRequeuesAndSyncs() async throws {
    let apiClient = FlakySubmitAPIClient(errors: [
        CaptureAPIError.serverStatus(
            status: 400,
            code: "temporary_backend_rejection",
            message: "Operator fixed the site assignment"
        ),
    ])
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await tokenStore.saveToken("token-123", accountID: model.account.id)
    await model.handleConnectivityChange(.satisfied)

    model.observationText = "Retry me"
    let didSave = await model.saveQuickObservation()
    let captureID = model.captures.first?.captureID

    #expect(didSave)
    #expect(model.captures.first?.status == .failed)
    #expect(model.captures.first?.lastError == "Operator fixed the site assignment")

    await model.retryCapture(try #require(captureID))

    #expect(model.captures.first?.status == .done)
    #expect(model.captures.first?.lastError == nil)
    #expect(model.queueSummary.done == 1)
    #expect(await apiClient.submittedCount == 1)
}

@Test @MainActor func retryFailedCaptureIsBlockedDuringActiveSync() async {
    let failed = LocalCapture(
        captureID: "capture-failed",
        jobID: "job-failed",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Retry me later",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        attempts: 1,
        lastError: "Previous failure"
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(captures: [failed])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    model.isSyncing = true
    await model.retryCapture("capture-failed")

    #expect(model.captures.first?.status == .failed)
    #expect(model.captures.first?.lastError == "Previous failure")
    #expect(model.captures.first?.retryAfter == nil)
    #expect(model.statusMessage == "Wait for sync to finish before retrying.")
}

@Test func failedCaptureRecoveryHintsExplainFieldActions() {
    let missingPhoto = LocalCapture(
        captureID: "capture-missing-photo",
        jobID: "job-missing-photo",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Missing photo",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        lastError: "Missing photo file: missing.jpg"
    )
    #expect(missingPhoto.failureRecoveryHint == "Delete this local capture and capture it again; the saved media file is no longer on this device.")

    let tooManyPhotos = LocalCapture(
        captureID: "capture-too-many",
        jobID: "job-too-many",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Too many photos",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        lastError: "At most one photo may be submitted"
    )
    #expect(tooManyPhotos.failureRecoveryHint == "Delete and resave this capture with fewer photos.")

    let backendIssue = LocalCapture(
        captureID: "capture-backend",
        jobID: "job-backend",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Backend issue",
        capturedAt: .now,
        exportedAt: .now,
        status: .failed,
        lastError: "Operator fixed the site assignment"
    )
    #expect(backendIssue.failureRecoveryHint == "Retry after the issue is fixed, or delete and capture it again.")

    var pending = backendIssue
    pending.status = .pending
    #expect(pending.failureRecoveryHint == nil)
}

@Test @MainActor func storedQueuePhotoLimitErrorsDisplayNativeLimitMessage() async {
    let capture = LocalCapture(
        captureID: "capture-stored-photo-limit",
        jobID: "job-stored-photo-limit",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Stored failure",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        status: .failed,
        lastError: "At most 6 images may be submitted"
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: captureSnapshot(captures: [capture])),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )

    await model.load()

    #expect(model.captures.first?.lastError == "At most 6 images may be submitted")
    #expect(model.captures.first.map { model.displayError(for: $0) } == "Limit is 100 photos per capture.")
}

@Test @MainActor func modelCanConnectAndSwitchBetweenCachedAccounts() async {
    let apiClient = SequencedSessionAPIClient(sessions: [
        BTQSession(
            person: BTQPerson(personID: "person_alpha", name: "Alpha User"),
            token: BTQToken(tokenID: "token_alpha", label: "Alpha", role: "cleaner"),
            sites: [BTQSite(siteID: "site_alpha", label: "Alpha Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        BTQSession(
            person: BTQPerson(personID: "person_beta", name: "Beta User"),
            token: BTQToken(tokenID: "token_beta", label: "Beta", role: "site_admin"),
            sites: [BTQSite(siteID: "site_beta", label: "Beta Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
    ])
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    let didConnectAlpha = await model.connect(token: "alpha")
    let alphaAccountID = model.account.id

    #expect(didConnectAlpha)
    #expect(model.account.personName == "Alpha User")
    #expect(model.account.tokenRole == "cleaner")
    #expect(model.sites.map(\.siteID) == ["site_alpha"])

    model.observationText = "Draft should not cross accounts"
    let didConnectBeta = await model.connect(token: "beta")
    let betaAccountID = model.account.id

    #expect(didConnectBeta)
    #expect(betaAccountID != alphaAccountID)
    #expect(model.accounts.count == 2)
    #expect(model.account.personName == "Beta User")
    #expect(model.account.tokenRole == "site_admin")
    #expect(model.sites.map(\.siteID) == ["site_beta"])
    #expect(model.observationText.isEmpty)

    model.observationText = "Another cross-account draft"
    await model.switchAccount(alphaAccountID)

    #expect(model.account.id == alphaAccountID)
    #expect(model.account.personName == "Alpha User")
    #expect(model.account.tokenRole == "cleaner")
    #expect(model.sites.map(\.siteID) == ["site_alpha"])
    #expect(model.observationText.isEmpty)
}

@Test @MainActor func invalidConnectReturnsFalseWithoutReplacingCachedSession() async {
    let apiClient = OneGoodSessionThenUnauthorizedAPIClient(session:
        BTQSession(
            person: BTQPerson(personID: "person_alpha", name: "Alpha User"),
            token: BTQToken(tokenID: "token_alpha", label: "Alpha"),
            sites: [BTQSite(siteID: "site_alpha", label: "Alpha Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        )
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    let didConnect = await model.connect(token: "alpha")
    let didReconnect = await model.connect(token: "invalid")
    let didParseBadLink = await model.connectWithOnboardingURL(URL(string: "https://fc.gregstoltz.com/onboard/")!)

    #expect(didConnect)
    #expect(!didReconnect)
    #expect(!didParseBadLink)
    #expect(model.account.personName == "Alpha User")
    #expect(model.sites.map(\.siteID) == ["site_alpha"])
    #expect(model.statusMessage == "No token found in link.")
}

@Test @MainActor func connectReturnsFalseWhileAlreadyConnectingWithoutCallingAPI() async {
    let apiClient = RecordingSessionAPIClient()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    model.isConnecting = true
    let didConnect = await model.connect(token: "token-123")

    #expect(!didConnect)
    #expect(model.isConnecting)
    #expect(await apiClient.requestedTokens.isEmpty)
}

@Test @MainActor func connectCanProceedWhileStartupLoadIsActive() async {
    let apiClient = RecordingSessionAPIClient()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    model.isLoading = true
    let didConnect = await model.connect(token: "token-123")

    #expect(didConnect)
    #expect(model.isLoading)
    #expect(!model.isConnecting)
    #expect(await apiClient.requestedTokens == ["token-123"])
}

@Test @MainActor func validConnectIsNotRejectedWhenLocalSnapshotSaveFails() async {
    let liveSession = BTQSession(
        person: BTQPerson(personID: "person_review", name: "App Store Review"),
        token: BTQToken(tokenID: "token_review", label: "Review"),
        sites: [BTQSite(siteID: "SANDBOX", label: "Sandbox Site")],
        canSubmit: true,
        canReview: false,
        maxImages: 6
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: FailingSaveFieldCaptureStore(snapshot: FieldCaptureSnapshot(account: .defaultProduction)),
        apiClient: SequencedSessionAPIClient(sessions: [liveSession]),
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()

    let didConnect = await model.connect(token: "review-token")

    #expect(didConnect)
    #expect(model.needsInitialSetup == false)
    #expect(model.account.personName == "App Store Review")
    #expect(model.sites.map(\.siteID) == ["SANDBOX"])
    #expect(model.statusMessage == "Ready for App Store Review. Local cache could not be saved.")
    #expect(await tokenStore.loadToken(accountID: model.account.id) == "review-token")
}

@Test @MainActor func removingActiveAccountDeletesTokenAndSwitchesToFallbackAccount() async {
    let apiClient = SequencedSessionAPIClient(sessions: [
        BTQSession(
            person: BTQPerson(personID: "person_alpha", name: "Alpha User"),
            token: BTQToken(tokenID: "token_alpha", label: "Alpha"),
            sites: [BTQSite(siteID: "site_alpha", label: "Alpha Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        BTQSession(
            person: BTQPerson(personID: "person_beta", name: "Beta User"),
            token: BTQToken(tokenID: "token_beta", label: "Beta"),
            sites: [BTQSite(siteID: "site_beta", label: "Beta Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
    ])
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await model.connect(token: "alpha-token")
    let alphaAccountID = model.account.id
    await model.connect(token: "beta-token")
    let betaAccountID = model.account.id

    model.observationText = "Remove should clear this draft"
    await model.removeAccount(betaAccountID)

    #expect(model.account.id == alphaAccountID)
    #expect(model.accounts.count == 1)
    #expect(model.account.personName == "Alpha User")
    #expect(model.observationText.isEmpty)
    #expect(await tokenStore.loadToken(accountID: betaAccountID) == nil)
    #expect(await tokenStore.loadToken(accountID: alphaAccountID) == "alpha-token")
}

@Test @MainActor func accountSwitchAndRemovalAreBlockedDuringSync() async {
    let apiClient = SequencedSessionAPIClient(sessions: [
        BTQSession(
            person: BTQPerson(personID: "person_alpha", name: "Alpha User"),
            token: BTQToken(tokenID: "token_alpha", label: "Alpha"),
            sites: [BTQSite(siteID: "site_alpha", label: "Alpha Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        BTQSession(
            person: BTQPerson(personID: "person_beta", name: "Beta User"),
            token: BTQToken(tokenID: "token_beta", label: "Beta"),
            sites: [BTQSite(siteID: "site_beta", label: "Beta Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
    ])
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await model.connect(token: "alpha-token")
    let alphaAccountID = model.account.id
    await model.connect(token: "beta-token")
    let betaAccountID = model.account.id

    model.isSyncing = true
    await model.switchAccount(alphaAccountID)

    #expect(model.account.id == betaAccountID)
    #expect(model.statusMessage == "Wait for sync to finish before switching accounts.")

    await model.removeAccount(betaAccountID)

    #expect(model.account.id == betaAccountID)
    #expect(model.accounts.count == 2)
    #expect(model.statusMessage == "Wait for sync to finish before removing accounts.")
}

@Test @MainActor func accountSwitchAndRemovalAreBlockedDuringConnect() async {
    let apiClient = SequencedSessionAPIClient(sessions: [
        BTQSession(
            person: BTQPerson(personID: "person_alpha", name: "Alpha User"),
            token: BTQToken(tokenID: "token_alpha", label: "Alpha"),
            sites: [BTQSite(siteID: "site_alpha", label: "Alpha Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        BTQSession(
            person: BTQPerson(personID: "person_beta", name: "Beta User"),
            token: BTQToken(tokenID: "token_beta", label: "Beta"),
            sites: [BTQSite(siteID: "site_beta", label: "Beta Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
    ])
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await model.connect(token: "alpha-token")
    let alphaAccountID = model.account.id
    await model.connect(token: "beta-token")
    let betaAccountID = model.account.id

    model.isConnecting = true
    await model.switchAccount(alphaAccountID)

    #expect(model.account.id == betaAccountID)
    #expect(model.statusMessage == "Wait for connect to finish before switching accounts.")

    await model.removeAccount(betaAccountID)

    #expect(model.account.id == betaAccountID)
    #expect(model.accounts.count == 2)
    #expect(model.statusMessage == "Wait for connect to finish before removing accounts.")
}

@Test @MainActor func removingAccountDeletesOnlyManagedMediaFiles() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-managed-media-\(UUID().uuidString)", isDirectory: true)
    let externalRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-external-media-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: externalRoot, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: mediaRoot)
        try? FileManager.default.removeItem(at: externalRoot)
    }

    let mediaStore = LocalMediaStore(rootDirectory: mediaRoot)
    let bucketURL = mediaStore.mediaDirectory(bucketID: "capture-one")
    try FileManager.default.createDirectory(at: bucketURL, withIntermediateDirectories: true)
    let managedPhotoURL = bucketURL.appendingPathComponent("photo.jpg")
    let managedAudioURL = bucketURL.appendingPathComponent("voice.m4a")
    let externalPhotoURL = externalRoot.appendingPathComponent("external-photo.jpg")
    try Data("managed-photo".utf8).write(to: managedPhotoURL)
    try Data("managed-audio".utf8).write(to: managedAudioURL)
    try Data("external-photo".utf8).write(to: externalPhotoURL)

    let account = BTQAccount(
        label: "Field User",
        baseURL: URL(string: "https://example.test")!,
        tokenID: "token_media",
        personID: "person_media",
        personName: "Field User"
    )
    let capture = LocalCapture(
        captureID: "capture-one",
        jobID: "job-one",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Remove me",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [
            CapturePhoto(filename: "photo.jpg", fileURL: managedPhotoURL),
            CapturePhoto(filename: "external-photo.jpg", fileURL: externalPhotoURL),
        ],
        audio: CaptureAudio(filename: "voice.m4a", fileURL: managedAudioURL, durationSeconds: 5)
    )
    let snapshot = FieldCaptureSnapshot(
        account: account,
        session: BTQSession(
            person: BTQPerson(personID: "person_media", name: "Field User"),
            token: BTQToken(tokenID: "token_media", label: "Field"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")],
        captures: [capture]
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: mediaStore
    )
    await tokenStore.saveToken("token-123", accountID: account.id)
    await model.load()

    await model.removeAccount(account.id)

    #expect(FileManager.default.fileExists(atPath: managedPhotoURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: managedAudioURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: externalPhotoURL.path) == true)
    #expect(await tokenStore.loadToken(accountID: account.id) == nil)
    #expect(model.accounts.count == 1)
}

@Test @MainActor func deletingCaptureRemovesManagedMediaOnly() async throws {
    let mediaRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-delete-capture-media-\(UUID().uuidString)", isDirectory: true)
    let externalRoot = FileManager.default.temporaryDirectory.appendingPathComponent("btq-delete-capture-external-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: mediaRoot, withIntermediateDirectories: true)
    try FileManager.default.createDirectory(at: externalRoot, withIntermediateDirectories: true)
    defer {
        try? FileManager.default.removeItem(at: mediaRoot)
        try? FileManager.default.removeItem(at: externalRoot)
    }

    let mediaStore = LocalMediaStore(rootDirectory: mediaRoot)
    let bucketURL = mediaStore.mediaDirectory(bucketID: "capture-delete")
    try FileManager.default.createDirectory(at: bucketURL, withIntermediateDirectories: true)
    let managedPhotoURL = bucketURL.appendingPathComponent("photo.jpg")
    let managedAudioURL = bucketURL.appendingPathComponent("voice.m4a")
    let externalPhotoURL = externalRoot.appendingPathComponent("external-photo.jpg")
    try Data("managed-photo".utf8).write(to: managedPhotoURL)
    try Data("managed-audio".utf8).write(to: managedAudioURL)
    try Data("external-photo".utf8).write(to: externalPhotoURL)

    let capture = LocalCapture(
        captureID: "capture-delete",
        jobID: "job-delete",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "general_note",
        note: "Delete me",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        status: .failed,
        photos: [
            CapturePhoto(filename: "photo.jpg", fileURL: managedPhotoURL),
            CapturePhoto(filename: "external-photo.jpg", fileURL: externalPhotoURL),
        ],
        audio: CaptureAudio(filename: "voice.m4a", fileURL: managedAudioURL, durationSeconds: 5)
    )
    let snapshot = FieldCaptureSnapshot(
        account: .defaultProduction,
        session: .demo,
        sites: BTQSession.demo.sites,
        captures: [capture]
    )
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: snapshot),
        apiClient: MockCaptureAPIClient(),
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler(),
        mediaStore: mediaStore
    )
    await model.load()

    await model.deleteCapture(capture.captureID)

    #expect(model.captures.isEmpty)
    #expect(model.queueSummary.failed == 0)
    #expect(model.statusMessage == "Capture removed")
    #expect(FileManager.default.fileExists(atPath: managedPhotoURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: managedAudioURL.path) == false)
    #expect(FileManager.default.fileExists(atPath: externalPhotoURL.path) == true)
}

@Test @MainActor func refreshSessionUpdatesCachedAssignedSites() async {
    let apiClient = SequencedSessionAPIClient(sessions: [
        BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "old_site", label: "Old Site")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [
                BTQSite(siteID: "new_site", label: "New Site"),
                BTQSite(siteID: "supply_site", label: "Supply Site"),
            ],
            canSubmit: true,
            canReview: false,
            maxImages: 4
        ),
    ])
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: MemoryTokenStore(),
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await model.load()
    await model.connect(token: "token-123")

    #expect(model.sites.map(\.siteID) == ["old_site"])
    #expect(model.selectedSiteID == "old_site")
    #expect(model.session?.maxImages == 6)

    await model.refreshSessionIfPossible()

    #expect(model.sites.map(\.siteID) == ["new_site", "supply_site"])
    #expect(model.selectedSiteID == "new_site")
    #expect(model.selectedSite?.siteID == "new_site")
    #expect(model.sites.contains { $0.siteID == model.selectedSiteID })
    #expect(model.session?.maxImages == 4)
    #expect(model.statusMessage == "Session refreshed")
}

@Test @MainActor func refreshSubmittedHistoryLoadsServerSubmissions() async {
    let apiClient = SubmittedHistoryAPIClient(
        response: MySubmissionsResponse(
            submissions: [
                SubmittedCapture(
                    captureID: "cap-history-1",
                    siteID: "site_1",
                    siteName: "Site One",
                    targetID: "site_1",
                    capturedAt: "2026-06-14T10:15:00Z",
                    photoCount: 1,
                    hasAudio: true,
                    hasTextNote: true,
                    noteText: "Lobby needs towels",
                    track: "B",
                    stage: "reviewed",
                    outcomeLabel: "No action needed",
                    perPhotoQuality: [
                        SubmittedPhotoQuality(
                            severity: "degraded",
                            flags: ["too_dark"],
                            description: "Dark hallway",
                            possibleIssues: ["lights off"]
                        )
                    ]
                )
            ],
            qualitySummary: SubmissionQualitySummary(totalProcessed: 5, clear: 4, flagCounts: ["too_dark": 1])
        )
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("token-history", accountID: model.account.id)

    await model.refreshSubmittedHistory()

    #expect(model.submittedCaptures.map(\.captureID) == ["cap-history-1"])
    #expect(model.submissionQualitySummary?.clear == 4)
    #expect(model.statusMessage == "Submitted history refreshed.")
    #expect(await apiClient.requestedTokens == ["token-history"])
}

@Test @MainActor func reviewInboxLoadsGroupsAndCarriesRevisionThroughDecision() async {
    let session = BTQSession(
        person: BTQPerson(personID: "operator_1", name: "Operator One"),
        token: BTQToken(tokenID: "token_review", label: "Operator"),
        sites: [BTQSite(siteID: "SANDBOX", label: "Sandbox Site")],
        canSubmit: true,
        canReview: true,
        maxImages: 6,
        inboxCount: 2
    )
    let items = [
        InboxItem(
            draftID: "jd_1",
            revision: "1-a",
            sourceCaptureID: "cap_1",
            source: "voice",
            message: "Need towels",
            evidence: "East restroom is empty.",
            site: "Sandbox Site",
            siteID: "SANDBOX",
            groupID: "grp_1",
            submitterName: "Operator One",
            createdAt: "Today",
            jobType: "log_supply_need",
            payload: ["item_name": .string("Paper towels")]
        ),
        InboxItem(
            draftID: "jd_2",
            revision: "1-b",
            sourceCaptureID: "cap_1",
            source: "voice",
            message: "Need liners",
            evidence: "Supply closet is low.",
            site: "Sandbox Site",
            siteID: "SANDBOX",
            groupID: "grp_1",
            submitterName: "Operator One",
            createdAt: "Today",
            jobType: "log_supply_need",
            payload: ["item_name": .string("Can liners")]
        ),
    ]
    let apiClient = ReviewInboxAPIClient(
        session: session,
        inbox: InboxResponse(count: items.count, items: items),
        decisionResponse: InboxDecisionResponse(ok: true, draftID: "jd_1", status: "approved")
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: FieldCaptureSnapshot(account: .defaultProduction)),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("token-review", accountID: model.account.id)
    await model.load()

    await model.refreshInbox()

    #expect(model.canReviewInbox)
    #expect(model.inboxBadgeCount == 2)
    #expect(model.inboxItems.map(\.draftID) == ["jd_1", "jd_2"])
    #expect(model.inboxGroups.count == 1)

    await model.reviewInboxItem(items[0], action: .approve)

    #expect(model.inboxItems.map(\.draftID) == ["jd_2"])
    #expect(model.inboxBadgeCount == 1)
    #expect(model.statusMessage == "Approved.")
    #expect(await apiClient.decisions == [RecordedInboxDecision(action: .approve, draftID: "jd_1", revision: "1-a")])
}

@Test @MainActor func alreadyDecidedInboxDecisionDropsCardWithoutError() async {
    let session = BTQSession(
        person: BTQPerson(personID: "operator_1", name: "Operator One"),
        token: BTQToken(tokenID: "token_review", label: "Operator"),
        sites: [BTQSite(siteID: "SANDBOX", label: "Sandbox Site")],
        canSubmit: true,
        canReview: true,
        maxImages: 6,
        inboxCount: 1
    )
    let item = InboxItem(draftID: "jd_done", revision: "2-done", jobType: "append_to_note")
    let apiClient = ReviewInboxAPIClient(
        session: session,
        inbox: InboxResponse(count: 1, items: [item]),
        decisionResponse: InboxDecisionResponse(error: "already_decided")
    )
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(snapshot: FieldCaptureSnapshot(account: .defaultProduction)),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    await tokenStore.saveToken("token-review", accountID: model.account.id)
    await model.load()
    await model.refreshInbox()

    await model.reviewInboxItem(item, action: .reject)

    #expect(model.inboxItems.isEmpty)
    #expect(model.inboxBadgeCount == 0)
    #expect(model.statusMessage == "Already handled on another device.")
    #expect(await apiClient.decisions == [RecordedInboxDecision(action: .reject, draftID: "jd_done", revision: "2-done")])
}

@Test @MainActor func submittedHistoryRefreshDoesNotCrossAccountSwitch() async {
    let alphaID = UUID(uuidString: "00000000-0000-0000-0000-0000000000a1")!
    let betaID = UUID(uuidString: "00000000-0000-0000-0000-0000000000b2")!
    let alphaAccount = BTQAccount(
        id: alphaID,
        label: "Alpha",
        baseURL: URL(string: "https://alpha.example.test")!,
        tokenID: "token_alpha",
        personName: "Alpha User"
    )
    let betaAccount = BTQAccount(
        id: betaID,
        label: "Beta",
        baseURL: URL(string: "https://beta.example.test")!,
        tokenID: "token_beta",
        personName: "Beta User"
    )
    let alphaSite = BTQSite(siteID: "site_alpha", label: "Alpha Site")
    let betaSite = BTQSite(siteID: "site_beta", label: "Beta Site")
    let response = MySubmissionsResponse(
        submissions: [
            SubmittedCapture(
                captureID: "cap-alpha-history",
                siteID: alphaSite.siteID,
                siteName: alphaSite.label,
                targetID: alphaSite.siteID,
                capturedAt: "2026-06-14T10:15:00Z",
                photoCount: 0,
                hasAudio: false,
                hasTextNote: true,
                noteText: "Alpha-only history",
                track: "A",
                stage: "processed"
            )
        ],
        qualitySummary: SubmissionQualitySummary(totalProcessed: 1, clear: 1, flagCounts: [:])
    )
    let modelBox = FieldCaptureModelBox()
    let apiClient = ReentrantSubmittedHistoryAPIClient(response: response) {
        await modelBox.model?.switchAccount(betaID)
    }
    let tokenStore = MemoryTokenStore()
    let model = FieldCaptureModel(
        store: MemoryFieldCaptureStore(
            snapshot: FieldCaptureSnapshot(
                account: alphaAccount,
                activeAccountID: alphaID,
                accountWorkspaces: [
                    BTQAccountWorkspace(account: alphaAccount, session: nil, sites: [alphaSite]),
                    BTQAccountWorkspace(account: betaAccount, session: nil, sites: [betaSite]),
                ]
            )
        ),
        apiClient: apiClient,
        tokenStore: tokenStore,
        notificationScheduler: NoopUploadNotificationScheduler()
    )
    modelBox.model = model
    await model.load()
    await tokenStore.saveToken("alpha-token", accountID: alphaID)

    await model.refreshSubmittedHistory()

    #expect(model.account.id == betaID)
    #expect(model.submittedCaptures.isEmpty)
    #expect(model.submissionQualitySummary == nil)
    #expect(model.statusMessage == "Switched to Beta User")
    #expect(!model.isRefreshingSubmittedHistory)
    #expect(await apiClient.requestedTokens == ["alpha-token"])
}

@Test func apiClientUsesSessionAndSubmitContracts() async throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-api-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let photoURL = temp.appendingPathComponent("photo.jpg")
    try makeTestImageData(type: .jpeg).write(to: photoURL)
    let recorder = RequestRecorder()
    let client = HTTPCaptureAPIClient(session: makeStubbedSession(recorder: recorder), uploadBodyDirectory: temp)

    let session = try await client.session(baseURL: URL(string: "https://example.test")!, token: "token-123")
    let sessionRequest = await recorder.requests.first

    #expect(session.person.personID == "employee_1")
    #expect(sessionRequest?.url?.path == "/api/session")
    #expect(sessionRequest?.value(forHTTPHeaderField: "Authorization") == "Bearer token-123")
    #expect(sessionRequest?.value(forHTTPHeaderField: "Accept") == "application/json")

    let history = try await client.mySubmissions(baseURL: URL(string: "https://example.test")!, token: "token-history")
    let historyRequest = await recorder.requests.last

    #expect(history.submissions.first?.captureID == "cap-history-api")
    #expect(historyRequest?.url?.path == "/api/my-submissions")
    #expect(historyRequest?.httpMethod == "GET")
    #expect(historyRequest?.value(forHTTPHeaderField: "Authorization") == "Bearer token-history")
    #expect(historyRequest?.value(forHTTPHeaderField: "Accept") == "application/json")

    let inbox = try await client.inbox(baseURL: URL(string: "https://example.test")!, token: "token-review")
    let inboxRequest = await recorder.requests.last

    #expect(inbox.items.first?.draftID == "jd_api_1")
    #expect(inboxRequest?.url?.path == "/api/inbox")
    #expect(inboxRequest?.httpMethod == "GET")
    #expect(inboxRequest?.value(forHTTPHeaderField: "Authorization") == "Bearer token-review")

    let inboxItem = try #require(inbox.items.first)
    let approveResponse = try await client.decideInboxItem(
        action: .approve,
        item: inboxItem,
        reason: nil,
        baseURL: URL(string: "https://example.test")!,
        token: "token-review"
    )
    let approveRequest = await recorder.requests.last
    let approveBody = try #require(await recorder.bodies.last)
    let approveJSON = try JSONSerialization.jsonObject(with: approveBody) as? [String: Any]

    #expect(approveResponse.status == "approved")
    #expect(approveRequest?.url?.path == "/api/inbox/approve")
    #expect(approveRequest?.httpMethod == "POST")
    #expect(approveRequest?.value(forHTTPHeaderField: "Content-Type") == "application/json")
    #expect(approveJSON?["draft_id"] as? String == "jd_api_1")
    #expect(approveJSON?["_rev"] as? String == "1-api")

    let setResponse = try await client.decideInboxSet(
        [InboxSetDecisionEntry(draftID: "jd_api_1", revision: "1-api", checked: true)],
        baseURL: URL(string: "https://example.test")!,
        token: "token-review"
    )
    let setRequest = await recorder.requests.last
    let setBody = try #require(await recorder.bodies.last)
    let setJSON = try JSONSerialization.jsonObject(with: setBody) as? [String: Any]
    let setDrafts = setJSON?["drafts"] as? [[String: Any]]

    #expect(setResponse.approved == 1)
    #expect(setRequest?.url?.path == "/api/inbox/approve-set")
    #expect(setDrafts?.first?["draft_id"] as? String == "jd_api_1")
    #expect(setDrafts?.first?["_rev"] as? String == "1-api")
    #expect(setDrafts?.first?["checked"] as? Bool == true)

    let capture = LocalCapture(
        captureID: "cap-unified-api",
        jobID: "job-api",
        visitID: nil,
        siteID: "site_1",
        siteLabel: "Site One",
        targetID: "site_1",
        qcCategory: "supplies",
        note: "API test",
        capturedAt: Date(timeIntervalSince1970: 1_800_000_000),
        exportedAt: Date(timeIntervalSince1970: 1_800_000_001),
        photos: [CapturePhoto(filename: "photo.jpg", fileURL: photoURL)]
    )

    let response = try await client.submit(capture: capture, baseURL: URL(string: "https://example.test")!, token: "token-456")
    let request = await recorder.requests.last

    #expect(response.captureID == "cap-unified-api")
    #expect(request?.url?.path == "/api/submit")
    #expect(request?.httpMethod == "POST")
    #expect(request?.value(forHTTPHeaderField: "Authorization") == "Bearer token-456")
    #expect(request?.value(forHTTPHeaderField: "Content-Type")?.contains("multipart/form-data") == true)

    let errorClient = HTTPCaptureAPIClient(
        session: makeBackendErrorSession(
            status: 400,
            body: #"{"error":"too_many_images","message":"At most one photo may be submitted"}"#
        ),
        uploadBodyDirectory: temp
    )
    do {
        _ = try await errorClient.submit(capture: capture, baseURL: URL(string: "https://example.test")!, token: "token-456")
        Issue.record("Expected backend error payload to throw")
    } catch let error as CaptureAPIError {
        #expect(error == .serverStatus(status: 400, code: "too_many_images", message: "At most one photo may be submitted"))
        #expect(error.description == "At most one photo may be submitted")
    }
}

@Test func apiClientRejectsInsecureBaseURLBeforeBearerRequest() async throws {
    let temp = FileManager.default.temporaryDirectory.appendingPathComponent("btq-api-insecure-\(UUID().uuidString)", isDirectory: true)
    try FileManager.default.createDirectory(at: temp, withIntermediateDirectories: true)
    defer { try? FileManager.default.removeItem(at: temp) }

    let client = HTTPCaptureAPIClient(uploadBodyDirectory: temp)

    do {
        _ = try await client.session(baseURL: URL(string: "http://example.test")!, token: "token-123")
        Issue.record("Expected insecure base URL to throw")
    } catch let error as CaptureAPIError {
        #expect(error == .insecureBaseURL)
        #expect(error.description == "Server URL must use HTTPS")
    }
}

@Test func apiClientDefaultsToConnectivityAwareForegroundUploadSession() {
    let client = HTTPCaptureAPIClient()
    let configuration = client.session.configuration

    #expect(configuration.waitsForConnectivity)
    #expect(configuration.allowsExpensiveNetworkAccess)
    #expect(configuration.allowsConstrainedNetworkAccess)
    #expect(configuration.httpMaximumConnectionsPerHost == 2)
    #expect(configuration.identifier == nil)
}

private enum TestImageType {
    case jpeg
    case png

    var identifier: String {
        switch self {
        case .jpeg: UTType.jpeg.identifier
        case .png: UTType.png.identifier
        }
    }
}

private struct PhotoNoteExpectation: Decodable {
    var index: Int
    var filename: String
    var note: String
}

private struct ClientMetadataExpectation: Decodable {
    var schemaVersion: Int
    var client: String
    var visitID: String?
    var siteID: String
    var assetKind: String
    var photoCount: Int
    var hasAudio: Bool
    var audioDurationSeconds: Double?

    enum CodingKeys: String, CodingKey {
        case schemaVersion = "schema_version"
        case client
        case visitID = "visit_id"
        case siteID = "site_id"
        case assetKind = "asset_kind"
        case photoCount = "photo_count"
        case hasAudio = "has_audio"
        case audioDurationSeconds = "audio_duration_seconds"
    }
}

private func packageRoot() -> URL {
    URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent()
        .deletingLastPathComponent()
        .deletingLastPathComponent()
}

private func loadProjectPlist(_ relativePath: String) throws -> [String: Any] {
    let data = try Data(contentsOf: packageRoot().appendingPathComponent(relativePath))
    let plist = try PropertyListSerialization.propertyList(from: data, options: [], format: nil)
    return try #require(plist as? [String: Any])
}

private func imagePixelSize(at relativePath: String) throws -> CGSize {
    let url = packageRoot().appendingPathComponent(relativePath)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? CGFloat,
          let height = properties[kCGImagePropertyPixelHeight] as? CGFloat else {
        throw ImageNormalizerError.decodeFailed
    }
    return CGSize(width: width, height: height)
}

private func imageHasAlpha(at relativePath: String) throws -> Bool {
    let url = packageRoot().appendingPathComponent(relativePath)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw ImageNormalizerError.decodeFailed
    }
    switch image.alphaInfo {
    case .none, .noneSkipFirst, .noneSkipLast:
        return false
    default:
        return true
    }
}

private func captureSnapshot(account: BTQAccount = .defaultProduction, captures: [LocalCapture]) -> FieldCaptureSnapshot {
    FieldCaptureSnapshot(
        account: account,
        session: BTQSession(
            person: BTQPerson(personID: "person_field", name: "Field User"),
            token: BTQToken(tokenID: "token_field", label: "Pilot"),
            sites: [BTQSite(siteID: "site_1", label: "Site One")],
            canSubmit: true,
            canReview: false,
            maxImages: 6
        ),
        sites: [BTQSite(siteID: "site_1", label: "Site One")],
        captures: captures
    )
}

private func urlSchemes(in info: [String: Any]) -> [String] {
    guard let urlTypes = info["CFBundleURLTypes"] as? [[String: Any]] else { return [] }
    return urlTypes.flatMap { $0["CFBundleURLSchemes"] as? [String] ?? [] }
}

private func nonEmptyString(_ value: Any?) -> Bool {
    guard let string = value as? String else { return false }
    return !string.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
}

private enum TestFieldCaptureStoreError: Error {
    case saveFailed
}

private actor FailingSaveFieldCaptureStore: FieldCaptureStore {
    private let snapshot: FieldCaptureSnapshot

    init(snapshot: FieldCaptureSnapshot) {
        self.snapshot = snapshot
    }

    func load() async throws -> FieldCaptureSnapshot {
        snapshot
    }

    func save(_ snapshot: FieldCaptureSnapshot) async throws {
        throw TestFieldCaptureStoreError.saveFailed
    }
}

private func makeTestImageData(type: TestImageType, width: Int = 1, height: Int = 1, orientation: Int? = nil) throws -> Data {
    let colorSpace = CGColorSpaceCreateDeviceRGB()
    var pixels = Array(repeating: UInt32(0xFF_44_88_CC), count: width * height)
    guard let context = CGContext(
        data: &pixels,
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: width * 4,
        space: colorSpace,
        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
    ), let image = context.makeImage() else {
        throw ImageNormalizerError.encodeFailed
    }

    let data = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(data, type.identifier as CFString, 1, nil) else {
        throw ImageNormalizerError.encodeFailed
    }
    let properties = orientation.map { [kCGImagePropertyOrientation: $0] as CFDictionary }
    CGImageDestinationAddImage(destination, image, properties)
    guard CGImageDestinationFinalize(destination) else {
        throw ImageNormalizerError.encodeFailed
    }
    return data as Data
}

private actor RequestRecorder {
    private(set) var requests: [URLRequest] = []
    private(set) var bodies: [Data] = []

    func record(request: URLRequest, body: Data) {
        requests.append(request)
        bodies.append(body)
    }
}

private func makeStubbedSession(recorder: RequestRecorder) -> URLSession {
    StubURLProtocol.handler = { request in
        let body = request.httpBody ?? streamData(request.httpBodyStream)
        await recorder.record(request: request, body: body)
        if request.url?.path == "/api/session" {
            return (
                200,
                """
                {
                  "person": {"person_id": "employee_1", "name": "Field Person"},
                  "token": {"token_id": "token_1", "label": "Pilot"},
                  "sites": [],
                  "can_submit": true,
                  "can_review": false,
                  "max_images": 6,
                  "inbox_count": 0
                }
                """.data(using: .utf8)!
            )
        }
        if request.url?.path == "/api/my-submissions" {
            return (
                200,
                """
                {
                  "submissions": [
                    {
                      "capture_id": "cap-history-api",
                      "site_id": "site_1",
                      "site_name": "Site One",
                      "target_type": "location",
                      "target_id": "site_1",
                      "captured_at": "2026-06-14T10:15:00Z",
                      "photo_count": 1,
                      "has_audio": false,
                      "has_text_note": true,
                      "note_text": "Submitted from API test",
                      "photo_urls": ["/media/photo_1"],
                      "track": "A",
                      "stage": "processed",
                      "retargetable": false,
                      "outcome_label": "",
                      "per_photo_quality": []
                    }
                  ],
                  "quality_summary": {
                    "total_processed": 1,
                    "clear": 1,
                    "flag_counts": {}
                  }
                }
                """.data(using: .utf8)!
            )
        }
        if request.url?.path == "/api/inbox" {
            return (
                200,
                """
                {
                  "count": 1,
                  "items": [
                    {
                      "draft_id": "jd_api_1",
                      "_rev": "1-api",
                      "source_capture_id": "cap-api-1",
                      "source": "voice",
                      "message": "Supply need: paper towels",
                      "evidence": "Operator reported empty dispenser.",
                      "site": "Sandbox Site",
                      "site_id": "SANDBOX",
                      "group_id": "grp_api",
                      "submitter_name": "API User",
                      "created_at": "Today",
                      "job_type": "log_supply_need",
                      "payload": {"site_id": "SANDBOX", "item_name": "Paper towels"}
                    }
                  ]
                }
                """.data(using: .utf8)!
            )
        }
        if request.url?.path == "/api/inbox/approve" || request.url?.path == "/api/inbox/reject" {
            return (
                200,
                """
                {
                  "ok": true,
                  "draft_id": "jd_api_1",
                  "status": "approved"
                }
                """.data(using: .utf8)!
            )
        }
        if request.url?.path == "/api/inbox/approve-set" {
            return (
                200,
                """
                {
                  "ok": true,
                  "approved": 1,
                  "rejected": 0,
                  "already_decided": 0,
                  "results": [
                    {"draft_id": "jd_api_1", "action": "approve", "status": "approved", "_rev": "2-api"}
                  ]
                }
                """.data(using: .utf8)!
            )
        }
        return (
            201,
            """
            {
              "status": "submitted",
              "job_id": "job-api",
              "capture_id": "cap-unified-api",
              "couchdb_doc_id": "cap-unified-api",
              "photo_count": 1,
              "audio_count": 0
            }
            """.data(using: .utf8)!
        )
    }
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    return URLSession(configuration: configuration)
}

private func makeBackendErrorSession(status: Int, body: String) -> URLSession {
    StubURLProtocol.handler = { _ in
        (status, Data(body.utf8))
    }
    let configuration = URLSessionConfiguration.ephemeral
    configuration.protocolClasses = [StubURLProtocol.self]
    return URLSession(configuration: configuration)
}

private func streamData(_ stream: InputStream?) -> Data {
    guard let stream else { return Data() }
    stream.open()
    defer { stream.close() }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 4_096)
    while stream.hasBytesAvailable {
        let count = stream.read(&buffer, maxLength: buffer.count)
        if count > 0 {
            data.append(buffer, count: count)
        } else {
            break
        }
    }
    return data
}

private final class StubURLProtocol: URLProtocol, @unchecked Sendable {
    nonisolated(unsafe) static var handler: ((URLRequest) async throws -> (Int, Data))?

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Task {
            do {
                let (status, data) = try await Self.handler?(request) ?? (500, Data())
                let response = HTTPURLResponse(
                    url: request.url!,
                    statusCode: status,
                    httpVersion: "HTTP/1.1",
                    headerFields: ["Content-Type": "application/json"]
                )!
                client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
                client?.urlProtocol(self, didLoad: data)
                client?.urlProtocolDidFinishLoading(self)
            } catch {
                client?.urlProtocol(self, didFailWithError: error)
            }
        }
    }

    override func stopLoading() {}
}

private actor SequencedSessionAPIClient: CaptureAPIClient {
    private var sessions: [BTQSession]

    init(sessions: [BTQSession]) {
        self.sessions = sessions
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        guard !sessions.isEmpty else { return .demo }
        return sessions.removeFirst()
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor RecordingSessionAPIClient: CaptureAPIClient {
    private let sessionResponse: BTQSession
    private(set) var requestedTokens: [String] = []

    init(session: BTQSession = .demo) {
        self.sessionResponse = session
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        requestedTokens.append(token)
        return sessionResponse
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor OneGoodSessionThenUnauthorizedAPIClient: CaptureAPIClient {
    private var sessionResponse: BTQSession?

    init(session: BTQSession) {
        self.sessionResponse = session
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        guard let sessionResponse else {
            throw CaptureAPIError.unauthorized
        }
        self.sessionResponse = nil
        return sessionResponse
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor FailingSubmitAPIClient: CaptureAPIClient {
    private let error: Error

    init(error: Error) {
        self.error = error
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        throw error
    }
}

@MainActor
private final class FieldCaptureModelBox {
    var model: FieldCaptureModel?
}

private actor ReentrantSubmitAPIClient: CaptureAPIClient {
    private var submitted: [LocalCapture] = []
    private let onSubmit: @MainActor @Sendable (LocalCapture) async -> Void

    var submittedCaptureIDs: [String] {
        submitted.map(\.captureID)
    }

    init(onSubmit: @escaping @MainActor @Sendable (LocalCapture) async -> Void) {
        self.onSubmit = onSubmit
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        submitted.append(capture)
        await onSubmit(capture)
        return SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor FlakySubmitAPIClient: CaptureAPIClient {
    private var errors: [Error]
    private var submitted: [LocalCapture] = []

    var submittedCount: Int {
        submitted.count
    }

    var submittedCaptureIDs: [String] {
        submitted.map(\.captureID)
    }

    init(errors: [Error]) {
        self.errors = errors
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        if !errors.isEmpty {
            throw errors.removeFirst()
        }
        submitted.append(capture)
        return SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor SubmittedHistoryAPIClient: CaptureAPIClient {
    private let response: MySubmissionsResponse
    private(set) var requestedTokens: [String] = []

    init(response: MySubmissionsResponse) {
        self.response = response
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        requestedTokens.append(token)
        return response
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private struct RecordedInboxDecision: Equatable, Sendable {
    var action: InboxDecisionAction
    var draftID: String
    var revision: String
}

private actor ReviewInboxAPIClient: CaptureAPIClient {
    private let sessionResponse: BTQSession
    private let inboxResponse: InboxResponse
    private let decisionResponse: InboxDecisionResponse
    private(set) var decisions: [RecordedInboxDecision] = []

    init(session: BTQSession, inbox: InboxResponse, decisionResponse: InboxDecisionResponse) {
        self.sessionResponse = session
        self.inboxResponse = inbox
        self.decisionResponse = decisionResponse
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        sessionResponse
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        MySubmissionsResponse(submissions: [])
    }

    func inbox(baseURL: URL, token: String) async throws -> InboxResponse {
        inboxResponse
    }

    func decideInboxItem(action: InboxDecisionAction, item: InboxItem, reason: String?, baseURL: URL, token: String) async throws -> InboxDecisionResponse {
        decisions.append(RecordedInboxDecision(action: action, draftID: item.draftID, revision: item.revision))
        return decisionResponse
    }

    func decideInboxSet(_ drafts: [InboxSetDecisionEntry], baseURL: URL, token: String) async throws -> InboxSetDecisionResponse {
        for draft in drafts {
            decisions.append(RecordedInboxDecision(action: draft.checked ? .approve : .reject, draftID: draft.draftID, revision: draft.revision))
        }
        return InboxSetDecisionResponse(ok: true, approved: drafts.filter(\.checked).count, rejected: drafts.filter { !$0.checked }.count)
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private actor ReentrantSubmittedHistoryAPIClient: CaptureAPIClient {
    private let response: MySubmissionsResponse
    private let onMySubmissions: @MainActor @Sendable () async -> Void
    private(set) var requestedTokens: [String] = []

    init(response: MySubmissionsResponse, onMySubmissions: @escaping @MainActor @Sendable () async -> Void) {
        self.response = response
        self.onMySubmissions = onMySubmissions
    }

    func session(baseURL: URL, token: String) async throws -> BTQSession {
        .demo
    }

    func mySubmissions(baseURL: URL, token: String) async throws -> MySubmissionsResponse {
        requestedTokens.append(token)
        await onMySubmissions()
        return response
    }

    func submit(capture: LocalCapture, baseURL: URL, token: String) async throws -> SubmitCaptureResponse {
        SubmitCaptureResponse(
            status: "submitted",
            jobID: capture.jobID,
            captureID: capture.captureID,
            couchdbDocID: capture.captureID,
            photoCount: capture.photos.count,
            audioCount: capture.audio == nil ? 0 : 1,
            idempotentReplay: false
        )
    }
}

private struct RecordingUploadFailure: Equatable, Sendable {
    var captureID: String
    var reason: String
    var siteLabel: String = ""
}

private actor RecordingUploadNotificationScheduler: UploadNotificationScheduling {
    private(set) var failedUploads: [RecordingUploadFailure] = []
    private(set) var syncRecoveredPendingCounts: [Int] = []
    private(set) var authorizationRequestCount = 0
    private(set) var testAlertCount = 0
    private var status: NotificationPermissionStatus
    private let requestedStatus: NotificationPermissionStatus

    init(status: NotificationPermissionStatus = .authorized, requestedStatus: NotificationPermissionStatus = .authorized) {
        self.status = status
        self.requestedStatus = requestedStatus
    }

    func authorizationStatus() async -> NotificationPermissionStatus {
        status
    }

    func requestAuthorization() async -> NotificationPermissionStatus {
        authorizationRequestCount += 1
        status = requestedStatus
        return status
    }

    func notifyTestAlert() async {
        testAlertCount += 1
    }

    func notifyUploadFailed(capture: LocalCapture, reason: String) async {
        failedUploads.append(RecordingUploadFailure(captureID: capture.captureID, reason: reason, siteLabel: capture.siteLabel))
    }

    func notifySyncRecovered(pendingCount: Int) async {
        syncRecoveredPendingCounts.append(pendingCount)
    }
}

private actor RecordingVoicePermissionChecker: VoiceRecordingPermissionChecking {
    private(set) var authorizationRequestCount = 0
    private var status: VoiceRecordingPermissionStatus
    private let requestedStatus: VoiceRecordingPermissionStatus

    init(status: VoiceRecordingPermissionStatus, requestedStatus: VoiceRecordingPermissionStatus) {
        self.status = status
        self.requestedStatus = requestedStatus
    }

    func authorizationStatus() async -> VoiceRecordingPermissionStatus {
        status
    }

    func requestAuthorization() async -> VoiceRecordingPermissionStatus {
        authorizationRequestCount += 1
        status = requestedStatus
        return status
    }
}

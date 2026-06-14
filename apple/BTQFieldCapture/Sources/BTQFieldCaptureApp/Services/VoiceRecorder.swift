import AVFAudio
import AVFoundation
import Foundation
import Observation

public enum VoiceRecordingPermissionStatus: Equatable, Sendable {
    case notDetermined
    case granted
    case denied
    case unknown

    public var allowsRecording: Bool {
        self == .granted
    }
}

public protocol VoiceRecordingPermissionChecking: Sendable {
    func authorizationStatus() async -> VoiceRecordingPermissionStatus
    func requestAuthorization() async -> VoiceRecordingPermissionStatus
}

public struct SystemVoiceRecordingPermissionChecker: VoiceRecordingPermissionChecking {
    public init() {}

    public func authorizationStatus() async -> VoiceRecordingPermissionStatus {
        VoiceRecordingPermissionStatus(AVAudioApplication.shared.recordPermission)
    }

    public func requestAuthorization() async -> VoiceRecordingPermissionStatus {
        let status = await authorizationStatus()
        switch status {
        case .granted, .denied:
            return status
        case .notDetermined, .unknown:
            let granted = await withCheckedContinuation { continuation in
                AVAudioApplication.requestRecordPermission { granted in
                    continuation.resume(returning: granted)
                }
            }
            return granted ? .granted : .denied
        }
    }
}

public enum VoiceRecordingInterruptionAction: Equatable, Sendable {
    case ignore
    case pause
    case resume
    case remainPaused
}

public struct VoiceRecordingInterruptionPolicy: Sendable {
    private var pausedByInterruption = false

    public init() {}

    public mutating func interruptionBegan(isRecording: Bool, isPaused: Bool) -> VoiceRecordingInterruptionAction {
        guard isRecording else {
            pausedByInterruption = false
            return .ignore
        }
        if isPaused {
            return .remainPaused
        }
        pausedByInterruption = true
        return .pause
    }

    public mutating func interruptionEnded(shouldResume: Bool) -> VoiceRecordingInterruptionAction {
        defer { pausedByInterruption = false }
        guard pausedByInterruption else { return .remainPaused }
        return shouldResume ? .resume : .remainPaused
    }
}

@MainActor
@Observable
public final class VoiceRecorder {
    public private(set) var isRecording = false
    public private(set) var isPaused = false
    public private(set) var isPlaying = false
    public private(set) var lastAudio: CaptureAudio?
    public private(set) var errorMessage: String?
    public private(set) var permissionStatus: VoiceRecordingPermissionStatus = .unknown
    public private(set) var elapsedSeconds: Double = 0

    @ObservationIgnored private let permissionChecker: any VoiceRecordingPermissionChecking
    private var recorder: AVAudioRecorder?
    private var player: AVAudioPlayer?
    private var playbackObserver: VoicePlaybackObserver?
    private var playbackTask: Task<Void, Never>?
    private var durationTask: Task<Void, Never>?
    private var currentURL: URL?
    private var interruptionPolicy = VoiceRecordingInterruptionPolicy()
    #if os(iOS)
    nonisolated(unsafe) private var interruptionTask: Task<Void, Never>?
    #endif

    public init(permissionChecker: any VoiceRecordingPermissionChecking = SystemVoiceRecordingPermissionChecker()) {
        self.permissionChecker = permissionChecker
        #if os(iOS)
        interruptionTask = Task { [weak self] in
            for await notification in NotificationCenter.default.notifications(
                named: AVAudioSession.interruptionNotification
            ) {
                await self?.handleAudioSessionInterruption(notification)
            }
        }
        #endif
    }

    deinit {
        #if os(iOS)
        interruptionTask?.cancel()
        #endif
    }

    public func refreshPermissionStatus() async {
        permissionStatus = await permissionChecker.authorizationStatus()
    }

    public func start() async {
        do {
            stopPlayback()
            deleteTemporaryAudio(lastAudio)
            lastAudio = nil
            elapsedSeconds = 0
            permissionStatus = await permissionChecker.requestAuthorization()
            guard permissionStatus.allowsRecording else {
                errorMessage = "Microphone access is required for voice notes."
                return
            }
            #if os(iOS)
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playAndRecord, mode: .spokenAudio, options: [.defaultToSpeaker, .allowBluetooth])
            try session.setActive(true)
            #endif

            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("btq-voice-\(UUID().uuidString).m4a")
            let settings: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatMPEG4AAC),
                AVSampleRateKey: 44_100,
                AVNumberOfChannelsKey: 1,
                AVEncoderAudioQualityKey: AVAudioQuality.high.rawValue,
            ]
            let recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder.prepareToRecord()
            recorder.record()
            self.recorder = recorder
            currentURL = url
            isRecording = true
            isPaused = false
            errorMessage = nil
            startDurationTimer()
        } catch {
            stopDurationTimer()
            errorMessage = error.localizedDescription
        }
    }

    public func pause() {
        guard isRecording, !isPaused else { return }
        elapsedSeconds = recorder?.currentTime ?? elapsedSeconds
        recorder?.pause()
        isPaused = true
    }

    public func resume() {
        guard isRecording, isPaused else { return }
        recorder?.record()
        isPaused = false
        errorMessage = nil
    }

    @discardableResult
    public func stop() -> CaptureAudio? {
        guard let recorder, let currentURL else { return nil }
        let duration = recorder.currentTime
        recorder.stop()
        stopDurationTimer()
        elapsedSeconds = duration
        isRecording = false
        isPaused = false
        let audio = CaptureAudio(
            filename: currentURL.lastPathComponent,
            mimeType: "audio/mp4",
            fileURL: currentURL,
            durationSeconds: duration
        )
        lastAudio = audio
        self.recorder = nil
        self.currentURL = nil
        return audio
    }

    @discardableResult
    public func finalizeForSave() -> CaptureAudio? {
        if isRecording || isPaused {
            return stop()
        }
        return lastAudio
    }

    public func play() {
        guard let url = lastAudio?.fileURL else { return }
        do {
            stopPlayback()
            #if os(iOS)
            try AVAudioSession.sharedInstance().setCategory(.playback, mode: .spokenAudio)
            try AVAudioSession.sharedInstance().setActive(true)
            #endif
            let player = try AVAudioPlayer(contentsOf: url)
            let playbackObserver = VoicePlaybackObserver(
                onFinish: { [weak self] in
                    Task { @MainActor in
                        self?.finishPlayback()
                    }
                },
                onError: { [weak self] error in
                    Task { @MainActor in
                        self?.finishPlayback(errorMessage: error?.localizedDescription ?? "Voice memo playback failed.")
                    }
                }
            )
            player.prepareToPlay()
            player.delegate = playbackObserver
            player.play()
            self.player = player
            self.playbackObserver = playbackObserver
            isPlaying = true
            let duration = max(player.duration, 0.25)
            playbackTask = Task { [weak self] in
                try? await Task.sleep(nanoseconds: UInt64((duration + 0.5) * 1_000_000_000))
                await MainActor.run {
                    self?.finishPlaybackIfCompleted()
                }
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    public func stopPlayback() {
        playbackTask?.cancel()
        playbackTask = nil
        player?.stop()
        player = nil
        playbackObserver = nil
        isPlaying = false
    }

    public func clear() {
        if isRecording || isPaused {
            _ = stop()
        }
        stopPlayback()
        deleteTemporaryAudio(lastAudio)
        lastAudio = nil
        elapsedSeconds = 0
        errorMessage = nil
    }

    nonisolated public static func formatDuration(_ duration: Double) -> String {
        let seconds = max(0, Int(duration.rounded()))
        return "\(seconds / 60):\(String(format: "%02d", seconds % 60))"
    }

    private func startDurationTimer() {
        stopDurationTimer()
        elapsedSeconds = recorder?.currentTime ?? 0
        durationTask = Task { [weak self] in
            while !Task.isCancelled {
                try? await Task.sleep(nanoseconds: 250_000_000)
                guard let self else { break }
                await MainActor.run {
                    guard let recorder = self.recorder else { return }
                    self.elapsedSeconds = recorder.currentTime
                }
            }
        }
    }

    private func stopDurationTimer() {
        durationTask?.cancel()
        durationTask = nil
    }

    private func finishPlaybackIfCompleted() {
        guard isPlaying, player?.isPlaying != true else { return }
        finishPlayback()
    }

    private func finishPlayback(errorMessage: String? = nil) {
        playbackTask?.cancel()
        playbackTask = nil
        player = nil
        playbackObserver = nil
        isPlaying = false
        if let errorMessage {
            self.errorMessage = errorMessage
        }
    }

    #if os(iOS)
    public func handleAudioSessionInterruption(_ notification: Notification) {
        guard let typeValue = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: typeValue) else {
            return
        }

        switch type {
        case .began:
            switch interruptionPolicy.interruptionBegan(isRecording: isRecording, isPaused: isPaused) {
            case .pause:
                pause()
                errorMessage = "Recording paused by interruption."
            case .remainPaused:
                errorMessage = "Recording interrupted while paused."
            case .ignore, .resume:
                break
            @unknown default:
                break
            }
        case .ended:
            let optionsValue = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            let options = AVAudioSession.InterruptionOptions(rawValue: optionsValue)
            switch interruptionPolicy.interruptionEnded(shouldResume: options.contains(.shouldResume)) {
            case .resume:
                resume()
            case .remainPaused:
                if isRecording, isPaused {
                    errorMessage = "Recording paused. Tap Resume to continue."
                }
            case .ignore, .pause:
                break
            @unknown default:
                break
            }
        @unknown default:
            break
        }
    }
    #endif

    private func deleteTemporaryAudio(_ audio: CaptureAudio?) {
        guard let url = audio?.fileURL else { return }
        let tempPath = FileManager.default.temporaryDirectory.standardizedFileURL.path
        let audioPath = url.standardizedFileURL.path
        guard audioPath == tempPath || audioPath.hasPrefix(tempPath + "/") else { return }
        try? FileManager.default.removeItem(at: url)
    }
}

private extension VoiceRecordingPermissionStatus {
    init(_ permission: AVAudioApplication.recordPermission) {
        switch permission {
        case .undetermined:
            self = .notDetermined
        case .denied:
            self = .denied
        case .granted:
            self = .granted
        @unknown default:
            self = .unknown
        }
    }
}

private final class VoicePlaybackObserver: NSObject, AVAudioPlayerDelegate {
    private let onFinish: () -> Void
    private let onError: (Error?) -> Void

    init(onFinish: @escaping () -> Void, onError: @escaping (Error?) -> Void) {
        self.onFinish = onFinish
        self.onError = onError
    }

    func audioPlayerDidFinishPlaying(_ player: AVAudioPlayer, successfully flag: Bool) {
        if flag {
            onFinish()
        } else {
            onError(nil)
        }
    }

    func audioPlayerDecodeErrorDidOccur(_ player: AVAudioPlayer, error: (any Error)?) {
        onError(error)
    }
}

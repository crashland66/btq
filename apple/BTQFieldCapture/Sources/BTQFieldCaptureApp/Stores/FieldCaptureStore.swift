import Foundation

public struct FieldCaptureSnapshot: Codable, Equatable, Sendable {
    public var account: BTQAccount
    public var session: BTQSession?
    public var sites: [BTQSite]
    public var visits: [Visit]
    public var captures: [LocalCapture]
    public var activeAccountID: UUID?
    public var accountWorkspaces: [BTQAccountWorkspace]

    public init(
        account: BTQAccount,
        session: BTQSession? = nil,
        sites: [BTQSite] = [],
        visits: [Visit] = [],
        captures: [LocalCapture] = [],
        activeAccountID: UUID? = nil,
        accountWorkspaces: [BTQAccountWorkspace]? = nil
    ) {
        self.account = account
        self.session = session
        self.sites = sites
        self.visits = visits
        self.captures = captures
        self.activeAccountID = activeAccountID ?? account.id
        self.accountWorkspaces = accountWorkspaces ?? [
            BTQAccountWorkspace(
                account: account,
                session: session,
                sites: sites,
                visits: visits,
                captures: captures
            )
        ]
    }

    enum CodingKeys: String, CodingKey {
        case account
        case session
        case sites
        case visits
        case captures
        case activeAccountID
        case accountWorkspaces
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        account = try container.decodeIfPresent(BTQAccount.self, forKey: .account) ?? .defaultProduction
        session = try container.decodeIfPresent(BTQSession.self, forKey: .session)
        sites = try container.decodeIfPresent([BTQSite].self, forKey: .sites) ?? []
        visits = try container.decodeIfPresent([Visit].self, forKey: .visits) ?? []
        captures = try container.decodeIfPresent([LocalCapture].self, forKey: .captures) ?? []
        activeAccountID = try container.decodeIfPresent(UUID.self, forKey: .activeAccountID) ?? account.id
        let decodedWorkspaces = try container.decodeIfPresent([BTQAccountWorkspace].self, forKey: .accountWorkspaces)
        accountWorkspaces = decodedWorkspaces?.isEmpty == false ? decodedWorkspaces! : [
            BTQAccountWorkspace(
                account: account,
                session: session,
                sites: sites,
                visits: visits,
                captures: captures
            )
        ]
    }
}

public protocol FieldCaptureStore: Sendable {
    func load() async throws -> FieldCaptureSnapshot
    func save(_ snapshot: FieldCaptureSnapshot) async throws
    func appendDraftPhoto(
        _ photo: CapturePhoto,
        to draft: LocalCapture,
        accountID: UUID,
        snapshot: FieldCaptureSnapshot
    ) async throws
}

public extension FieldCaptureStore {
    /// Stores may override this with a normalized append. The fallback preserves the
    /// existing behavior for lightweight and test stores that only support snapshots.
    func appendDraftPhoto(
        _ photo: CapturePhoto,
        to draft: LocalCapture,
        accountID: UUID,
        snapshot: FieldCaptureSnapshot
    ) async throws {
        try await save(snapshot)
    }
}

public actor JSONFieldCaptureStore: FieldCaptureStore {
    private let fileURL: URL
    private let encoder: JSONEncoder
    private let decoder: JSONDecoder
    private var cached: FieldCaptureSnapshot?

    public init(fileURL: URL = JSONFieldCaptureStore.defaultFileURL()) {
        self.fileURL = fileURL
        encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
    }

    public func load() async throws -> FieldCaptureSnapshot {
        if let cached {
            return cached
        }
        guard FileManager.default.fileExists(atPath: fileURL.path) else {
            let snapshot = FieldCaptureSnapshot(account: .defaultProduction)
            cached = snapshot
            return snapshot
        }
        let data = try Data(contentsOf: fileURL)
        let snapshot = try decoder.decode(FieldCaptureSnapshot.self, from: data)
        cached = snapshot
        return snapshot
    }

    public func save(_ snapshot: FieldCaptureSnapshot) async throws {
        try FileManager.default.createDirectory(at: fileURL.deletingLastPathComponent(), withIntermediateDirectories: true)
        let data = try encoder.encode(snapshot)
        try data.write(to: fileURL, options: [.atomic])
        cached = snapshot
    }

    public static func defaultFileURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base
            .appendingPathComponent("BTQFieldCapture", isDirectory: true)
            .appendingPathComponent("state.json")
    }
}

public actor MemoryFieldCaptureStore: FieldCaptureStore {
    private var snapshot: FieldCaptureSnapshot

    public init(snapshot: FieldCaptureSnapshot = FieldCaptureSnapshot(account: .defaultProduction, session: .demo, sites: BTQSession.demo.sites)) {
        self.snapshot = snapshot
    }

    public func load() async throws -> FieldCaptureSnapshot {
        snapshot
    }

    public func save(_ snapshot: FieldCaptureSnapshot) async throws {
        self.snapshot = snapshot
    }
}

public extension BTQAccount {
    static let defaultProduction = BTQAccount(
        label: "BTQ Field Capture",
        baseURL: URL(string: "https://fc.gregstoltz.com")!
    )
}
